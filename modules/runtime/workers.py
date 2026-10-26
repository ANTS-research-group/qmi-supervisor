"""Executable actors for the single-service process manager."""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import logging
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

from modules.device.device import Device
from modules.runtime.ipc import AcquisitionServer, Heartbeat, request, revision
from modules.runtime.settings import load_settings

LOG = logging.getLogger(__name__)


def modem_worker(path, runtime, worker_id, settings, stop, *, discovery=None, device_override=None,
                 owner_lock_fd=None, owner_lock_path=None, state_path=None, device_lock_dir=None,
                 usb_path=None, peer_routes=None, recover_owned_connection=False, retire_relocated=False):
    if retire_relocated:
        from modules.runtime.relocation import relocation_worker
        return relocation_worker(runtime, worker_id, settings, stop, owner_lock_fd=owner_lock_fd,
            owner_lock_path=owner_lock_path, state_path=state_path, device_lock_dir=device_lock_dir, usb_path=usb_path)
    # Only this role may import GI or allocate QMI clients.
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import GLib, Qmi
    from modules.device.backend import QmiBackend
    from modules.network.manager import Network
    from modules.runtime.supervisor import Supervisor, State
    from modules.device.device import discover

    class Backend(QmiBackend):
        actor_mode = True
        ready = False
        physical_lock = None

        def open(self, device):
            if self.ready and self.device == device:
                return
            if self.device is not None:
                self.close()
            if device_lock_dir:
                directory = Path(device_lock_dir)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                key = hashlib.sha256(str(Path(device.usb_path).resolve()).encode()).hexdigest()
                physical_lock = (directory/(key+'.lock')).open('a')
                try:
                    fcntl.flock(physical_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    physical_lock.close()
                    raise
                self.physical_lock = physical_lock
            try:
                super().open(device)
                self.session.client(Qmi.Service.NAS)
                self.session.client(Qmi.Service.DMS)
                self.ready = True
                saved = self.state.load()
                if self.settings.modem.mode == 'monitor' and saved and (
                        recover_owned_connection or saved.get('cleanup_pending')):
                    self.recover()
            except BaseException:
                self.close()
                raise

        def close(self):
            self.ready = False
            try:
                super().close()
            finally:
                if self.physical_lock:
                    self.physical_lock.close()
                    self.physical_lock = None

        def prepare(self):
            if self.settings.network.method == 'dhcp' and not shutil.which('udhcpc'):
                raise RuntimeError('DHCP requires udhcpc. Install dependencies with qmi-supervisor-configure.')
            active = subprocess.run(['systemctl', 'is-active', '--quiet', 'ModemManager.service'],
                                    capture_output=True, timeout=3).returncode == 0
            if active:
                raise RuntimeError('ModemManager is active. Prepare exclusive modem management with '
                                   'qmi-supervisor-configure --modem-manager disable, or keep connection disabled.')
            super().prepare()

    class Controller(Supervisor):
        def sample(self):
            # Sampling belongs exclusively to the telemetry actor.
            pass

        def cleanup(self):
            try:
                if backend.ready and backend.present():
                    backend.disconnect()
                else:
                    backend.close()
            except Exception:
                LOG.exception('Owned connection cleanup failed')

    lock_path = Path(owner_lock_path) if owner_lock_path else runtime.parent/'qmi-supervisor.lock'
    if owner_lock_fd is not None:
        descriptor, expected = os.fstat(owner_lock_fd), lock_path.stat()
        if (descriptor.st_dev, descriptor.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError('Inherited modem lock does not match the instance lock')
        lock = os.fdopen(os.dup(owner_lock_fd), 'a')
    else:
        lock = lock_path.open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    heartbeat = Heartbeat(runtime/'modem.json', worker_id, settings)
    backend = Backend(settings, stop, str(state_path or runtime.parent/'qmi-supervisor-state.json'))
    backend.network.peer_routes = Path(peer_routes) if peer_routes else None
    # Different actor incarnations must never reuse a device-generation number.
    backend.generation = time.monotonic_ns() // 1000
    def selected_discovery(*args, **kwargs):
        found = (discovery or discover)(*args, **kwargs)
        return found if found is None or not usb_path or found.usb_path == str(usb_path) else None
    controller = Controller(settings, backend, SimpleNamespace(stats=lambda: {}), stop, discovery=selected_discovery)
    server = AcquisitionServer(runtime/'modem.sock', stop)
    request_ready = threading.Event()
    sampling = operating = False
    last_config = None
    next_step = 0

    def snapshot():
        network = backend.network
        return dict(state=controller.state.name, device=asdict(controller.device) if controller.device else None,
            error=controller.last_error, failed_step=controller.failed_state,
            retry_in_seconds=max(0, controller.deadline-time.monotonic()) if controller.state == State.BACKOFF else None,
            network=dict(address=network.address, route=network.route, route_metric=network.route_metric,
                         requested_route_metric=settings.network.route_metric, route_policy=settings.network.route_policy,
                         connectivity=network.connectivity_status),
            connection=dict(phase=controller.state.name, mode=settings.modem.mode,
                            requested_apn=settings.modem.apn, requested_plmn=settings.modem.plmn or None),
            device_generation=backend.generation, modem_readable=backend.ready,
            identity=dict(backend.static), skipped_intervals=0)

    def tick(*unused, allow_acquisition=True, force=False):
        nonlocal sampling
        heartbeat.settings = settings
        heartbeat.beat(force=force, phase=controller.state.name, snapshot=snapshot(), error=controller.last_error,
            applied_revision=revision(settings, ('modem', 'network', 'recovery')),
            operation_deadline=time.monotonic()+max(5, settings.recovery.operation_timeout+2) if operating else None)
        if stop.is_set() or sampling or not backend.ready or not allow_acquisition:
            return True
        item = server.take()
        if item is None:
            return True
        if not settings.telemetry.enabled or time.monotonic() >= item['deadline']:
            item['response'] = {'error': 'Monitoring is disabled or the acquisition expired'}
            item['done'].set()
            return True
        sampling = True
        previous = backend.settings
        started, utc = time.monotonic(), datetime.now(timezone.utc).isoformat()
        try:
            budget = min(settings.telemetry.collection_budget, item['deadline']-started)
            backend.settings = replace(settings, telemetry=replace(settings.telemetry, collection_budget=budget))
            backend.observation_state = controller.state.name
            static, dynamic = backend.metrics()
            item['response'] = dict(static=static, dynamic=dynamic, report=backend.collection_report,
                device=asdict(backend.device), generation=backend.generation,
                window=dict(started=started, start_utc=utc, ended=time.monotonic(),
                            queue_wait_ms=max(0, started-item['queued'])*1000))
        except Exception as error:
            item['response'] = {'error': str(error)}
        finally:
            backend.settings = previous
            item['done'].set()
            sampling = False
        return True

    def wake_acquisition():
        request_ready.set()
        idle = GLib.idle_source_new()
        idle.set_callback(lambda *args: (tick(), False)[1])
        idle.attach(backend.session.context)
        backend.session.context.wakeup()

    server.wakeup = wake_acquisition
    source = GLib.timeout_source_new(250)
    source.set_callback(tick)
    source.attach(backend.session.context)
    try:
        while not stop.is_set():
            request_ready.clear()
            if not server.thread.is_alive() or not server.path.is_socket():
                LOG.warning('Repairing the private acquisition channel; retaining modem ownership')
                server.close()
                server = AcquisitionServer(runtime/'modem.sock', stop)
                server.wakeup = wake_acquisition
            try:
                stat = path.stat()
                current = stat.st_mtime_ns, stat.st_size, stat.st_ino
                if current != last_config:
                    try:
                        candidate = load_settings(path, device_override)
                    except ValueError as error:
                        last_config = current
                        LOG.warning('Configuration update rejected: %s', error)
                        stop.wait(0.05)
                        continue
                    old = settings
                    changing_device = (old.modem.device, old.modem.serial) != (candidate.modem.device, candidate.modem.serial)
                    changing_connection = (old.modem, old.network, old.recovery) != (candidate.modem, candidate.network, candidate.recovery)
                    operating = True
                    if changing_device:
                        backend.close()
                    elif changing_connection and old.modem.mode == 'connect':
                        controller.transition(State.OPEN)
                        backend.disconnect()
                    if changing_connection:
                        backend.network = Network(candidate.network, candidate.recovery.operation_timeout, stop)
                        backend.network.changed = backend.checkpoint
                        backend.network.peer_routes = Path(peer_routes) if peer_routes else None
                    settings = candidate
                    backend.settings = controller.settings = settings
                    backend.session.timeout = settings.recovery.operation_timeout
                    if changing_device:
                        controller.device = None
                        controller.transition(State.WAIT_DEVICE)
                    elif changing_connection:
                        controller.transition(State.OPEN if backend.ready and settings.modem.mode == 'connect'
                                              else State.MONITOR if backend.ready else State.WAIT_DEVICE)
                    if backend.ready:
                        gps = settings.telemetry.enabled and settings.telemetry.gps and settings.telemetry.gps_source == 'modem'
                        if not gps and backend.gnss is not None:
                            backend.gnss.close()
                            backend.gnss = None
                        elif gps and backend.gnss is None:
                            backend.start_gnss()
                    last_config = current
            except Exception as error:
                controller.last_error = str(error)
                LOG.warning('Configuration application is pending: %s', error)
                stop.wait(1)
                continue
            finally:
                operating = False
            tick(allow_acquisition=False)
            if time.monotonic() >= next_step:
                operating = True
                tick(allow_acquisition=False, force=True)
                try:
                    controller.step()
                except Exception as error:
                    controller.fail(error)
                finally:
                    operating = False
                next_step = time.monotonic() + (1 if controller.state in {
                    State.WAIT_DEVICE, State.BACKOFF, State.WAIT_REGISTRATION, State.ONLINE, State.MONITOR} else 0)
            for _ in range(32):
                if not backend.session.context.pending():
                    break
                backend.session.context.iteration(False)
            tick()
            request_ready.wait(0.2)
    finally:
        source.destroy()
        server.close()
        try:
            backend.close()
        finally:
            heartbeat.beat(force=True, phase='STOPPED')
            heartbeat.close()
            lock.close()
    return 0


class RemoteBackend:
    """Only the modem actor timestamps and acquires radio observations."""
    def __init__(self, runtime, settings, run_id):
        self.runtime, self.settings, self.run_id = runtime, settings, run_id
        self.device = None
        self.generation = None
        self.collection_report = {}
        self.acquisition_window = None

    def metrics(self):
        budget = min(60, self.settings.telemetry.collection_budget)
        result = request(self.runtime/'modem.sock',
            dict(method='acquire', run_id=self.run_id, deadline=time.monotonic()+budget), budget+0.5)
        self.device = Device(**result['device'])
        self.generation = result['generation']
        self.collection_report = result['report']
        self.collection_report['ipc_queue_wait_ms'] = result['window']['queue_wait_ms']
        self.acquisition_window = result['window']
        return result['static'], result['dynamic']


def telemetry_worker(path, runtime, worker_id, settings, stop):
    from modules.telemetry.collector import Telemetry
    from modules.telemetry.history import AcquisitionHistory
    heartbeat = Heartbeat(runtime/'telemetry.json', worker_id, settings)
    sample = None
    published_log_id = None
    collector = Telemetry(settings.telemetry, stop)
    history = AcquisitionHistory(settings.web)
    history_config, next_history_config = None, 0
    backend = RemoteBackend(runtime, settings, collector.run_id)
    scheduled, pending_skips, skipped_total = time.monotonic(), 0, 0
    phase, error = 'WAIT_DEVICE', None
    completed = udp_completed = 0
    writer_progress = udp_progress = time.monotonic()
    previous_errors = (0, 0, 0)
    degraded_until = 0
    try:
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_history_config:
                next_history_config = now+1
                try:
                    metadata = path.stat()
                    fingerprint = metadata.st_mtime_ns, metadata.st_size, metadata.st_ino
                    if fingerprint != history_config:
                        history_config = fingerprint
                        history.configure(load_settings(path).web)
                except (OSError, ValueError):
                    # The manager reports rejected configuration; retain the
                    # last valid history policy without disturbing acquisition.
                    pass
            if now >= scheduled:
                missed = max(0, math.floor((now-scheduled)/settings.telemetry.interval))
                skipped_total += missed
                scheduled += missed*settings.telemetry.interval
                try:
                    # Device identity is supplied by the same acquisition, not a
                    # separate discovery that could race a USB replacement.
                    sample = collector.sample(backend, None, scheduled_monotonic=scheduled,
                                              skipped_intervals=pending_skips+missed)
                    history.append(sample)
                    pending_skips = 0
                    phase, error = 'RUNNING', None
                except Exception as failure:
                    pending_skips += missed+1
                    skipped_total += 1
                    phase, error = 'WAIT_DEVICE', str(failure)
                scheduled += settings.telemetry.interval
                if time.monotonic() >= scheduled:
                    overrun = math.floor((time.monotonic()-scheduled)/settings.telemetry.interval)+1
                    scheduled += overrun*settings.telemetry.interval
                    pending_skips += overrun
                    skipped_total += overrun
            stats = collector.stats()
            errors = tuple(stats.get(name, 0) for name in ('persistence_failed', 'persistence_dropped', 'flush_failed'))
            if errors != previous_errors:
                degraded_until = time.monotonic()+max(5, settings.telemetry.interval*2)
            previous_errors = errors
            if stats.get('last_write_success') is False or time.monotonic() < degraded_until:
                if phase == 'RUNNING':
                    phase = 'DEGRADED'
            # A new observation must reach the latest-only file before the next one
            # replaces it; the one-second throttle is only the liveness cadence.
            log_id = (sample or {}).get('log_id')
            heartbeat.beat(force=log_id != published_log_id, phase=phase, error=error, sample=sample, stats=stats,
                acquisition_history=history.snapshot(), skipped_intervals=skipped_total, run_id=collector.run_id,
                applied_revision=revision(settings, ('telemetry',)))
            published_log_id = log_id
            # A blocked writer cannot be cancelled safely inside a thread. Exit
            # this actor; only its own process is reaped and replaced.
            attempts = stats.get('persisted', 0)+stats.get('persistence_failed', 0)
            if not stats.get('pending') or attempts != completed:
                writer_progress = time.monotonic()
            completed = attempts
            udp_attempts = stats.get('udp_sent', 0)+stats.get('udp_failed', 0)
            if not stats.get('udp_pending') or udp_attempts != udp_completed:
                udp_progress = time.monotonic()
            udp_completed = udp_attempts
            if ((stats.get('pending') and time.monotonic()-writer_progress > 30)
                    or (stats.get('udp_pending') and time.monotonic()-udp_progress > 30)):
                LOG.error('Storage has made no timely progress; restarting telemetry only')
                return 1
            stop.wait(min(0.2, max(0, scheduled-time.monotonic())))
    finally:
        clean = collector.close(5)
        heartbeat.beat(force=True, phase='STOPPED', sample=sample, stats=collector.stats(),
                       acquisition_history=history.snapshot(), skipped_intervals=skipped_total, run_id=collector.run_id,
                       error=None if clean else 'Storage did not drain cleanly')
        heartbeat.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Internal QMI Supervisor actor')
    parser.add_argument('--role', choices=('modem', 'telemetry', 'web'), required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--worker-id', required=True)
    parser.add_argument('--device')
    parser.add_argument('--log-level', choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'), default='INFO')
    parser.add_argument('--owner-lock-fd', type=int)
    parser.add_argument('--recover-owned-connection', action='store_true')
    parser.add_argument('--retire-relocated', action='store_true')
    for option in ('owner-lock-path', 'state-path', 'device-lock-dir', 'usb-path', 'peer-routes'):
        parser.add_argument('--'+option, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format='%(asctime)s '+args.role+' %(levelname)s %(name)s: %(message)s')
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    settings = load_settings(args.config, args.device)
    try:
        return run_role(args, settings, stop)
    except Exception as error:
        LOG.exception('%s actor failed', args.role)
        heartbeat = Heartbeat(args.runtime/(args.role+'.json'), args.worker_id, settings)
        heartbeat.beat(force=True, phase='ERROR', error=f'{type(error).__name__}: {error}')
        heartbeat.close()
        return 1


def run_role(args, settings, stop):
    if args.role == 'web':
        from modules.web.server import serve
        heartbeat = Heartbeat(args.runtime/'web.json', args.worker_id, settings)
        try:
            serve(settings, args.config, args.runtime/'status.json', stop,
                  progress=lambda: heartbeat.beat(phase='RUNNING', applied_revision=revision(settings, ('web',))))
        finally:
            heartbeat.close()
        return 0
    if args.role == 'modem':
        return modem_worker(args.config, args.runtime, args.worker_id, settings, stop,
                            device_override=args.device, owner_lock_fd=args.owner_lock_fd,
                            recover_owned_connection=args.recover_owned_connection,
                            retire_relocated=args.retire_relocated,
                            **{key: getattr(args, key) for key in
                               ('owner_lock_path', 'state_path', 'device_lock_dir', 'usb_path', 'peer_routes')})
    return telemetry_worker(args.config, args.runtime, args.worker_id, settings, stop)


if __name__ == '__main__':
    raise SystemExit(main())
