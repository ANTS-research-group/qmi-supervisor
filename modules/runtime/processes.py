"""A single systemd service, with independently supervised executable children."""
from dataclasses import asdict
import fcntl
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

from modules.runtime.ipc import read_json, revision
from modules.runtime.settings import load_settings

LOG = logging.getLogger(__name__)
ROLES = ('modem', 'telemetry', 'web')


class Child:
    def __init__(self, role):
        self.role = role
        self.process = None
        self.worker_id = None
        self.started = self.last_heartbeat = self.next_start = 0
        self.failures = self.restarts = 0
        self.signature = None
        self.error = None
        self.status = {}
        self.status_fingerprint = None
        self.log_level = 'INFO'
        self._status_runtime = self._status_path = None

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def start(self, config, runtime, signature, *, popen=subprocess.Popen, clock=time.monotonic,
              device_override=None, log_level='INFO', owner_lock_fd=None, worker_options=None):
        if self.process is not None:
            raise RuntimeError('An unreaped worker cannot be replaced')
        self.worker_id = uuid.uuid4().hex
        command = [sys.executable, '-m', 'modules.runtime.workers', '--role', self.role,
                   '--config', str(config), '--runtime', str(runtime), '--worker-id', self.worker_id]
        if device_override:
            command += ['--device', device_override]
        if log_level != 'INFO':
            command += ['--log-level', log_level]
        for name, value in (worker_options or {}).items():
            if value is True:
                command.append('--'+name.replace('_', '-'))
            elif value is not None and value is not False:
                command += ['--'+name.replace('_', '-'), str(value)]
        descriptors = ()
        if self.role == 'modem' and owner_lock_fd is not None:
            command += ['--owner-lock-fd', str(owner_lock_fd)]
            descriptors = (owner_lock_fd,)
        self.process = popen(command, cwd=Path(__file__).resolve().parents[2], start_new_session=True,
                             pass_fds=descriptors)
        self.signature = signature
        self.log_level = log_level
        self.started = self.last_heartbeat = clock()
        self.status = {}
        self.status_fingerprint = None
        LOG.info('Started %s worker pid=%s', self.role, self.process.pid)

    def stop(self, *, timeout=6, failed=False, clock=time.monotonic):
        if self.process is None:
            return True
        process = self.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.error = 'Worker could not be reaped; replacement is blocked'
                    return False
        else:
            process.wait()
        self.process = None
        if failed:
            self.next_start = clock() + min(2 * 2 ** min(self.failures, 5), 60)
            self.failures += 1
            self.restarts += 1
        return True

    def observe(self, runtime, *, clock=time.monotonic):
        runtime = Path(runtime)
        if runtime != self._status_runtime:
            # Keep heartbeat filename components alive instead of repeatedly
            # inserting/removing them from pathlib's interned-string table.
            self._status_runtime = runtime
            self._status_path = runtime / (self.role + '.json')
            self.status_fingerprint = None
        path = self._status_path
        status = None
        try:
            metadata = path.stat()
            fingerprint = metadata.st_mtime_ns, metadata.st_size, metadata.st_ino
            if fingerprint != self.status_fingerprint:
                status = read_json(path)
                if status is not None:
                    self.status_fingerprint = fingerprint
        except OSError:
            pass
        if status and status.get('worker_id') == self.worker_id:
            observed = status.get('heartbeat_monotonic')
            if type(observed) in (int, float) and self.last_heartbeat <= observed <= clock():
                self.last_heartbeat = observed
                self.status = status
                if self.alive:
                    self.error = None
        if self.alive and clock()-self.started > 60 and clock()-self.last_heartbeat < 5:
            self.failures = 0


def notify_systemd(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.connect('\0' + address[1:] if address.startswith('@') else address)
            connection.sendall(message.encode())
    except OSError:
        pass


class ProcessManager:
    def __init__(self, config, settings, stop, *, runtime='/run/qmi-supervisor', popen=subprocess.Popen,
                 clock=time.monotonic, device_override=None, log_level='INFO',
                 manage_web=True, worker_options=None):
        self.config, self.settings, self.stop = Path(config).resolve(), settings, stop
        self.runtime, self.popen, self.clock = Path(runtime), popen, clock
        self.device_override, self.log_level = device_override, log_level
        self.children = {role: Child(role) for role in ROLES}
        self.publisher = None
        self.last_sample = None
        self.revision = revision(load_settings(self.config))
        self.fingerprint = None
        self.error = None
        self.owner_lock_fd = None
        self.instance_id = uuid.uuid4().hex
        self.manage_web, self.worker_options = manage_web, worker_options or {}

    @staticmethod
    def signature(settings, role):
        if role == 'telemetry':
            return revision(settings, ('telemetry',))
        if role == 'web':
            return revision(settings, ('web',))
        # The modem actor applies settings in place, preserving its QMI ownership.
        return 'modem'

    def reload(self):
        try:
            stat = self.config.stat()
            fingerprint = stat.st_mtime_ns, stat.st_size, stat.st_ino
            if fingerprint == self.fingerprint:
                return
            self.fingerprint = fingerprint
            stored = load_settings(self.config)
            settings = load_settings(self.config, self.device_override) if self.device_override else stored
            if self.publisher and settings.web.history_size != self.settings.web.history_size:
                self.publisher.resize_history(settings.web.history_size)
            self.settings, self.revision = settings, revision(stored)
            self.error = None
        except (OSError, ValueError) as error:
            self.error = str(error)
            LOG.warning('Configuration update rejected: %s', error)

    def cleanup_pending(self):
        if self.settings.modem.mode == 'connect':
            return False
        path = Path(self.worker_options.get('state_path') or self.runtime.parent/'qmi-supervisor-state.json')
        try:
            return path.exists()
        except OSError:
            # An unreadable checkpoint cannot acknowledge a successful release.
            return True

    def reconcile(self):
        self.reload()
        settings, now = self.settings, self.clock()
        cleanup_pending = self.cleanup_pending()
        wanted = {'modem': settings.modem.mode == 'connect' or settings.telemetry.enabled or cleanup_pending,
                  'telemetry': settings.telemetry.enabled, 'web': self.manage_web and settings.web.enabled}
        # Stop consumers before the modem; starting follows the opposite order.
        for role in reversed(ROLES):
            child = self.children[role]
            child.observe(self.runtime, clock=self.clock)
            signature = self.signature(settings, role)
            if not wanted[role] or (child.process is not None and
                                    (signature != child.signature or self.log_level != child.log_level)):
                child.stop(timeout=6)
                child.observe(self.runtime, clock=self.clock)
                child.next_start = 0
            elif child.process is not None and (not child.alive or
                    (now-child.last_heartbeat > 15 and now > (child.status.get('operation_deadline') or 0))):
                child.error = ('Worker heartbeat expired' if child.alive
                               else f'Worker exited with status {child.process.returncode}')
                LOG.warning('%s: %s', role, child.error)
                child.stop(timeout=3, failed=True, clock=self.clock)
        for role in ROLES:
            child = self.children[role]
            if wanted[role] and child.process is None and now >= child.next_start:
                try:
                    options = dict(self.worker_options)
                    if role == 'modem' and cleanup_pending:
                        options['recover_owned_connection'] = True
                    child.start(self.config, self.runtime, self.signature(settings, role),
                                popen=self.popen, clock=self.clock, device_override=self.device_override,
                                log_level=self.log_level, owner_lock_fd=self.owner_lock_fd,
                                worker_options=options)
                except OSError as error:
                    child.error = str(error)
                    child.next_start = now + min(2 * 2 ** min(child.failures, 5), 60)
                    child.failures += 1
        if settings.web.enabled:
            self.publish()
        elif self.publisher:
            self.publisher.set_resource_targets(None)

    def feature(self, role, enabled, applied):
        child, now = self.children[role], self.clock()
        age = max(0, now-child.last_heartbeat) if child.process is not None else None
        fresh = child.alive and bool(child.status) and age is not None and age < 15
        cleanup = role == 'modem' and not enabled and self.cleanup_pending()
        acknowledged = not enabled and child.process is None and not cleanup
        pending = cleanup or not acknowledged and child.status.get('applied_revision') != applied
        state = ('STOPPING' if child.status.get('phase') in {'ONLINE', 'CONNECT', 'CONFIGURE_IP'} else 'PENDING') if pending else 'DISABLED'
        return dict(enabled=enabled, state=state if not enabled else
                    child.status.get('phase', 'STARTING') if fresh and child.status else 'RECOVERING',
                    pid=child.process.pid if child.process else None,
                    fresh=fresh if enabled else True, heartbeat_age_ms=age*1000 if age is not None else None,
                    applied_revision=applied if acknowledged else child.status.get('applied_revision'), desired_revision=applied,
                    pending=pending,
                    cleanup_pending=cleanup,
                    effective=enabled and fresh and child.status.get('phase') in {'ONLINE', 'RUNNING', 'DEGRADED'},
                    error=child.status.get('error') or child.error,
                    retry_in_seconds=max(0, child.next_start-now) if enabled and not child.alive else None,
                    restarts=child.restarts)

    def publish(self):
        from modules.runtime.platform import StatusPublisher
        settings = self.settings
        if self.publisher is None:
            self.publisher = StatusPublisher(self.runtime/'status.json', history_size=settings.web.history_size)
        def targets():
            result = {'manager': os.getpid(), **{role: child.process for role, child in self.children.items()}}
            result['supervisor'] = self.children['modem'].process
            return result
        if self.publisher._resource_targets is None:
            self.publisher.set_resource_targets(targets)
        modem, telemetry = self.children['modem'], self.children['telemetry']
        snapshot = dict(modem.status.get('snapshot') or {})
        snapshot.setdefault('state', 'STARTING' if modem.alive else 'IDLE')
        connection_enabled = settings.modem.mode == 'connect'
        idle = not connection_enabled and not settings.telemetry.enabled and not self.cleanup_pending()
        if idle:
            snapshot.update(state='IDLE', device=None, network={}, error=None)
        elif not modem.alive:
            snapshot.update(state='BACKOFF', error=modem.error, network={}, modem_readable=False)
        snapshot.update(config_revision=self.revision, configuration_error=self.error,
            profile_instance_id=self.instance_id,
            device_override=self.device_override,
            features={'connection': self.feature('modem', connection_enabled,
                         revision(settings, ('modem', 'network', 'recovery'))),
                      'telemetry': self.feature('telemetry', settings.telemetry.enabled,
                         revision(settings, ('telemetry',)))},
            processes={role: dict(alive=child.alive, pid=child.process.pid if child.process else None,
                         worker_id=child.worker_id if child.process is not None else None,
                         restarts=child.restarts, error=child.status.get('error') or child.error,
                         state=child.status.get('phase') if child.alive else 'STOPPED',
                         heartbeat_monotonic=child.last_heartbeat if child.alive and child.status else None,
                         heartbeat_at_utc=child.status.get('heartbeat_at_utc') if child.alive else None,
                         heartbeat_age_ms=max(0, self.clock()-child.last_heartbeat)*1000 if child.alive else None,
                         fresh=child.alive and bool(child.status) and self.clock()-child.last_heartbeat < 15,
                         applied_revision=child.status.get('applied_revision'),
                         desired_revision=revision(settings, ('modem', 'network', 'recovery')) if role == 'modem'
                                          else self.signature(settings, role))
                       for role, child in self.children.items()},
            telemetry=telemetry.status.get('stats', {}),
            skipped_intervals=telemetry.status.get('skipped_intervals', snapshot.get('skipped_intervals', 0)),
            run_id=telemetry.status.get('run_id'),
            heartbeat_monotonic=modem.status.get('heartbeat_monotonic') if modem.alive else None,
            heartbeat_at_utc=modem.status.get('heartbeat_at_utc') if modem.alive else None)
        if idle:
            snapshot.update(heartbeat_monotonic=self.clock())
        telemetry_status = dict(telemetry.status)
        if not settings.telemetry.enabled:
            telemetry_status['sample'] = None
        self.publisher.update_platform(snapshot, telemetry_status or None)

    def run(self):
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime.chmod(0o700)
        # Reserve the legacy instance lock even while objectives are idle.
        # Only the modem child inherits the same locked open-file description.
        with (self.runtime/'manager.lock').open('a') as lock, \
                (self.runtime.parent/'qmi-supervisor.lock').open('a') as modem_lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(modem_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.error('Another QMI Supervisor instance or legacy modem owner is running')
                return 1
            self.owner_lock_fd = modem_lock.fileno()
            notify_systemd('READY=1')
            try:
                while not self.stop.is_set():
                    self.reconcile()
                    notify_systemd('WATCHDOG=1')
                    self.stop.wait(0.25)
            finally:
                notify_systemd('STOPPING=1')
                for role in ('telemetry', 'modem', 'web'):
                    self.children[role].stop(timeout=8)
                if self.publisher:
                    self.publisher.close()
        return 0
