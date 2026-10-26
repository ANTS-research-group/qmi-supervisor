#!/usr/bin/env python3
"""Audit simultaneous synthetic UEs through the production fleet, IPC and writers."""
import argparse
import csv
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from modules.deployment.provisioning import write_atomic
from modules.device.device import Device
from modules.runtime.fleet import FleetManager
from modules.runtime.fleet_config import Configuration
from modules.runtime.ipc import read_json
from modules.runtime.settings import Settings
from modules.web.auth import hash_password
from tests.web.static_fixture import make_export


def audit_csv(directory):
    identifiers, intervals, lags = [], [], []
    startup_skips = steady_skips = 0
    for path in sorted(Path(directory).glob('modem_*/dynamic_stats_v2_*.csv')):
        with path.open(newline='') as stream:
            for row in csv.DictReader(stream):
                identifiers.append(int(row['sample_id']))
                skipped = int(row.get('skipped_intervals') or 0)
                if len(identifiers) == 1:
                    startup_skips += skipped
                else:
                    steady_skips += skipped
                if row.get('effective_interval_ms'):
                    intervals.append(float(row['effective_interval_ms']))
                lags.append(float(row['schedule_lag_ms']))
    return dict(rows=len(identifiers), sequential_ids=identifiers == list(range(1, len(identifiers)+1)),
                mean_interval_ms=sum(intervals)/len(intervals) if intervals else None,
                min_interval_ms=min(intervals) if intervals else None,
                max_interval_ms=max(intervals) if intervals else None,
                startup_skipped_intervals=startup_skips, steady_skipped_intervals=steady_skips,
                max_schedule_lag_ms=max(lags) if lags else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=3600)
    parser.add_argument('--modems', type=int, default=2)
    parser.add_argument('--output', type=Path, default=Path('/tmp/qmi-fleet-soak.json'))
    args = parser.parse_args()
    if not 1 <= args.modems <= 32 or not 1 <= args.seconds <= 86400:
        parser.error('Use 1..32 synthetic modems and 1..86400 seconds')
    root = Path(tempfile.mkdtemp(prefix='qmi-fleet-soak-'))
    runtime, config = root/'run', root/'settings.ini'
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    defaults = Settings()
    password = 'synthetic-fleet-soak-password'
    settings = replace(defaults, telemetry=replace(defaults.telemetry, directory=str(root/'captures')),
                       web=replace(defaults.web, enabled=True, port=port, password_hash=hash_password(password)))
    write_atomic(config, Configuration(settings, {}).render())
    environment = dict(os.environ, QMI_TEST_STATIC_ROOT=str(make_export(root/'static')))
    devices = [Device(f'/dev/synthetic-{index}', f'synthetic-wwan{index}', f'/sys/synthetic/{index}',
                      f'synthetic-{index}', f'Synthetic modem {index}') for index in range(args.modems)]
    def launch(command, **kwargs):
        command[2] = 'tests.runtime.actor_fixture'
        return subprocess.Popen(command, env=environment, **kwargs)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    manager = FleetManager(config, stop, runtime=runtime, popen=launch, discovery=lambda: devices)
    started = time.monotonic()
    observations_path = root/'observations.jsonl'
    observer_error = None
    api_observations, api_errors = {}, []
    api_error_count = 0
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for folder in ('modules/runtime', 'modules/telemetry', 'modules/network', 'modules/device')
              for path in sorted(Path(folder).glob('*.py'))}
    def observe():
        nonlocal api_error_count, observer_error
        cookie = None
        def login():
            body = json.dumps({'username': settings.web.username, 'password': password}).encode()
            request = Request(f'http://127.0.0.1:{port}/api/login', data=body,
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=2) as response:
                return response.headers['Set-Cookie'].split(';', 1)[0]
        def scoped_snapshot(identifier):
            nonlocal cookie
            url = f'http://127.0.0.1:{port}/api/status?modem={quote(identifier)}'
            for attempt in (1, 2):
                if cookie is None:
                    cookie = login()
                try:
                    with urlopen(Request(url, headers={'Cookie': cookie}), timeout=1) as response:
                        return json.load(response).get('snapshot') or {}
                except HTTPError as error:
                    # The session lifetime can be shorter than a long soak.
                    if error.code != 401 or attempt == 2:
                        raise
                    cookie = None
            raise ValueError('Web session could not be established')
        while not stop.wait(10):
            snapshot = read_json(runtime/'status.json') or {}
            profiles = {}
            for path in sorted((runtime/'modems').glob('*/status.json')):
                value = read_json(path) or {}
                profiles[path.parent.name] = dict(history_size=len(value.get('history', [])),
                    writer_history_size=len(value.get('pipeline_history', [])),
                    pending=value.get('telemetry', {}).get('pending'), resources=value.get('resources'))
                if stop.is_set():
                    break
                identifier = path.parent.name
                try:
                    scoped = scoped_snapshot(identifier)
                    sample = scoped.get('sample') or {}
                    if (scoped.get('selected_modem') != identifier or not sample
                            or sample.get('device') != (scoped.get('device') or {}).get('path')):
                        raise ValueError('Scoped API does not identify the acquired modem')
                    expected_path = manager.members[identifier].settings.modem.device
                    if sample.get('device') != expected_path:
                        raise ValueError('Scoped API returned another modem sample')
                    points = scoped.get('history') or []
                    identifiers = [point['sample_id'] for point in points]
                    if (len(points) > settings.web.history_size or not identifiers
                            or identifiers != list(range(identifiers[0], identifiers[-1]+1))
                            or any(point.get('run_id') != sample.get('timing', {}).get('run_id') for point in points)):
                        raise ValueError('Scoped API history has missing or mixed-run acquisitions')
                    api_observations[identifier] = api_observations.get(identifier, 0)+1
                except (OSError, ValueError, KeyError) as error:
                    if not stop.is_set():
                        api_error_count += 1
                        if len(api_errors) < 100:
                            api_errors.append(dict(profile=identifier, error=str(error)))
            # Do not retain an unbounded observer list in the very parent
            # whose RSS is being measured. Load the report only after capture
            # and all measurement threads have stopped.
            try:
                with observations_path.open('a') as destination:
                    destination.write(json.dumps(dict(elapsed_seconds=time.monotonic()-started,
                        resources=snapshot.get('resources'), modems=profiles), separators=(',', ':'))+'\n')
            except OSError as error:
                observer_error = str(error)
                stop.set()
                return
    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    def finish():
        if not stop.wait(args.seconds):
            stop.set()
    threading.Thread(target=finish, daemon=True).start()
    result = manager.run()
    observer.join(4)
    if observer.is_alive():
        raise RuntimeError('The resource observer did not stop; the capture audit is incomplete')
    duration = time.monotonic()-started
    observations = ([json.loads(line) for line in observations_path.read_text().splitlines()]
                    if observations_path.exists() else [])
    profiles = {}
    for identifier, member in sorted(manager.members.items()):
        telemetry = read_json(member.runtime/'telemetry.json') or {}
        snapshot = read_json(member.runtime/'status.json') or {}
        history = snapshot.get('history', [])
        identifiers = [point['sample_id'] for point in history]
        stats = telemetry.get('stats') or {}
        profiles[identifier] = dict(stats=stats, history_size=len(history),
            writer_history_size=len(snapshot.get('pipeline_history', [])),
            history_audit=dict(sequential_ids=not identifiers or identifiers == list(range(identifiers[0], identifiers[-1]+1)),
                               sample_ids=identifiers, publication_lag_samples=stats.get('acquired', 0)-(identifiers[-1] if identifiers else 0)),
            csv_audit=audit_csv(member.settings.telemetry.directory),
            actor_restarts={role: child.restarts for role, child in member.children.items() if role != 'web'})
    success = (result == 0 and duration >= args.seconds and len(profiles) == args.modems and
               manager.web.restarts == 0 and observer_error is None)
    success = success and not api_error_count and (args.seconds < 11 or all(api_observations.get(key) for key in profiles))
    for value in profiles.values():
        stats = value['stats']
        success = success and bool(stats.get('acquired')) and value['history_audit']['sequential_ids'] \
            and value['history_size'] <= 120 and value['writer_history_size'] <= 120 \
            and value['csv_audit']['sequential_ids'] and value['csv_audit']['rows'] == stats.get('persisted') \
            and not value['csv_audit']['steady_skipped_intervals'] \
            and stats.get('acquired') == stats.get('persisted') \
            and not stats.get('persistence_dropped') and not stats.get('persistence_failed') \
            and not any(value['actor_restarts'].values())
    report = dict(completed=bool(success), synthetic=True, duration_seconds=duration, requested_seconds=args.seconds,
        modem_count=args.modems, root=str(root), modems=profiles, observations=observations,
        runtime_source_sha256=hashes, web_restarts=manager.web.restarts,
        observation_storage='streamed-jsonl; materialised after capture stopped', observer_error=observer_error,
        observer_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        api_observations=api_observations, api_error_count=api_error_count, api_errors=api_errors)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(completed=bool(success), duration_seconds=duration,
        modems={key: dict(acquired=value['stats'].get('acquired'), persisted=value['stats'].get('persisted'),
                          csv=value['csv_audit'], history_size=value['history_size'],
                          sequential_history=value['history_audit']['sequential_ids'])
                for key, value in profiles.items()}), indent=2))
    return int(not success)


if __name__ == '__main__':
    raise SystemExit(main())
