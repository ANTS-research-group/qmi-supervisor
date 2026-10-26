#!/usr/bin/env python3
"""Compare fleet objectives with/without web using isolated synthetic UEs."""
import argparse
from dataclasses import replace
import itertools
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from modules.deployment.provisioning import render_config, write_atomic
from modules.device.device import Device
from modules.runtime.fleet import FleetManager
from modules.runtime.settings import Settings
from modules.telemetry.resources import ProcessSampler
from modules.web.auth import hash_password
from tests.web.static_fixture import make_export


def run_case(config, runtime, modems):
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    environment = dict(os.environ, QMI_TEST_STATIC_ROOT=str(make_export(runtime.parent/'static')))
    def launch(command, **kwargs):
        command[2] = 'tests.runtime.actor_fixture'
        return subprocess.Popen(command, env=environment, **kwargs)
    devices = [Device(f'/dev/synthetic-{index}', f'synthetic-wwan{index}', f'/sys/synthetic/{index}',
                      f'synthetic-{index}', f'Synthetic modem {index}') for index in range(modems)]
    return FleetManager(config, stop, runtime=runtime, popen=launch, discovery=lambda: devices).run()


def targets(pid, proc_root='/proc'):
    result = dict(manager=pid)
    root = Path(proc_root)
    children = (root/f'{pid}/task/{pid}/children').read_text().split()
    for child in children:
        try:
            command = (root/child/'cmdline').read_bytes().decode().split('\0')
            if '--role' in command:
                role = command[command.index('--role')+1]
                if role in {'modem', 'telemetry', 'web'}:
                    result[f'{role}:{child}'] = int(child)
        except (OSError, ValueError):
            continue
    return result


def aggregate(processes, role):
    records = [record for name, record in processes.items() if name.split(':', 1)[0] == role]
    complete = bool(records) and all(record.get('state') == 'ok' for record in records)
    def total(field):
        values = [record.get(field) for record in records]
        return sum(values) if values and all(value is not None for value in values) else None
    return dict(process_count=len(records), state='disabled' if not records else 'ok' if complete else 'partial',
                cpu_percent=total('cpu_percent'), rss_bytes=total('rss_bytes'))


def sample_processes(samplers, processes):
    # Keep one accounting baseline per actual child, rather than overwriting a
    # single "modem" slot and silently omitting peers. This is an external
    # development observer; production sampling remains in its existing worker.
    for name in set(samplers)-set(processes):
        del samplers[name]
    records = {}
    for name, pid in processes.items():
        if name not in samplers:
            samplers[name] = ProcessSampler()
        records[name] = samplers[name].sample({'supervisor': pid})['supervisor']
    return {role: aggregate(records, role) for role in ('manager', 'modem', 'telemetry', 'web')} | {'processes': records}


def summarize(samples, role):
    """Weight CPU by measured intervals; a short final interval has less weight."""
    by_process = {}
    valid = True
    for sample in samples:
        if sample[role]['process_count'] and sample[role]['state'] != 'ok':
            valid = False
        for name, record in sample['processes'].items():
            if name.split(':', 1)[0] != role:
                continue
            cpu, elapsed = record.get('cpu_percent'), record.get('cpu_interval_seconds')
            if cpu is None or elapsed is None or elapsed <= 0:
                valid = False
                continue
            work, duration = by_process.get(name, (0.0, 0.0))
            by_process[name] = work+cpu*elapsed, duration+elapsed
    rss = [point[role]['rss_bytes'] for point in samples if point[role]['rss_bytes'] is not None]
    return dict(cpu_percent_mean=sum(work/duration for work, duration in by_process.values())
                if valid and by_process else None, rss_bytes_max=max(rss) if rss else None,
                process_count_max=max(point[role]['process_count'] for point in samples))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=30, help='Measured time per case, after warmup')
    parser.add_argument('--warmup', type=float, default=5)
    parser.add_argument('--modems', type=int, default=1, help='Number of synthetic UEs (1..32)')
    parser.add_argument('--output', type=Path, default=Path('/tmp/qmi-platform-profile.json'))
    parser.add_argument('--case-config', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--case-runtime', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.modems <= 32:
        parser.error('Use 1..32 synthetic modems')
    if args.case_config:
        return run_case(args.case_config, args.case_runtime, args.modems)
    if not (5 <= args.seconds <= 86400 and 0 <= args.warmup <= 3600):
        parser.error('Use 5..86400 measured seconds and 0..3600 warmup seconds')
    root = Path(tempfile.mkdtemp(prefix='qmi-platform-profile-'))
    cases = []
    for connection, monitoring, web in itertools.product((False, True), repeat=3):
        directory = root/f'{int(connection)}-{int(monitoring)}-{int(web)}'
        directory.mkdir()
        defaults = Settings()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        settings = replace(defaults,
            modem=replace(defaults.modem, mode='connect' if connection else 'monitor'),
            telemetry=replace(defaults.telemetry, enabled=monitoring, directory=str(directory/'captures')),
            web=replace(defaults.web, enabled=web, port=port,
                        password_hash=hash_password('synthetic-profile-password')))
        config = directory/'settings.ini'
        write_atomic(config, render_config(settings))
        with (directory/'runtime.log').open('w') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--case-config', str(config), '--case-runtime', str(directory/'run'), '--modems', str(args.modems)],
                # The child starts the actual production fleet parent.
                # Pass the requested physical count to its synthetic discovery.
                stdout=log, stderr=log, start_new_session=True)
            samples = []
            try:
                time.sleep(args.warmup)
                samplers = {}
                sample_processes(samplers, targets(process.pid))
                expected = dict(manager=1, modem=args.modems if connection or monitoring else 0,
                                telemetry=args.modems if monitoring else 0, web=int(web))
                started = time.monotonic()
                while time.monotonic()-started < args.seconds:
                    time.sleep(max(0, min(5, args.seconds-(time.monotonic()-started))))
                    if process.poll() is not None:
                        raise RuntimeError(f'Profile manager exited unexpectedly; see {directory}/runtime.log')
                    sample = sample_processes(samplers, targets(process.pid))
                    if any(sample[role]['process_count'] != count for role, count in expected.items()):
                        raise RuntimeError(f'Unexpected child process counts; see {directory}/runtime.log')
                    samples.append(sample)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        summary = {}
        for role in ('manager', 'modem', 'telemetry', 'web'):
            summary[role] = summarize(samples, role)
        case = dict(connection=connection, monitoring=monitoring, web=web, modem_count=args.modems,
                    expected_process_counts=expected, summary=summary, samples=samples)
        cases.append(case)
        print(json.dumps({key: value for key, value in case.items() if key != 'samples'}), flush=True)
    report = dict(synthetic=True, manager='FleetManager', modem_count=args.modems,
                  seconds_per_case=args.seconds, warmup_seconds=args.warmup,
                  cpu_semantics='100% = one CPU core; external /proc observer; no radio work in synthetic modem',
                  rss_semantics='Per-role sum of process RSS; shared pages may be counted in more than one process',
                  web_workload='Server enabled without HTTP clients; frontend rendering runs in the browser',
                  root=str(root), cases=cases)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
