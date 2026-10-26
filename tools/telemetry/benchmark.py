#!/usr/bin/env python3
"""Reproduce sampling drift without a modem, root privileges or network access.

Default: deterministic virtual-clock models, not a hardware measurement.
--realtime: exercise the actual Supervisor scheduler and Telemetry CSV worker
with a fake backend and optional artificial query/storage delays. All files
are temporary. Quantiles retain at most 2,048 recent timings.
"""
import argparse
from collections import deque
import csv
from dataclasses import replace
import json
import math
from pathlib import Path
import resource
import sys
import tempfile
import threading
import time
import tracemalloc


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class Timings:
    def __init__(self):
        self.values = deque(maxlen=2048)
        self.count = 0
        self.total = self.maximum = 0.0

    def add(self, value):
        self.values.append(value)
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)

    def report(self):
        values = sorted(self.values)
        def percentile(fraction):
            return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else 0.0
        return {"count": self.count, "mean_ms": self.total / max(1, self.count) * 1000,
                "p50_ms": percentile(0.5) * 1000, "p95_ms": percentile(0.95) * 1000,
                "p99_ms": percentile(0.99) * 1000, "max_ms": self.maximum * 1000,
                "quantile_window": len(values)}


def simulate(args, model):
    """Constant-cost discrete-event model, including a single bounded writer."""
    now = deadline = 0.0
    previous_start = None
    periods, lateness, queries, writes, ages = (Timings() for _ in range(5))
    pending = deque()
    writer_end = writer_acquired = None
    persisted = dropped = skipped = max_pending = 0

    def advance_writer(until):
        nonlocal writer_end, writer_acquired, persisted
        while writer_end is not None and writer_end <= until:
            persisted += 1
            writes.add(args.write_delay)
            ages.add(writer_end - writer_acquired)
            if pending:
                writer_acquired = pending.popleft()
                writer_end += args.write_delay
            else:
                writer_end = writer_acquired = None

    for index in range(args.samples):
        if model == "deadline_bounded":
            now = max(now, deadline)
            scheduled = deadline
        else:
            scheduled = index * args.interval
        start = now
        lateness.add(max(0.0, start - scheduled))
        if previous_start is not None:
            periods.add(start - previous_start)
        previous_start = start
        now += args.query_delay
        queries.add(args.query_delay)
        if model == "deadline_bounded":
            advance_writer(now)
            if writer_end is None:
                writer_acquired, writer_end = now, now + args.write_delay
            else:
                if len(pending) >= args.queue_size:
                    dropped += 1
                else:
                    pending.append(now)
            max_pending = max(max_pending, len(pending) + (writer_end is not None))
            deadline += args.interval
            if index + 1 < args.samples and deadline < now:
                missed = math.floor((now - deadline) / args.interval) + 1
                skipped += missed
                deadline += missed * args.interval
        else:
            now += args.write_delay
            writes.add(args.write_delay)
            ages.add(args.write_delay)
            persisted += 1
            if model == "original_compensated":
                now = max(start + args.interval, now)
            else:
                now += args.interval
    acquisition_end = now
    if model == "deadline_bounded":
        while writer_end is not None:
            advance_writer(writer_end)
    return {"model": model, "acquired": args.samples, "persisted": persisted,
            "persistence_dropped": dropped, "skipped_intervals": skipped,
            "max_pending_including_inflight": max_pending,
            "last_start_seconds": previous_start,
            "last_start_minus_sample_index_grid_ms":
                (previous_start - (args.samples - 1) * args.interval) * 1000,
            "acquisition_end_seconds": acquisition_end,
            "start_lateness": lateness.report(), "start_period": periods.report(),
            "query_cost": queries.report(), "write_cost": writes.report(),
            "acquired_to_persisted": ages.report()}


def realtime(args):
    """Production scheduler/worker; fake backend and artificial sink latency."""
    from modules.device.device import Device
    from modules.runtime.settings import Settings
    from modules.runtime.supervisor import Supervisor
    from modules.telemetry.collector import Telemetry

    class Backend:
        def metrics(self, *unused, **kwargs):
            time.sleep(args.query_delay)
            return {"dms_value_imei": "benchmark"}, {"wds_value_tx_bytes_ok": 0}

    class Probe(Telemetry):
        def __init__(self, *values, **kwargs):
            self.starts, self.queries, self.writes = Timings(), Timings(), Timings()
            self.lateness = Timings()
            self.count = 0
            self.previous_start = None
            super().__init__(*values, **kwargs)

        def sample(self, *values, **kwargs):
            started = time.monotonic()
            self.lateness.add(max(0.0, started - kwargs.get("scheduled_monotonic", started)))
            if self.previous_start is not None:
                self.starts.add(started - self.previous_start)
            self.previous_start = started
            result = super().sample(*values, **kwargs)
            self.queries.add(time.monotonic() - started)
            self.count += 1
            return result

        def persist(self, payload):
            started = time.monotonic()
            time.sleep(args.write_delay)
            result = super().persist(payload)
            self.writes.add(time.monotonic() - started)
            return result

    with tempfile.TemporaryDirectory(prefix="qmi-timing-") as directory:
        defaults = Settings()
        settings = replace(defaults, telemetry=replace(
            defaults.telemetry, interval=args.interval, directory=directory,
            enabled=True, csv=True, json=False, throughput=False, gps=False,
            udp_host="", collector_host="", queue_size=args.queue_size,
            profile="full", collection_budget=min(defaults.telemetry.collection_budget, args.interval * 0.8)))
        stop = threading.Event()
        tracemalloc.start()
        cpu_start, started = time.process_time(), time.monotonic()
        telemetry = Probe(settings.telemetry, stop)
        supervisor = Supervisor(settings, Backend(), telemetry, stop)
        supervisor.device = Device("/dev/benchmark", "benchmark0", "/tmp", "benchmark", "fake")
        supervisor.next_sample = started
        memory_points = []
        checkpoint = max(1, args.samples // 4)
        last_checkpoint = -1
        max_pending = 0
        try:
            while telemetry.count < args.samples:
                remaining = supervisor.next_sample - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                supervisor.sample()
                max_pending = max(max_pending, telemetry.stats().get("pending", 0))
                if telemetry.count % checkpoint == 0 and telemetry.count != last_checkpoint:
                    last_checkpoint = telemetry.count
                    memory_points.append({"samples": telemetry.count,
                                          "traced_bytes": tracemalloc.get_traced_memory()[0]})
        finally:
            drained = telemetry.close(timeout=max(5, (args.queue_size + 1) * args.write_delay * 2 + 1))
            stop.set()
        elapsed = time.monotonic() - started
        traced_current, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {"mode": "realtime_fake_backend", "elapsed_seconds": elapsed,
                "sampling_skipped_intervals": supervisor.skipped_intervals,
                "cpu_seconds": time.process_time() - cpu_start,
                "peak_process_rss_kib_linux": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "python_traced_current_bytes": traced_current, "python_traced_peak_bytes": traced_peak,
                "memory_checkpoints": memory_points, "drained": drained,
                "max_observed_pending_including_inflight": max_pending,
                "start_period": telemetry.starts.report(), "start_lateness": telemetry.lateness.report(),
                "sample_call_cost": telemetry.queries.report(), "write_cost": telemetry.writes.report(),
                "telemetry": telemetry.stats()}


def csv_size_probe():
    """Measure the former header-read/append pattern, using cached temporary CSVs."""
    results = []
    columns = ["metric_" + str(index) for index in range(100)]
    row = {key: 0 for key in columns}
    with tempfile.TemporaryDirectory(prefix="qmi-csv-size-") as directory:
        for initial_rows in (100, 100000):
            path = Path(directory) / (str(initial_rows) + ".csv")
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                # Fixed-size blocks: creating a larger test file need not retain it in RAM.
                block = (",".join("0" for _ in columns) + "\n") * 100
                for _ in range(initial_rows // 100):
                    stream.write(block)
            initial_bytes = path.stat().st_size
            timings = Timings()
            for _ in range(500):
                started = time.perf_counter()
                with path.open(newline="") as stream:
                    if next(csv.reader(stream), []) != columns:
                        raise RuntimeError("Unexpected CSV schema")
                with path.open("a", newline="") as stream:
                    csv.DictWriter(stream, fieldnames=columns).writerow(row)
                timings.add(time.perf_counter() - started)
            results.append({"initial_rows": initial_rows, "initial_bytes": initial_bytes,
                            "header_check_and_append": timings.report()})
    return {"mode": "cached_temporary_csv_no_fsync", "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--interval", type=float, default=1, help="target interval, seconds")
    parser.add_argument("--query-delay", type=float, default=0.2, help="fake query cost, seconds")
    parser.add_argument("--write-delay", type=float, default=0.4, help="fake sink cost, seconds")
    parser.add_argument("--queue-size", type=int, default=8, help="pending writes, excluding in-flight write")
    parser.add_argument("--realtime", action="store_true", help="exercise actual scheduler and CSV worker")
    parser.add_argument("--csv-size-check", action="store_true", help="also benchmark header-check/append at two file sizes")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.samples < 2 or not 1 <= args.queue_size <= 4096:
        parser.error("samples must be >= 2 and queue-size between 1 and 4096")
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("interval must be positive and finite")
    if any(not math.isfinite(value) or value < 0 for value in (args.query_delay, args.write_delay)):
        parser.error("query-delay and write-delay must be nonnegative and finite")
    report = {"parameters": vars(args), "hardware": "none; no radio precision measurement"}
    if args.realtime:
        report["result"] = realtime(args)
    else:
        report["mode"] = "virtual_clock_simulation_constant_costs"
        report["results"] = [simulate(args, model) for model in
                             ("original_compensated", "previous_finish_plus_interval", "deadline_bounded")]
    if args.csv_size_check:
        report["csv_size_probe"] = csv_size_probe()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("No modem or network. " + ("Real clock, synthetic backend." if args.realtime else
                                       "Deterministic virtual-clock simulation."))
        if args.realtime:
            result = report["result"]
            print(json.dumps(result, indent=2))
        else:
            print(f"{'model':31} {'mean period ms':>17} {'maximum lag ms':>14} {'skipped':>9} {'dropped':>12} {'maximum pending':>15}")
            for result in report["results"]:
                print(f"{result['model']:31} {result['start_period']['mean_ms']:17.3f} "
                      f"{result['start_lateness']['max_ms']:14.3f} {result['skipped_intervals']:9} "
                      f"{result['persistence_dropped']:12} {result['max_pending_including_inflight']:15}")
        if args.csv_size_check:
            print("Temporary CSV, warm cache, no fsync (not a Mochabin disk measurement):")
            for result in report["csv_size_probe"]["results"]:
                timing = result["header_check_and_append"]
                print(f"  {result['initial_bytes']:>10} bytes: p50={timing['p50_ms']:.4f} ms, "
                      f"p95={timing['p95_ms']:.4f} ms, p99={timing['p99_ms']:.4f} ms")


if __name__ == "__main__":
    main()
