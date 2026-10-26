"""Acquire snapshots synchronously; persist and send them in bounded workers."""
from collections import OrderedDict, deque
from collections.abc import Mapping
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
from queue import Queue, Empty, Full
import re
import socket
import threading
import time
import uuid

from modules.telemetry.metrics import ListDynamicMetrics, ListStaticMetrics


TIMING_COLUMNS = ["sample_id", "scheduled_at_utc", "acquired_start_utc", "acquired_end_utc",
                  "acquisition_duration_ms", "schedule_lag_ms", "skipped_intervals", "run_id",
                  "effective_interval_ms", "wall_clock_step_ms", "query_timing_json"]
TIMING_COLUMNS += ['persistence_dropped_total', 'udp_dropped_total']
TRAFFIC_FIELDS = ('throughput_upload_kb', 'throughput_download_kb',
                  'throughput_upload_speed_kbps', 'throughput_download_speed_kbps')


def fields(schema):
    return [value for key, value in vars(schema).items() if not key.startswith("_")]


class Telemetry:
    MAX_CSV_HANDLES = 8
    MAX_CSV_SCHEMAS = 32
    MAX_STATIC_IDENTITIES = 32

    def __init__(self, settings, stop, publish=None):
        self.settings, self.stop, self.publish = settings, stop, publish
        self.log = logging.getLogger(__name__)
        self.previous = None
        self.gps = {}
        self._gps_connection = dict(connected=False, error=None, connected_at_utc=None,
                                    last_message_at_utc=None)
        self.sequence = 0
        self.run_id = uuid.uuid4().hex
        self._previous_start = None
        self._previous_end = None
        self._durations = deque(maxlen=256)
        self._lags = deque(maxlen=256)
        self._write_durations = deque(maxlen=256)
        self._flush_durations = deque(maxlen=256)
        self._flushing = False
        self._last_write_latency_ms = None
        self._last_write_sample_id = self._last_write_success = self._last_write_duration_ms = None
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._accepting = settings.enabled
        self._counts = dict(acquired=0, persisted=0, persistence_dropped=0, persistence_failed=0,
                            udp_dropped=0, udp_sent=0, udp_failed=0, publish_failed=0, flush_failed=0)
        self._reported_drops = {"persistence_dropped": 0, "udp_dropped": 0}
        self._pending = {}
        self._udp_pending = {}
        self._inflight = self._udp_inflight = False
        self._last_persisted_sample_id = None
        self._last_acquisition_duration_ms = self._last_schedule_lag_ms = None
        self._last_persistence_error = None
        self._csv_files = OrderedDict()
        self._validated = OrderedDict()
        self._static_last = OrderedDict()
        self._rotation_day = None
        self._last_flush = time.monotonic()
        self._flush_interval = getattr(settings, "flush_interval", 1.0)
        self.persistence = Queue(maxsize=getattr(settings, "queue_size", 64))
        self.outgoing = Queue(maxsize=1)
        self._writer = self._udp_worker = self._gps_worker = None
        if settings.enabled and (settings.csv or settings.json):
            self._writer = threading.Thread(target=self._write_loop, name="telemetry-storage", daemon=True)
            self._writer.start()
        if settings.enabled and (settings.udp_host or settings.collector_host):
            self._udp_worker = threading.Thread(target=self.send_udp, name="telemetry-udp", daemon=True)
            self._udp_worker.start()
        if settings.enabled and settings.gps and getattr(settings, 'gps_source', 'gpsd') == 'gpsd':
            self._gps_worker = threading.Thread(target=self.read_gps, name="telemetry-gps", daemon=True)
            self._gps_worker.start()

    def stats(self):
        """Return a consistent snapshot; pending includes the in-flight write."""
        now = time.monotonic()
        with self._lock:
            def p95(values):
                ordered = sorted(values)
                return ordered[max(0, math.ceil(len(ordered)*.95)-1)] if ordered else None
            return dict(self._counts, pending=len(self._pending), queued=self.persistence.qsize(),
                        inflight=self._inflight, queue_capacity=self.persistence.maxsize,
                        queue_age_ms=max(0, (now-min(self._pending.values()))*1000) if self._pending else 0,
                        udp_pending=len(self._udp_pending), udp_inflight=self._udp_inflight,
                        udp_queue_age_ms=max(0, (now-min(self._udp_pending.values()))*1000) if self._udp_pending else 0,
                        last_sample_id=self.sequence, last_persisted_sample_id=self._last_persisted_sample_id,
                        last_acquisition_duration_ms=self._last_acquisition_duration_ms,
                        last_schedule_lag_ms=self._last_schedule_lag_ms,
                        last_persistence_error=self._last_persistence_error,
                        acquisition_p95_ms=p95(self._durations), schedule_lag_p95_ms=p95(self._lags),
                        write_p95_ms=p95(self._write_durations), last_write_latency_ms=self._last_write_latency_ms,
                        last_write_sample_id=self._last_write_sample_id,
                        last_write_success=self._last_write_success, last_write_duration_ms=self._last_write_duration_ms,
                        flush_p95_ms=p95(self._flush_durations), flushing=self._flushing,
                        persistence_semantics='buffered; flush interval applies; no fsync guarantee',
                        workers_alive=[worker.name for worker in (self._writer, self._udp_worker, self._gps_worker)
                                       if worker is not None and worker.is_alive()],
                        accepting=self._accepting, writer_alive=bool(self._writer and self._writer.is_alive()))

    def _record_gps(self, result):
        """Keep receiver time separate from local reception and replace an old fix."""
        if not isinstance(result, Mapping):
            return
        kind = result.get("class")
        if not isinstance(kind, str):
            return
        kind = kind.lower()
        if kind not in {"tpv", "sky"}:
            return
        source_time, source_epoch, values = result.get("time"), None, {}
        if source_time is not None:
            try:
                if type(source_time) in {int, float}:
                    source_epoch = float(source_time)
                elif isinstance(source_time, str):
                    parsed = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        source_epoch = parsed.timestamp()
                if source_epoch is not None and not math.isfinite(source_epoch):
                    source_epoch = None
            except (ValueError, OverflowError, OSError):
                pass
            if source_epoch is not None:
                values["datetime"] = source_time
                values["timestamp"] = source_epoch
        if type(source_time) in {int, float} and source_epoch is None:
            source_time = None
        mode = result.get('mode')
        if type(mode) is not int or mode not in {0, 1, 2, 3}:
            mode = None
        for key in ("lat", "lon", "alt", "altHAE", "altMSL", "epx", "epy", "epv", "speed", "eps", "hdop", "pdop"):
            if key not in result or (kind == 'tpv' and (mode is None or mode < 2)):
                continue
            value = result[key]
            try:
                valid = type(value) in {int, float} and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid or (key == 'lat' and not -90 <= value <= 90) or (key == 'lon' and not -180 <= value <= 180):
                continue
            if kind == 'tpv' and key in {'alt', 'altHAE', 'altMSL', 'epv'} and mode < 3:
                continue
            values[key.lower()] = value
        received_monotonic, received_utc = time.monotonic(), datetime.now(timezone.utc)
        record = dict(values=values, source_time=source_time, source_epoch=source_epoch,
                      age_at_receipt=max(0, received_utc.timestamp()-source_epoch) if source_epoch is not None else 0,
                      mode=mode, has_fix=kind == 'tpv' and 'lat' in values and 'lon' in values,
                      received_monotonic=received_monotonic, received_at_utc=received_utc.isoformat())
        with self._lock:
            self.gps[kind] = record
            self._gps_connection.update(connected=True, error=None, last_message_at_utc=record['received_at_utc'])

    def _gps_connected(self, connected, error=None):
        with self._lock:
            self._gps_connection.update(connected=connected, error=error)
            if connected:
                self._gps_connection['connected_at_utc'] = datetime.now(timezone.utc).isoformat()

    def read_gps(self):
        while not self._closing.is_set() and not self.stop.is_set():
            error = None
            try:
                with socket.create_connection(("127.0.0.1", 2947), timeout=2) as connection:
                    connection.sendall(b'?WATCH={"enable":true,"json":true};\n')
                    connection.settimeout(1)
                    self._gps_connected(True)
                    buffer = b""
                    while not self._closing.is_set() and not self.stop.is_set():
                        try:
                            part = connection.recv(16384)
                        except socket.timeout:
                            continue
                        if not part:
                            error = 'GPSd closed the connection'
                            break
                        buffer += part
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            try:
                                result = json.loads(line)
                                if isinstance(result, dict):
                                    self._record_gps(result)
                            except (ValueError, UnicodeDecodeError):
                                continue
                        if len(buffer) > 65536:
                            buffer = b""
            except OSError as failure:
                error = 'GPSd unavailable: ' + str(failure)[:224]
                self.log.debug("%s", error)
            finally:
                self._gps_connected(False, error)
            self._closing.wait(1)

    def _gps_snapshot(self, monotonic_now, utc_now):
        with self._lock:
            records = deepcopy(self.gps)
            connected = self._gps_connection['connected']
        values, report = {}, {}
        for kind, record in records.items():
            elapsed = max(0, monotonic_now-record["received_monotonic"])
            age = record['age_at_receipt'] + elapsed
            if record["source_epoch"] is not None:
                age = max(age, utc_now.timestamp()-record["source_epoch"])
            fresh = age <= getattr(self.settings, "gps_max_age", 10.0) and (kind != "tpv" or record["source_epoch"] is not None)
            report[kind] = dict(source_time=record["source_time"], received_at_utc=record["received_at_utc"],
                                age_ms=age*1000, age_at_receipt_ms=record['age_at_receipt']*1000,
                                fresh=fresh, mode=record['mode'], has_fix=record['has_fix'])
            if fresh and connected:
                values.update({f"gpsd_{kind}_{key}": value for key, value in record["values"].items()})
        return values, report

    def _gps_status(self, records):
        with self._lock:
            connection = dict(self._gps_connection)
        tpv = records.get('tpv')
        if not connection['connected']:
            status = 'disconnected'
        elif not tpv:
            status = 'no_fix' if records else 'connected'
        elif not tpv['fresh']:
            status = 'stale'
        elif tpv['has_fix']:
            status = 'fixed'
        else:
            status = 'no_fix'
        return dict(connection, enabled=True, source='gpsd', status=status,
                    max_age_seconds=getattr(self.settings, 'gps_max_age', 10.0), **records)

    @staticmethod
    def traffic_metadata(interface, enabled):
        return dict(enabled=bool(enabled), interface=interface, source='sysfs',
                    status='initializing' if enabled else 'disabled', error=None,
                    observed_at_utc=None, observed_monotonic=None, read_start_monotonic=None,
                    read_duration_ms=None, interval_seconds=None, previous_observed_at_utc=None,
                    tx_bytes=None, rx_bytes=None, rate_unit='Kibit/s', counter_unit='bytes',
                    observation_clock='host_counter_read_completion')

    def throughput(self, interface, identity=None, *, report=None):
        """Read real interface counters; rates span successive read completions.

        A new interface/device generation, counter reset, or failed read requires
        a fresh baseline. A first observation never represents zero traffic.
        """
        if report is None:
            report = self.traffic_metadata(interface, True)
        started = time.monotonic()
        report['read_start_monotonic'] = started
        try:
            root = Path("/sys/class/net") / interface / "statistics"
            sent, received = (int((root / name).read_text()) for name in ("tx_bytes", "rx_bytes"))
            if sent < 0 or received < 0:
                raise ValueError('Interface byte counters must be nonnegative')
        except Exception as error:
            self.previous = None
            report.update(status='error', error=str(error)[:256], observed_monotonic=time.monotonic(),
                          observed_at_utc=datetime.now(timezone.utc).isoformat())
            report['read_duration_ms'] = max(0, report['observed_monotonic']-started)*1000
            raise
        now, observed_utc = time.monotonic(), datetime.now(timezone.utc).isoformat()
        key = identity, interface
        report.update(observed_monotonic=now, observed_at_utc=observed_utc,
                      read_duration_ms=max(0, now-started)*1000, tx_bytes=sent, rx_bytes=received)
        result = {"throughput_upload_kb": sent / 1024, "throughput_download_kb": received / 1024}
        if self.previous and self.previous[0] == key:
            _, before, old_sent, old_received, before_utc = self.previous
            elapsed = now - before
            report.update(interval_seconds=elapsed, previous_observed_at_utc=before_utc)
            if elapsed > 0 and sent >= old_sent and received >= old_received:
                report['status'] = 'ok'
                result.update(throughput_upload_speed_kbps=(sent-old_sent)*8/1024/elapsed,
                              throughput_download_speed_kbps=(received-old_received)*8/1024/elapsed)
            elif sent < old_sent or received < old_received:
                report['status'] = 'counter_reset'
            else:
                report['error'] = 'A positive counter observation interval is not yet available'
        self.previous = key, now, sent, received, observed_utc
        return result

    def attempt(self, sink, action):
        try:
            action()
            return True
        except Exception as error:
            self.log.warning("Telemetry %s failed: %s", sink, error)
            if sink != "throughput":
                with self._lock:
                    self._last_persistence_error = str(error)
            return False

    def csv_row(self, path, columns, data):
        """Validate a file header once, then reuse its append handle and writer."""
        columns = tuple(columns)
        cached = self._csv_files.get(path)
        if cached is None:
            exists = path.exists() and path.stat().st_size > 0
            known = self._validated.get(path)
            if known is False or (known is not None and known != columns):
                raise ValueError(f"CSV schema differs in {path}; use a new telemetry directory")
            if exists and known is None:
                with path.open(newline="") as stream:
                    if next(csv.reader(stream), []) != list(columns):
                        self._cache_header(path, False)
                        raise ValueError(f"CSV schema differs in {path}; use a new telemetry directory")
            stream = path.open("a", newline="")
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            try:
                if not exists:
                    writer.writeheader()
            except Exception:
                stream.close()
                raise
            cached = stream, writer, columns
            self._csv_files[path] = cached
            self._cache_header(path, columns)
            while len(self._csv_files) > self.MAX_CSV_HANDLES:
                _, (old_stream, _, _) = self._csv_files.popitem(last=False)
                old_stream.close()
        elif cached[2] != columns:
            raise ValueError(f"CSV schema differs in {path}; use a new telemetry directory")
        self._csv_files.move_to_end(path)
        cached[1].writerow({key: json.dumps(value, separators=(',', ':')) if isinstance(value, (dict, list)) else value
                           for key, value in data.items()})

    def _cache_header(self, path, columns):
        self._validated[path] = columns
        self._validated.move_to_end(path)
        while len(self._validated) > self.MAX_CSV_SCHEMAS:
            self._validated.popitem(last=False)

    @staticmethod
    def json_file(path, data):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.replace(path)

    def _flush(self, close=False):
        started = time.monotonic()
        with self._lock:
            self._flushing = True
        failed = False
        for stream, _, _ in tuple(self._csv_files.values()):
            try:
                stream.flush()
                if close:
                    stream.close()
            except Exception as error:
                failed = True
                self.log.warning("Telemetry CSV flush failed: %s", error)
                with self._lock:
                    self._last_persistence_error = str(error)
        if close:
            self._csv_files.clear()
        self._last_flush = time.monotonic()
        with self._lock:
            self._flush_durations.append((self._last_flush-started)*1000)
            self._flushing = False
        if failed:
            with self._lock:
                self._counts["flush_failed"] += 1

    def persist(self, payload):
        """Storage hook called only by the writer; it never reads the modem."""
        identifier = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(
            payload["static"].get("dms_value_imei") or payload["device_identity"]))
        directory = Path(self.settings.directory) / ("modem_" + identifier)
        day = payload["timing"]["acquired_start_utc"][:10].replace("-", "")
        if day != self._rotation_day:
            self._flush(close=True)
            self._validated.clear()
            self._static_last.clear()
            self._rotation_day = day
        directory.mkdir(parents=True, exist_ok=True)
        clean = True
        if self.settings.csv:
            static_path = directory / f"static_stats_{day}.csv"
            signature = json.dumps(payload["static"], sort_keys=True, default=str)
            if self._static_last.get(static_path) != signature:
                saved = self.attempt("static CSV", lambda: self.csv_row(static_path, fields(ListStaticMetrics), payload["static"]))
                clean = saved and clean
                if saved:
                    self._static_last[static_path] = signature
                    self._static_last.move_to_end(static_path)
                    while len(self._static_last) > self.MAX_STATIC_IDENTITIES:
                        self._static_last.popitem(last=False)
            row = dict(self._csv_dynamic(payload), sample_id=payload["log_id"], **{
                key: payload["timing"][key] for key in TIMING_COLUMNS if key != "sample_id"})
            clean = self.attempt("dynamic CSV", lambda: self.csv_row(
                directory / f"dynamic_stats_v2_{day}.csv", TIMING_COLUMNS + fields(ListDynamicMetrics), row)) and clean
        if self.settings.json:
            clean = self.attempt("JSON", lambda: self.json_file(directory / "stats.json", payload)) and clean
        return clean

    @staticmethod
    def _csv_dynamic(payload):
        """Use equivalent legacy position columns only in this CSV row copy.

        JSON retains explicit gnss_* names/source. The deprecated GPSd `alt`
        column has no unambiguous datum, so QMI MSL altitude stays in the GPS
        metadata JSON. Circular uncertainty is not an epx/epy axis error.
        """
        values = dict(payload['dynamic'])
        gps = payload.get('collection', {}).get('gps') or {}
        if gps.get('source') != 'qmi_loc':
            return values
        mapping = {'gpsd_tpv_lat': 'gnss_latitude', 'gpsd_tpv_lon': 'gnss_longitude',
                   'gpsd_tpv_althae': 'gnss_altitude_ellipsoid_m',
                   'gpsd_tpv_speed': 'gnss_horizontal_speed_mps', 'gpsd_tpv_datetime': 'gnss_timestamp_utc'}
        fixed = gps.get('status') == 'fixed'
        for target, source in mapping.items():
            values[target] = values.get(source) if fixed else None
        values['gpsd_tpv_timestamp'] = None
        if isinstance(values['gpsd_tpv_datetime'], str):
            try:
                source_time = datetime.fromisoformat(values['gpsd_tpv_datetime'].replace('Z', '+00:00'))
                if source_time.tzinfo is not None:
                    values['gpsd_tpv_timestamp'] = source_time.timestamp()
            except (ValueError, OverflowError, OSError):
                pass
        return values

    def _write_loop(self):
        try:
            while not self._closing.is_set() or not self.persistence.empty():
                remaining = max(0.01, self._flush_interval-(time.monotonic()-self._last_flush))
                try:
                    payload = self.persistence.get(timeout=min(remaining, 0.1))
                except Empty:
                    self._report_drops("persistence_dropped")
                    if time.monotonic()-self._last_flush >= self._flush_interval:
                        self._flush()
                    continue
                with self._lock:
                    self._inflight = True
                clean = False
                started = time.monotonic()
                try:
                    clean = self.persist(payload)
                except Exception as error:
                    self.log.warning("Telemetry storage failed: %s", error)
                    with self._lock:
                        self._last_persistence_error = str(error)
                finally:
                    ended = time.monotonic()
                    with self._lock:
                        self._last_write_duration_ms = max(0, ended-started)*1000
                        self._last_write_sample_id = payload['log_id']
                        self._last_write_success = bool(clean)
                        self._write_durations.append(self._last_write_duration_ms)
                        self._last_write_latency_ms = max(0, ended-payload['timing']['acquired_end_monotonic'])*1000
                        self._inflight = False
                        self._pending.pop(payload["log_id"], None)
                        self._counts["persisted" if clean else "persistence_failed"] += 1
                        if clean:
                            self._last_persisted_sample_id = payload["log_id"]
                    self.persistence.task_done()
                self._report_drops("persistence_dropped")
                if time.monotonic()-self._last_flush >= self._flush_interval:
                    self._flush()
        finally:
            self._flush(close=True)

    @staticmethod
    def udp(host, port, data):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.settimeout(1)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            connection.sendto(json.dumps(data).encode(), (host, port))

    def send_udp(self):
        while not self._closing.is_set() or not self.outgoing.empty():
            try:
                payload = self.outgoing.get(timeout=0.1)
            except Empty:
                continue
            with self._lock:
                self._udp_inflight = True
            clean = True
            for host, port, data in (
                (self.settings.udp_host, self.settings.udp_port, payload),
                (self.settings.collector_host, self.settings.collector_port,
                 {"static": payload["static"], "dynamic": payload["dynamic"]}),
            ):
                if host:
                    try:
                        self.udp(host, port, data)
                    except Exception as error:
                        clean = False
                        self.log.warning("Telemetry UDP failed: %s", error)
            with self._lock:
                self._udp_pending.pop(payload["log_id"], None)
                self._udp_inflight = False
                self._counts["udp_sent" if clean else "udp_failed"] += 1
            self.outgoing.task_done()
            self._report_drops("udp_dropped")

    def _report_drops(self, counter):
        # A slow logging handler also belongs outside the acquisition path.
        with self._lock:
            count = self._counts[counter]
            changed = count != self._reported_drops[counter]
            self._reported_drops[counter] = count
        if changed:
            self.log.warning("Telemetry %s=%s; bounded worker queue is full", counter, count)

    def _enqueue(self, queue, pending, payload, dropped):
        with self._lock:
            try:
                if self._closing.is_set():
                    raise Full
                queue.put_nowait(payload)
                pending[payload["log_id"]] = time.monotonic()
            except Full:
                self._counts[dropped] += 1

    def sample(self, backend, device, *, scheduled_monotonic=None, scheduled_at_utc=None, skipped_intervals=0):
        """Acquire once and offer a snapshot without waiting for file/network sinks."""
        with self._lock:
            if not self._accepting:
                return None
        started = time.monotonic()
        start_utc = datetime.now(timezone.utc)
        scheduled = started if scheduled_monotonic is None else scheduled_monotonic
        if scheduled_at_utc is None:
            scheduled_at_utc = (start_utc + timedelta(seconds=scheduled-started)).isoformat()
        elif isinstance(scheduled_at_utc, datetime):
            scheduled_at_utc = scheduled_at_utc.isoformat()
        static, dynamic = backend.metrics()
        if device is None:
            device = backend.device
        window = getattr(backend, 'acquisition_window', None)
        if isinstance(window, dict):
            started = window['started']
            start_utc = datetime.fromisoformat(window['start_utc'])
        static = dict.fromkeys(fields(ListStaticMetrics)) | static
        dynamic = dict.fromkeys(fields(ListDynamicMetrics)) | dynamic
        # These values belong only to the host interface counters. Never reuse
        # QMI channel capability/rate fields as measured application traffic.
        dynamic.update(dict.fromkeys(TRAFFIC_FIELDS))
        traffic = self.traffic_metadata(device.interface, self.settings.throughput)
        if self.settings.throughput:
            identity = (device.identity, getattr(backend, 'generation', None))
            try:
                dynamic.update(self.throughput(device.interface, identity, report=traffic))
            except Exception:
                # Per-sample status carries the failure without synchronous
                # logging/file I/O in the acquisition path.
                pass
        else:
            self.previous = None
        ended = time.monotonic()
        end_utc = datetime.now(timezone.utc)
        timing = dict(scheduled_at_utc=scheduled_at_utc, acquired_start_utc=start_utc.isoformat(),
                      acquired_end_utc=end_utc.isoformat(), acquisition_duration_ms=max(0, ended-started)*1000,
                      schedule_lag_ms=max(0, started-scheduled)*1000, skipped_intervals=skipped_intervals,
                      scheduled_monotonic=scheduled, acquired_start_monotonic=started, acquired_end_monotonic=ended)
        gps_source = getattr(self.settings, 'gps_source', 'gpsd')
        gps_report = {}
        if self.settings.gps and gps_source == 'gpsd':
            values, gps_report = self._gps_snapshot(ended, end_utc)
            dynamic.update(values)
            timing["gps"] = gps_report
        report = getattr(backend, "collection_report", {})
        if callable(report):
            report = report()
        collection = deepcopy(dict(report)) if isinstance(report, Mapping) else {}
        if not self.settings.gps:
            collection['gps'] = dict(enabled=False, source='gpsd' if gps_source == 'gpsd' else 'qmi_loc',
                                     status='disabled', error=None)
        elif gps_source == 'gpsd':
            collection['gps'] = self._gps_status(gps_report)
        else:
            collection.setdefault('gps', dict(enabled=True, source='qmi_loc', status='unavailable',
                                              error='The modem has not reported a GNSS observation'))
        collection['interface_traffic'] = traffic
        if not isinstance(collection.get('units'), dict):
            collection['units'] = {}
        collection['units'].update({key: 'KiB' if key.endswith('_kb') else 'Kibit/s' for key in TRAFFIC_FIELDS})
        requests = collection.get('requests', {})
        observed = {key: {name: record.get(name) for name in ('sent_at_utc', 'received_at_utc', 'status', 'duration_ms')}
                    for key, record in requests.items()} if isinstance(requests, Mapping) else {}
        for name in ('schedule', 'indications'):
            if isinstance(collection.get(name), Mapping):
                observed[name] = deepcopy(collection[name])
        if collection['gps'].get('enabled'):
            observed['gps'] = deepcopy(collection['gps'])
            if collection['gps'].get('source') == 'qmi_loc':
                observed['gps']['values'] = {key: value for key, value in dynamic.items()
                                              if key.startswith('gnss_') and value is not None}
        if traffic['enabled']:
            observed['interface_traffic'] = dict(traffic)
        before_mono, before_utc = self._previous_end or (started, start_utc)
        timing.update(run_id=self.run_id,
                      effective_interval_ms=(started-self._previous_start)*1000 if self._previous_start is not None else None,
                      wall_clock_step_ms=((end_utc-before_utc).total_seconds()-(ended-before_mono))*1000,
                      query_timing_json=json.dumps(observed, separators=(',', ':')))
        self._previous_start = started
        self._previous_end = ended, end_utc
        with self._lock:
            self.sequence += 1
            sample_id = self.sequence
            self._counts["acquired"] += 1
            self._last_acquisition_duration_ms = timing["acquisition_duration_ms"]
            self._last_schedule_lag_ms = timing["schedule_lag_ms"]
            self._durations.append(timing['acquisition_duration_ms'])
            self._lags.append(timing['schedule_lag_ms'])
        pipeline = self.stats()
        timing.update(persistence_dropped_total=pipeline['persistence_dropped'], udp_dropped_total=pipeline['udp_dropped'])
        generation = getattr(backend, 'generation', None)
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = None
        payload = dict(log_id=sample_id, start_datetime=timing["acquired_start_utc"],
                       end_datetime=timing["acquired_end_utc"], device=device.path, interface=device.interface,
                       device_identity=device.identity, schema_version=2, static=static, dynamic=dynamic,
                       device_generation=generation,
                       timing=timing, collection=collection, pipeline=pipeline)
        if self._writer:
            self._enqueue(self.persistence, self._pending, payload, "persistence_dropped")
        if self._udp_worker:
            self._enqueue(self.outgoing, self._udp_pending, payload, "udp_dropped")
        if self.publish is not None:
            try:
                self.publish(payload)
            except Exception as error:
                with self._lock:
                    self._counts["publish_failed"] += 1
                self.log.warning("Telemetry latest publisher failed: %s", error)
        return payload

    def close(self, timeout=5):
        """Stop accepting samples and drain queues within the time limit.

        A timed-out filesystem call cannot be interrupted; its daemon worker may
        finish later. False reports that drain/flush did not finish cleanly.
        The global stop Event never prevents queued storage from draining.
        """
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            self._accepting = False
            self._closing.set()
        for worker in (self._writer, self._udp_worker, self._gps_worker):
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0, deadline-time.monotonic()))
        with self._lock:
            return (not self._pending and not self._udp_pending
                    and not any(worker and worker.is_alive() for worker in (self._writer, self._udp_worker, self._gps_worker))
                    and not self._counts["persistence_failed"] and not self._counts["flush_failed"])
