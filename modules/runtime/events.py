"""Append-only diagnosis event stream; this module never imports GI.

The stream is a third-party-readable witness of meaningful transitions: a
diagnosis opens, escalates, changes or resolves; the connection reaches or
leaves ONLINE; monitoring stalls or resumes; a worker restarts. Every line is
built from codes, states, counts and timestamps only. Raw error text, IMSI,
ICCID, IMEI and SIM bytes never enter it.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import time
import uuid

from modules.runtime.diagnosis import STATE_ORDER, bounded_text

MAX_LINE_BYTES = 2048
MAX_STRING_BYTES = 256
QUEUE_SIZE = 256
SEVERITY_ORDER = {'info': 0, 'warning': 1, 'error': 2}
EVENT_NAME = 'events.jsonl'
BACKUP_NAME = 'events.jsonl.1'
_SENTINEL = object()


def _write_all(fd, data):
    """Write every byte to an append-only descriptor."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError('event write made no progress')
        view = view[written:]


def _parse_utc(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _elapsed_seconds(value, now):
    parsed = _parse_utc(value)
    if parsed is None:
        return 0
    try:
        return round(max(0.0, (now - parsed).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _bounded_strings(value, limit=MAX_STRING_BYTES):
    """Bound every string value; leave booleans, numbers and nulls alone."""
    if isinstance(value, str):
        return bounded_text(value, limit)
    if isinstance(value, dict):
        return {str(key): _bounded_strings(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded_strings(item, limit) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return bounded_text(value, limit)


class EventLog:
    """One writer thread appends JSON lines; emit() never blocks or raises."""

    def __init__(self, directory, *, profile='default', modem=None, max_bytes=1_048_576,
                 clock=time.monotonic, utcnow=None):
        self.directory = Path(directory)
        self.profile = profile
        self.modem = modem
        self.max_bytes = int(max_bytes)
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.instance = uuid.uuid4().hex
        self.path = self.directory / EVENT_NAME
        self.backup = self.directory / BACKUP_NAME
        self.queue = Queue(maxsize=QUEUE_SIZE)
        self.seq = 0
        self.dropped = 0
        self.closed = False
        self._descriptor = None
        self._size = 0
        self.directory.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._run, name='event-log', daemon=True)
        self.thread.start()

    # -- caller side ---------------------------------------------------
    def emit(self, event_type, data=None):
        """Queue one event. Never blocks and never raises."""
        if self.closed:
            return
        try:
            item = (event_type, data if isinstance(data, dict) else {},
                    self.profile, self.modem)
            self.queue.put_nowait(item)
        except Full:
            # Drop the oldest queued event so the newest one survives.
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Empty:
                pass
            self.dropped += 1
            try:
                self.queue.put_nowait(item)
            except Full:
                self.dropped += 1
        except Exception:
            # A broken queue must never reach the supervisor loop.
            pass

    def close(self, timeout=2):
        """Enqueue the sentinel, let the writer drain, then join it."""
        if not self.closed:
            self.closed = True
            try:
                self.queue.put(_SENTINEL, timeout=timeout)
            except Full:
                pass
            except Exception:
                pass
        try:
            self.thread.join(timeout)
        except Exception:
            pass

    # -- writer thread -------------------------------------------------
    def _run(self):
        while True:
            item = self.queue.get()
            try:
                if item is _SENTINEL:
                    self._close_descriptor()
                    return
                self._write(item)
            except Exception:
                # A line that cannot be persisted is lost; the supervisor keeps running.
                self._close_descriptor()
            finally:
                try:
                    self.queue.task_done()
                except Exception:
                    pass

    def _write(self, item):
        event_type, data, profile, modem = item
        seq = self.seq + 1
        payload = self._encode(seq, event_type, data, profile, modem)
        self._append(payload)
        self.seq = seq

    def _encode(self, seq, event_type, data, profile, modem):
        record = {
            'schema_version': 1,
            'seq': seq,
            'at_utc': self.utcnow().isoformat(),
            'instance': self.instance,
            'profile': bounded_text('' if profile is None else str(profile), MAX_STRING_BYTES),
            'modem': bounded_text('' if modem is None else str(modem), MAX_STRING_BYTES),
            'type': bounded_text('' if event_type is None else str(event_type), 128),
            'data': _bounded_strings(data),
        }
        raw = self._dump(record)
        if len(raw) <= MAX_LINE_BYTES:
            return raw
        # Progressively shorten string values until the line fits.
        for limit in (128, 64, 32, 16, 8, 0):
            record['data'] = _bounded_strings(record['data'], limit)
            raw = self._dump(record)
            if len(raw) <= MAX_LINE_BYTES:
                return raw
        record['data'] = {}
        record['profile'] = bounded_text(record['profile'], 64)
        record['modem'] = bounded_text(record['modem'], 64)
        record['type'] = bounded_text(record['type'], 32)
        raw = self._dump(record)
        if len(raw) <= MAX_LINE_BYTES:
            return raw
        record.update(profile='', modem='', type='event')
        return self._dump(record)

    @staticmethod
    def _dump(record):
        return (json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')

    def _open_descriptor(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.chmod(self.path, 0o644)
        except OSError:
            pass
        try:
            size = os.fstat(descriptor).st_size
        except OSError:
            size = 0
        self._descriptor, self._size = descriptor, size

    def _close_descriptor(self):
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _rotate(self):
        self._close_descriptor()
        try:
            os.replace(self.path, self.backup)
        except OSError:
            pass
        self._open_descriptor()

    def _append(self, payload):
        if self._descriptor is None:
            self._open_descriptor()
        if self._size and self._size + len(payload) > self.max_bytes:
            self._rotate()
        _write_all(self._descriptor, payload)
        # Raw descriptor writes reach the kernel; O_APPEND keeps lines whole.
        self._size += len(payload)


class EventDetector:
    """Compare one observation with the previous one and describe the changes."""

    def __init__(self, utcnow=None):
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.previous = None

    def update(self, modem_snapshot, telemetry_status, children):
        try:
            snapshot = modem_snapshot if isinstance(modem_snapshot, dict) else {}
            telemetry = telemetry_status if isinstance(telemetry_status, dict) else {}
            state = snapshot.get('state')
            observed = {
                'state': state,
                'diagnosis': self._active(snapshot.get('diagnosis')),
                'stall': self._stall(telemetry.get('stall')),
                'restarts': self._restarts(children),
            }
            previous = self.previous
            self.previous = observed
            if previous is None:
                # The first observation records baselines without emitting.
                return []
            events = []
            events.extend(self._diagnosis_events(previous, observed['diagnosis'], state))
            events.extend(self._connection_events(previous, state))
            events.extend(self._stall_events(previous, observed['stall']))
            events.extend(self._restart_events(previous, observed['restarts']))
            return events
        except Exception:
            return []

    @staticmethod
    def _active(value):
        if not isinstance(value, dict) or not value.get('code'):
            return None
        if value.get('active') is False:
            return None
        return {
            'code': value.get('code'),
            'severity': value.get('severity'),
            'step': value.get('step'),
            'operation': value.get('operation'),
            'attempts': value.get('attempts'),
            'since_utc': value.get('since_utc'),
            'failing_since_utc': value.get('failing_since_utc'),
        }

    @staticmethod
    def _stall(value):
        if not isinstance(value, dict):
            return None
        return {'since_utc': value.get('since_utc'),
                'reason_code': value.get('reason_code'),
                'skipped': value.get('skipped')}

    @staticmethod
    def _restarts(children):
        result = {}
        if not isinstance(children, dict):
            return result
        for role, child in children.items():
            value = child.get('restarts') if isinstance(child, dict) else getattr(child, 'restarts', None)
            if isinstance(value, int) and not isinstance(value, bool):
                result[str(role)] = value
        return result

    @staticmethod
    def _connection_events(previous, state):
        connected = state in ('ONLINE', 'MONITOR')
        was_connected = previous.get('state') in ('ONLINE', 'MONITOR')
        if connected and not was_connected:
            return [('connection.online', {'state': state})]
        if was_connected and not connected:
            return [('connection.offline', {'state': state, 'from': previous.get('state')})]
        return []

    def _diagnosis_events(self, previous, current, state):
        old = previous.get('diagnosis')
        if current is not None:
            if old is None:
                return [('diagnosis.opened', {
                    'code': current['code'], 'severity': current['severity'],
                    'step': current['step'], 'operation': current['operation'],
                    'since_utc': current['since_utc']})]
            if current['code'] != old['code']:
                return [('diagnosis.changed', {
                    'code': current['code'], 'previous_code': old['code'],
                    'severity': current['severity']})]
            if SEVERITY_ORDER.get(current['severity'], -1) > SEVERITY_ORDER.get(old['severity'], -1):
                return [('diagnosis.escalated', {
                    'code': current['code'], 'severity': current['severity'],
                    'attempts': current['attempts'],
                    'failing_since_utc': current['failing_since_utc']})]
            return []
        if old is None:
            return []
        resolution = 'recovered'
        if state not in ('ONLINE', 'MONITOR'):
            if (STATE_ORDER.get(str(state), -1)
                    > STATE_ORDER.get(str(old.get('step')), -1)):
                resolution = 'step_passed'
        duration = _elapsed_seconds(old.get('failing_since_utc'), self.utcnow())
        return [('diagnosis.resolved', {
            'code': old['code'], 'resolution': resolution,
            'attempts': old.get('attempts'), 'duration_s': duration})]

    def _stall_events(self, previous, stall):
        old = previous.get('stall')
        if stall is not None and old is None:
            return [('telemetry.stalled', {
                'since_utc': stall.get('since_utc'),
                'reason_code': stall.get('reason_code')})]
        if stall is None and old is not None:
            return [('telemetry.resumed', {
                'stalled_seconds': _elapsed_seconds(old.get('since_utc'), self.utcnow()),
                'skipped': old.get('skipped')})]
        return []

    @staticmethod
    def _restart_events(previous, restarts):
        old = previous.get('restarts') or {}
        events = []
        for role in sorted(restarts):
            if role in old and restarts[role] > old[role]:
                events.append(('worker.restarted', {'role': role, 'restarts': restarts[role]}))
        return events
