#!/usr/bin/env python3
"""Probe one worker pair in isolation; use fleet_soak.py for the fleet parent."""
import argparse
import csv
import hashlib
from dataclasses import replace
import json
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from modules.deployment.provisioning import render_config, write_atomic
from modules.runtime.ipc import read_json
from modules.runtime.processes import ProcessManager
from modules.runtime.settings import Settings
from modules.web.auth import hash_password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=3600)
    parser.add_argument('--output', type=Path, default=Path('/tmp/qmi-platform-soak.json'))
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='qmi-platform-soak-'))
    runtime = root/'run'
    runtime.mkdir(mode=0o700)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    defaults = Settings()
    settings = replace(defaults, telemetry=replace(defaults.telemetry, directory=str(root/'captures')),
        web=replace(defaults.web, enabled=True, port=port, password_hash=hash_password('synthetic-soak-password')))
    config = root/'settings.ini'
    write_atomic(config, render_config(settings))
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    def launch(command, **kwargs):
        command[2] = 'tests.runtime.actor_fixture'
        return subprocess.Popen(command, **kwargs)
    manager = ProcessManager(config, settings, stop, runtime=runtime, popen=launch)
    # Keep the manager in the foreground: this is the process whose resource
    # use is measured, rather than a testing-only thread around the platform.
    started = time.monotonic()
    resource_history = []
    def observe():
        while not stop.wait(30):
            status = read_json(runtime/'status.json') or {}
            resource_history.append(dict(elapsed_seconds=time.monotonic()-started,
                history_size=len(status.get('history', [])),
                writer_history_size=len(status.get('pipeline_history', [])),
                pending=status.get('telemetry', {}).get('pending'),
                resources={role: {key: record.get(key) for key in ('cpu_percent', 'rss_bytes')}
                           for role, record in (status.get('resources') or {}).items()
                           if isinstance(record, dict) and role != 'supervisor'}))
    threading.Thread(target=observe, daemon=True).start()
    runtime_sources = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                       for folder in ('modules/runtime', 'modules/telemetry')
                       for path in sorted(Path(folder).glob('*.py'))}
    def finish():
        if not stop.wait(args.seconds):
            stop.set()
    threading.Thread(target=finish, daemon=True).start()
    manager.run()
    duration = time.monotonic()-started
    last = read_json(runtime/'status.json') or {}
    telemetry = read_json(runtime/'telemetry.json') or {}
    history, pipeline = last.get('history', []), last.get('pipeline_history', [])
    rows, ids, intervals, lags = 0, [], [], []
    for path in sorted((root/'captures').rglob('dynamic_stats_v2_*.csv')):
        with path.open(newline='') as stream:
            for row in csv.DictReader(stream):
                rows += 1
                ids.append(int(row['sample_id']))
                if row['effective_interval_ms']:
                    intervals.append(float(row['effective_interval_ms']))
                lags.append(float(row['schedule_lag_ms']))
    completed = duration >= args.seconds
    history_ids = [point['sample_id'] for point in history]
    history_complete = (not history_ids or history_ids == list(range(history_ids[0], history_ids[-1]+1)))
    report = dict(duration_seconds=duration, completed=completed, synthetic=True, root=str(root),
        requested_seconds=args.seconds, history_size=len(history), writer_history_size=len(pipeline),
        stats=telemetry.get('stats'), resources=last.get('resources'),
        actor_restarts={role: child.restarts for role, child in manager.children.items()},
        acquisition_intervals_ms=[point.get('effective_interval_ms') for point in history],
        acquisition_lag_ms=[point.get('schedule_lag_ms') for point in history],
        runtime_source_sha256=runtime_sources, resource_history=resource_history,
        history_audit=dict(sequential_ids=history_complete, sample_ids=history_ids),
        csv_audit=dict(rows=rows, unique_ids=len(set(ids)),
            sequential_ids=ids == list(range(1, rows+1)),
            mean_interval_ms=sum(intervals)/len(intervals) if intervals else None,
            min_interval_ms=min(intervals) if intervals else None,
            max_interval_ms=max(intervals) if intervals else None,
            max_schedule_lag_ms=max(lags) if lags else None))
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    stats = report['stats'] or {}
    return int(not completed or not stats.get('acquired') or len(history) > 120 or len(pipeline) > 120
               or not history_complete or rows != stats.get('persisted') or not report['csv_audit']['sequential_ids']
               or stats.get('persistence_dropped', 0) or stats.get('persistence_failed', 0)
               or any(report['actor_restarts'].values()))


if __name__ == '__main__':
    raise SystemExit(main())
