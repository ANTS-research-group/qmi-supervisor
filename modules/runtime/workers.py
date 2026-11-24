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
from modules.runtime import diagnosis
from modules.runtime.ipc import AcquisitionServer, Heartbeat, request, revision
from modules.runtime.settings import load_settings
from modules.runtime.state import RuntimeState
from modules.runtime.version import installed_version

LOG = logging.getLogger(__name__)


def modem_worker(path, runtime, worker_id, settings, stop, *, discovery=None, device_override=None,
                 owner_lock_fd=None, owner_lock_path=None, state_path=None, device_lock_dir=None,
                 usb_path=None, peer_routes=None, recover_owned_connection=False, retire_relocated=False,
                 log_filter=None):
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
                    with diagnosis.stage('open.lock'):
                        fcntl.flock(physical_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException as error:
                    physical_lock.close()
                    if isinstance(error, BlockingIOError):
                        diagnosis.annotate(error, 'device.locked')
                    raise
                self.physical_lock = physical_lock
            try:
                with diagnosis.stage('open.session'):
                    super().open(device)
                with diagnosis.stage('open.allocate'):
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
                raise diagnosis.annotate(RuntimeError(
                    'DHCP requires udhcpc. Install dependencies with qmi-supervisor-configure.'),
                    'host.udhcpc_missing')
            active = subprocess.run(['systemctl', 'is-active', '--quiet', 'ModemManager.service'],
                                    capture_output=True, timeout=3).returncode == 0
            if active:
                raise diagnosis.annotate(RuntimeError(
                    'ModemManager is active. Prepare exclusive modem management with '
                    'qmi-supervisor-configure --modem-manager disable, or keep connection disabled.'),
                    'host.modemmanager_active')
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
    streak = RuntimeState(runtime/'failure-streak.json')

    def restore_streak(device):
        """A saved streak is evidence only for the same physical modem and port."""
        try:
            saved = streak.load()
        except (OSError, ValueError):
            return None
        if not saved or not isinstance(saved.get('tracker'), dict):
            return None
        if saved.get('identity') != device.identity:
            return None
        if saved.get('usb_path') != str(Path(device.usb_path).resolve()):
            return None
        return saved['tracker']

    controller = Controller(settings, backend, SimpleNamespace(stats=lambda: {}), stop,
                            discovery=selected_discovery, restore_streak=restore_streak)
    controller.limiter = log_filter
    streak_written, streak_saved_at = None, 0.0

    def save_streak():
        nonlocal streak_written, streak_saved_at
        try:
            record = controller.tracker.to_record()
            if record is None:
                if streak.path.exists():
                    streak.clear()
                    streak_written, streak_saved_at = None, time.monotonic()
                return
            if controller.device is None:
                return
            payload = {'identity': controller.device.identity,
                       'usb_path': str(Path(controller.device.usb_path).resolve()), 'tracker': record}
            if payload == streak_written:
                return
            important = streak_written is None or any(
                payload['tracker'].get(key) != streak_written['tracker'].get(key)
                for key in ('code', 'step', 'operation', 'error_logged', 'previous', 'since_utc'))
            now = time.monotonic()
            if important or now - streak_saved_at >= diagnosis.STREAK_SAVE_SECONDS:
                streak.save(payload)
                streak_written, streak_saved_at = payload, now
        except (OSError, ValueError):
            pass
    server = AcquisitionServer(runtime/'modem.sock', stop)
    request_ready = threading.Event()
    sampling = operating = False
    last_config = None
    next_step = 0

    def registration_view():
        """The registration episode while it is being waited on or its code is active."""
        if getattr(backend, 'registration', None) is None:
            return None
        code = (controller.diagnosis() or {}).get('code') or ''
        if controller.state.name != 'WAIT_REGISTRATION' and not str(code).startswith('registration.'):
            return None
        summary = getattr(backend, 'registration_summary', None)
        return summary() if callable(summary) else None

    def snapshot():
        network = backend.network
        published = controller.diagnosis()
        if published is None and controller.config_issue:
            issue = controller.config_issue
            published = {'code': 'config.pending', 'severity': 'warning', 'active': True,
                         'step': issue.get('step'), 'operation': issue.get('operation'),
                         'params': issue.get('params') or {}, 'detail': issue.get('detail') or '',
                         'observed_at_utc': datetime.now(timezone.utc).isoformat()}
        published = diagnosis.bounded_diagnosis(published)
        profile = runtime.name if runtime.parent.name == 'modems' else 'default'
        sim = getattr(backend, 'sim_report', None)
        if isinstance(sim, dict) and hasattr(backend, 'sim_recovery_view'):
            sim = dict(sim, recovery=backend.sim_recovery_view(),
                       slots=getattr(backend, 'sim_slots', None))
        return dict(state=controller.state.name, id=profile,
            device=diagnosis.public_device(controller.device),
            log_label=diagnosis.log_label(device=controller.device, serial=settings.modem.serial,
                                          profile=profile),
            error=controller.last_error, failed_step=controller.failed_state,
            retry_in_seconds=max(0, controller.deadline-time.monotonic()) if controller.state == State.BACKOFF else None,
            diagnosis=published, sim=sim, registration=registration_view(),
            registered_plmn=getattr(backend, 'registered_plmn', None),
            recovery_pending=bool(getattr(backend, '_pending_recovery', None)),
            network=dict(address=network.address, gateway=getattr(network, 'gateway', None),
                         interface=getattr(network, 'interface', None),
                         route=network.route, route_metric=network.route_metric,
                         requested_route_metric=settings.network.route_metric, route_policy=settings.network.route_policy,
                         connectivity=network.connectivity_status),
            connection=dict(phase=controller.state.name, mode=settings.modem.mode,
                            requested_apn=settings.modem.apn, requested_plmn=settings.modem.plmn or None),
            device_generation=backend.generation, modem_readable=backend.ready,
            identity=dict(backend.static), skipped_intervals=0)

    def compact_diagnosis():
        published = controller.diagnosis()
        if not isinstance(published, dict):
            return None
        return {key: published.get(key) for key in ('code', 'severity', 'attempts', 'failing_since_utc')}

    def unreadable_response():
        state = controller.state.name
        return {'error': 'Modem session is not readable while connection step %s is retried'
                         % (controller.failed_state or state),
                'code': 'modem_absent' if controller.device is None else 'modem_not_readable',
                'state': state, 'failed_step': controller.failed_state,
                'diagnosis': compact_diagnosis(),
                'sim': getattr(backend, 'sim_report', None),
                'registration': registration_view(),
                'device_identity': getattr(controller.device, 'identity', None),
                'generation': backend.generation}

    def reconnect_response():
        """Actor-loop reconnect: only this thread transitions, never the socket reader."""
        if settings.modem.mode != 'connect':
            return {'ok': False, 'error': 'not_connect_mode'}
        if getattr(backend, '_pending_recovery', None):
            return {'ok': False, 'error': 'recovery_pending'}
        if not backend.ready:
            return {'ok': False, 'error': 'unavailable'}
        try:
            if controller.state in (State.ONLINE, State.CONNECT, State.CONFIGURE_IP):
                backend.disconnect()
            controller.transition(State.OPEN)
        except Exception:
            return {'ok': False, 'error': 'unavailable'}
        return {'ok': True}

    def tick(*unused, allow_acquisition=True, force=False):
        nonlocal sampling
        heartbeat.settings = settings
        heartbeat.beat(force=force, phase=controller.state.name, snapshot=snapshot(), error=controller.last_error,
            applied_revision=revision(settings, ('modem', 'network', 'recovery')),
            operation_deadline=time.monotonic()+max(5, settings.recovery.operation_timeout+2) if operating else None)
        if stop.is_set() or sampling or not allow_acquisition:
            return True
        if not backend.ready:
            # The actor cannot read the modem. Peek first: an OPEN item must stay
            # queued so a quick open can still serve it, while an expired one is
            # drained so the one-slot queue never stays full.
            pending = server.peek()
            if pending is None:
                return True
            if time.monotonic() >= pending['deadline']:
                pending = server.take()
                if pending is not None:
                    pending['response'] = {'error': 'Acquisition deadline expired',
                                           'code': 'acquisition_expired'}
                    pending['done'].set()
            elif controller.state in {State.BACKOFF, State.WAIT_DEVICE}:
                pending = server.take()
                if pending is not None:
                    pending['response'] = unreadable_response()
                    pending['done'].set()
            # OPEN may still finish in time: the item is left until its deadline.
            return True
        item = server.take()
        if item is None:
            return True
        if item.get('method') == 'reconnect':
            item['response'] = reconnect_response()
            item['done'].set()
            return True
        if not settings.telemetry.enabled:
            item['response'] = {'error': 'Monitoring is disabled', 'code': 'monitoring_disabled'}
            item['done'].set()
            return True
        if time.monotonic() >= item['deadline']:
            item['response'] = {'error': 'Acquisition deadline expired', 'code': 'acquisition_expired'}
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
                state=controller.state.name, diagnosis=compact_diagnosis(),
                sim=getattr(backend, 'sim_report', None),
                registration=registration_view(),
                window=dict(started=started, start_utc=utc, ended=time.monotonic(),
                            queue_wait_ms=max(0, started-item['queued'])*1000))
        except Exception as error:
            item['response'] = {'error': str(error), 'code': 'acquisition_failed',
                                'state': controller.state.name, 'diagnosis': compact_diagnosis()}
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
                    if (changing_connection and (old.modem.plmn, old.modem.apn)
                            != (settings.modem.plmn, settings.modem.apn)
                            and hasattr(backend, 'reset_registration')):
                        backend.reset_registration()
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
                    controller.config_issue = None
            except Exception as error:
                controller.last_error = str(error)
                controller.config_issue = diagnosis.classify(
                    diagnosis.annotate(error, 'config.pending'), 'CONFIG')
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
                save_streak()
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
        self.state = None
        self.diagnosis = None
        self.sim = None
        self.registration = None
        self.collection_report = {}
        self.acquisition_window = None

    def metrics(self):
        budget = min(60, self.settings.telemetry.collection_budget)
        result = request(self.runtime/'modem.sock',
            dict(method='acquire', run_id=self.run_id, deadline=time.monotonic()+budget), budget+0.5)
        self.device = Device(**result['device'])
        self.generation = result['generation']
        self.state = result.get('state')
        self.diagnosis = result.get('diagnosis')
        self.sim = result.get('sim')
        self.registration = result.get('registration')
        self.collection_report = result['report']
        self.collection_report['ipc_queue_wait_ms'] = result['window']['queue_wait_ms']
        self.acquisition_window = result['window']
        return result['static'], result['dynamic']


def gap_priority(code):
    """A specific coded reason outranks a generic expiry, which outranks free text."""
    if not code:
        return 1
    return 2 if code == 'acquisition_expired' else 3


def telemetry_worker(path, runtime, worker_id, settings, stop):
    from modules.telemetry.collector import HealthFile, Telemetry, health_document
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
    health = HealthFile(settings.telemetry)
    profile = runtime.name if runtime.parent.name == 'modems' else 'default'
    last_sample_mono, last_sample_utc = scheduled, datetime.now(timezone.utc).isoformat()
    last_stored_utc = None
    stall = stall_started = None
    gap_reason, gap_code = None, None
    connection_state, connection_diagnosis = 'WAIT_DEVICE', None
    connection_sim = connection_registration = None
    next_health, health_fingerprint = 0.0, None

    def modem_label():
        identity = (sample or {}).get('device_identity') or settings.modem.serial
        return diagnosis.log_label(serial=identity, profile=profile)

    def publish_health(force=False):
        nonlocal next_health, health_fingerprint
        if health.writer is None:
            return
        now = time.monotonic()
        fingerprint = ((connection_diagnosis or {}).get('code'),
                       (connection_diagnosis or {}).get('severity'),
                       connection_state, bool(stall), (connection_sim or {}).get('code'),
                       (connection_registration or {}).get('code'))
        if not force and fingerprint == health_fingerprint and now < next_health:
            return
        registration = None
        if isinstance(connection_registration, dict):
            registration = {
                'code': connection_registration.get('code'),
                'home': connection_registration.get('home'),
                'camped': [entry.get('plmn') for entry in
                           (connection_registration.get('camped') or [])
                           if isinstance(entry, dict) and entry.get('plmn')][:16]}
        health.offer(health_document(
            modem=modem_label(), profile=profile,
            connection_state=connection_state, supervisor_version=installed_version(),
            last_sample_at_utc=last_stored_utc,
            diagnosis=connection_diagnosis, stall=stall,
            sim={'code': (connection_sim or {}).get('code')} if connection_sim else None,
            registration=registration))
        health_fingerprint, next_health = fingerprint, now + diagnosis.HEALTH_REFRESH_SECONDS
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
                    last_sample_mono = time.monotonic()
                    last_sample_utc = last_stored_utc = datetime.now(timezone.utc).isoformat()
                    connection_state = backend.state or 'RUNNING'
                    connection_diagnosis = backend.diagnosis
                    connection_sim = backend.sim
                    connection_registration = backend.registration
                    gap_code = gap_reason = None
                    if stall is not None:
                        LOG.info('Telemetry resumed after %s without samples (%d intervals skipped)',
                                 diagnosis.format_duration(last_sample_mono-stall_started),
                                 max(0, skipped_total-stall['skipped']))
                        stall = stall_started = None
                except Exception as failure:
                    pending_skips += missed+1
                    skipped_total += 1
                    phase, error = 'WAIT_DEVICE', str(failure)
                    details = getattr(failure, 'details', None) or {}
                    code = getattr(failure, 'code', None) or 'acquisition_failed'
                    # A generic expiry must never replace a specific reason: the
                    # stall should keep saying 'modem_not_readable'.
                    if gap_priority(code) >= gap_priority(gap_code):
                        gap_code, gap_reason = code, diagnosis.bounded_text(str(failure))
                    if getattr(failure, 'code', None):
                        connection_state = details.get('state') or connection_state
                        if isinstance(details.get('diagnosis'), dict):
                            connection_diagnosis = details['diagnosis']
                        if isinstance(details.get('sim'), dict):
                            connection_sim = details['sim']
                        if isinstance(details.get('registration'), dict):
                            connection_registration = details['registration']
                scheduled += settings.telemetry.interval
                if time.monotonic() >= scheduled:
                    overrun = math.floor((time.monotonic()-scheduled)/settings.telemetry.interval)+1
                    scheduled += overrun*settings.telemetry.interval
                    pending_skips += overrun
                    skipped_total += overrun
            if stall is None and time.monotonic()-last_sample_mono >= diagnosis.stall_seconds(settings.telemetry.interval):
                stall_started = last_sample_mono
                stall = {'since_utc': last_sample_utc, 'last_sample_at_utc': last_stored_utc,
                         'reason_code': gap_code or 'acquisition_failed',
                         'reason': gap_reason or '',
                         'connection_code': (connection_diagnosis or {}).get('code'),
                         'skipped': skipped_total}
                LOG.warning('Telemetry stalled: no sample since %s UTC (%s)',
                            last_sample_utc, gap_reason or 'no acquisition completed')
            publish_health()
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
                stall=stall, applied_revision=revision(settings, ('telemetry',)))
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
        publish_health(force=True)
        health.close()
        heartbeat.beat(force=True, phase='STOPPED', sample=sample, stats=collector.stats(),
                       acquisition_history=history.snapshot(), skipped_intervals=skipped_total, run_id=collector.run_id,
                       stall=stall, error=None if clean else 'Storage did not drain cleanly')
        heartbeat.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Internal QMI Supervisor actor')
    parser.add_argument('--role', choices=('modem', 'telemetry', 'web', 'bus', 'plugins'), required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--worker-id', required=True)
    parser.add_argument('--device')
    parser.add_argument('--log-level', choices=('DEBUG', 'INFO', 'WARNING', 'ERROR'), default='INFO')
    parser.add_argument('--log-label', default='')
    parser.add_argument('--owner-lock-fd', type=int)
    parser.add_argument('--recover-owned-connection', action='store_true')
    parser.add_argument('--retire-relocated', action='store_true')
    for option in ('owner-lock-path', 'state-path', 'device-lock-dir', 'usb-path', 'peer-routes'):
        parser.add_argument('--'+option, type=Path)
    args = parser.parse_args()
    label = args.log_label.strip()
    identity = args.role + (f'[{label}]' if label else '')
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format='%(asctime)s '+identity+' %(levelname)s %(name)s: %(message)s')
    limiter = None
    if args.role == 'modem':
        from modules.runtime.logfilter import install
        limiter = install()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    settings = load_settings(args.config, args.device)
    try:
        return run_role(args, settings, stop, limiter)
    except Exception as error:
        LOG.exception('%s actor failed', args.role)
        heartbeat = Heartbeat(args.runtime/(args.role+'.json'), args.worker_id, settings)
        heartbeat.beat(force=True, phase='ERROR', error=f'{type(error).__name__}: {error}')
        heartbeat.close()
        return 1


def run_role(args, settings, stop, limiter=None):
    if args.role == 'bus':
        from modules.runtime.bus import bus_worker
        return bus_worker(args.config, args.runtime, args.worker_id, settings, stop)
    if args.role == 'plugins':
        from modules.runtime.plugin_supervisor import run_plugins
        return run_plugins(args.config, args.runtime, args.worker_id, settings, stop)
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
                            retire_relocated=args.retire_relocated, log_filter=limiter,
                            **{key: getattr(args, key) for key in
                               ('owner_lock_path', 'state_path', 'device_lock_dir', 'usb_path', 'peer_routes')})
    return telemetry_worker(args.config, args.runtime, args.worker_id, settings, stop)


if __name__ == '__main__':
    raise SystemExit(main())
