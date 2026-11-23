"""Shared fakes for the updater tests (not a test module)."""
from datetime import timedelta
import io
import json
from pathlib import Path
import re
import shutil
import subprocess

TEST_FINGERPRINT = '54F3194C1190482A124798C299A63F58A88CB47E'
DEB_NAME = re.compile(r'qmi-supervisor_(.+)_all\.deb')


class FakeClock:
    """Monotonic time, UTC wall time and local time advance together."""

    def __init__(self, start_utc, start_monotonic=1000.0):
        self.now_utc = start_utc
        self.now_monotonic = float(start_monotonic)
        self.callbacks = []

    def monotonic(self):
        return self.now_monotonic

    def utcnow(self):
        return self.now_utc

    def now_local(self):
        return self.now_utc.astimezone()

    def on_tick(self, callback):
        self.callbacks.append(callback)

    def sleep(self, seconds):
        self.now_monotonic += seconds
        self.now_utc += timedelta(seconds=seconds)
        for callback in list(self.callbacks):
            callback()


class _FakeResponse:
    status = 200
    headers = {}

    def __init__(self, opener, payload):
        self.opener = opener
        self.stream = io.BytesIO(payload)

    def read(self, size=-1):
        chunk = self.stream.read(size)
        self.opener.bytes_read += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    """An `open_url` that serves `files` (URL -> bytes or exception) and records reads."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.urls = []
        self.bytes_read = 0
        self.calls = []

    def __call__(self, url, *, accept, timeout):
        self.urls.append(url)
        self.calls.append((url, accept, timeout))
        value = self.files[url]
        if isinstance(value, BaseException):
            raise value
        return _FakeResponse(self, value)


def _completed(args, returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class FakeUpdateHost:
    """In-memory dpkg, apt-get, dpkg-deb, gpgv, systemd and runtime files."""

    def __init__(self, root, clock, *, installed='1.0.0-1', status='ii', service='active',
                 web=True, online=('default',), fingerprint=TEST_FINGERPRINT,
                 install_modes=None, after_install_steps=None):
        self.root = Path(root)
        self.clock = clock
        self.installed, self.status, self.service = installed, status, service
        self.web, self.online = web, tuple(online)
        self.fingerprint = fingerprint
        self.calls = []
        self.packages = {}
        self.incomplete = False
        self.config_rc = 0
        self.config_stderr = 'qmi_supervisor.py: error: Unknown configuration section [update]'
        self.gpgv = 'good'
        self.install_mode = 'ok'
        self.install_stderr = ''
        self.install_modes = list(install_modes or [])
        self.after_install_steps = list(after_install_steps or [])
        self.install_hook = None
        self.main_pid, self.restarts = 4242, 0
        self.reported_version = 'target'      # 'target' | 'old' | None
        self.after_install = {}
        self.online_delay = 0
        self.previous_version = installed
        self.last_install_version = installed
        self.started_monotonic = clock.monotonic() + 1
        self.worker_id = 0
        self.online_since = None
        self.healthy_at = float('-inf')
        self.pid_change_at = None
        self.restart_at = None
        self._pid_changed = self._restarted_count = False
        self.refresh()
        clock.on_tick(self.refresh)

    # -- runtime files ---------------------------------------------------
    def _version_to_report(self):
        if self.reported_version == 'target':
            return self.last_install_version
        if self.reported_version == 'old':
            return self.previous_version
        return None

    def _write(self, path, document):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))

    def refresh(self):
        now = self.clock.monotonic()
        if self.pid_change_at is not None and now >= self.pid_change_at and not self._pid_changed:
            self._pid_changed = True
            self.main_pid += 1
        if self.restart_at is not None and now >= self.restart_at and not self._restarted_count:
            self._restarted_count = True
            self.restarts += 1
        self.worker_id += 1
        healthy = now >= self.healthy_at
        version = self._version_to_report()
        if version is not None:
            self._write(self.root / 'version.json', {
                'schema_version': 1, 'supervisor_version': version, 'pid': self.main_pid,
                'started_monotonic': self.started_monotonic})
        else:
            try:
                (self.root / 'version.json').unlink()
            except OSError:
                pass
        self._write(self.root / 'web.json', {
            'phase': 'RUNNING' if self.web and healthy else 'STOPPED',
            'worker_id': 'web', 'heartbeat_monotonic': now})
        self._write(self.root / 'status.json', {
            'published_monotonic': now if self.web and healthy else 0})
        modems = self.root / 'modems'
        modems.mkdir(parents=True, exist_ok=True)
        for existing in modems.iterdir():
            if existing.name not in self.online:
                shutil.rmtree(existing, ignore_errors=True)
        for profile in self.online:
            online = healthy and (self.online_since is None
                                  or now - self.online_since >= self.online_delay)
            self._write(modems / profile / 'modem.json', {
                'phase': 'ONLINE' if online else 'STOPPED', 'worker_id': self.worker_id,
                'heartbeat_monotonic': now, 'snapshot': {'identity': 'never read'}})

    # -- command dispatch ------------------------------------------------
    def __call__(self, args, *, timeout=None, env=None, cwd=None):
        args = list(args)
        self.calls.append(args)
        if args[0] == 'dpkg' and '--configure' in args and '-a' in args:
            raise AssertionError('dpkg --configure -a is forbidden')
        if args[0] == 'dpkg-query':
            return self._dpkg_query(args)
        if args[0] == 'dpkg-deb' and len(args) > 1 and args[1] == '-f':
            return self._dpkg_fields(args)
        if args[0] == 'dpkg-deb' and len(args) > 1 and args[1] == '-x':
            return self._dpkg_extract(args)
        if args[0] == 'gpgv':
            return self._gpgv(args)
        if '--check-config' in args:
            stderr = self.config_stderr + '\n' if self.config_rc else ''
            return _completed(args, self.config_rc, '', stderr)
        if args[0] == 'apt-get' and 'install' in args:
            return self._install(args)
        if args[0] == 'dpkg' and '-i' in args:
            return self._install(args)
        if args[0] == 'systemctl':
            return self._systemctl(args)
        raise AssertionError(f'unexpected command: {args}')

    def _dpkg_query(self, args):
        if self.status in (None, 'none'):
            return _completed(args, 1, '', 'no packages found\n')
        return _completed(args, 0, f'{self.status} |{self.installed}\n', '')

    def _version_from_name(self, path):
        match = DEB_NAME.search(Path(str(path)).name)
        return match.group(1) if match else None

    def _dpkg_fields(self, args):
        path = args[2]
        fields = self.packages.get(str(path))
        if fields is None:
            fields = {'Package': 'qmi-supervisor',
                      'Version': self._version_from_name(path) or self.installed,
                      'Architecture': 'all'}
        names = args[3:] or list(fields)
        return _completed(args, 0, ''.join(f'{name}: {fields[name]}\n'
                                           for name in names if name in fields), '')

    def _dpkg_extract(self, args):
        tree = Path(args[3])
        from modules.update.verify import REQUIRED_FILES
        for name in REQUIRED_FILES:
            if self.incomplete and name == 'usr/local/sbin/qmi-supervisor-update':
                continue
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('synthetic package file\n')
        return _completed(args, 0, '', '')

    def _gpgv(self, args):
        def status(keyword, *rest):
            return '[GNUPG:] ' + ' '.join((keyword,) + rest) + '\n'
        base = status('NEWSIG') + status('KEY_CONSIDERED', self.fingerprint, '0')
        valid = status('VALIDSIG', self.fingerprint, '2026-11-20', '1234567890', '0', '4', '0', '1',
                       '10', '00', self.fingerprint)
        if self.gpgv == 'good':
            return _completed(args, 0, base + status('GOODSIG', 'ABCD1234', 'test key') + valid
                              + status('TRUST_ULTIMATE', '0', 'pgp'), '')
        if self.gpgv == 'bad':
            return _completed(args, 1, status('BADSIG', 'ABCD1234', 'test key'), '')
        if self.gpgv == 'nokey':
            return _completed(args, 2, status('ERRSIG', 'ABCD1234', '1', '', '0', '1', '')
                              + status('NO_PUBKEY', 'ABCD1234'), '')
        if self.gpgv == 'expired':
            return _completed(args, 0, base + status('GOODSIG', 'ABCD1234', 'test key')
                              + status('EXPKEYSIG', 'ABCD1234', 'test key') + valid, '')
        raise AssertionError(f'unknown gpgv mode: {self.gpgv}')

    def _install(self, args):
        version = self._version_from_name(args[-1]) or self.installed
        mode = self.install_modes.pop(0) if self.install_modes else self.install_mode
        if mode == 'timeout':
            raise subprocess.TimeoutExpired(args, 600)
        if mode == 'lock':
            return _completed(args, 100, '',
                              'E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1\n')
        if mode == 'error':
            return _completed(args, 100, '', (self.install_stderr or 'E: synthetic failure') + '\n')
        if mode == 'broken':
            self.previous_version, self.installed, self.status = self.installed, version, 'iF'
            self._restarted(version)
            return _completed(args, 100, '', 'E: synthetic broken package\n')
        self.previous_version, self.installed, self.status = self.installed, version, 'ii'
        self._restarted(version)
        return _completed(args, 0, '', '')

    def _restarted(self, version):
        self.main_pid += 1
        self.started_monotonic = self.clock.monotonic() + 0.5
        self.last_install_version = version
        self.online_since = self.clock.monotonic()
        knobs = (self.after_install_steps.pop(0) if self.after_install_steps
                 else self.after_install)
        for name in ('service', 'web', 'online'):
            if name in knobs:
                setattr(self, name, knobs[name])
        if 'online' in knobs:
            self.online = tuple(knobs['online'])
        self.online_delay = int(knobs.get('online_delay', 0) or 0)
        self.refresh()
        if self.install_hook is not None:
            self.install_hook(self, version)

    def _systemctl(self, args):
        command = args[1] if len(args) > 1 else ''
        if command == 'show':
            active = 'active' if self.service == 'active' else 'inactive'
            substate = 'running' if self.service == 'active' else 'dead'
            values = {'ActiveState': active, 'SubState': substate,
                      'MainPID': self.main_pid if self.service == 'active' else 0,
                      'NRestarts': self.restarts}
            if '--value' in args:
                name = args[args.index('-p') + 1] if '-p' in args else 'ActiveState'
                return _completed(args, 0, f'{values.get(name, "")}\n', '')
            return _completed(args, 0,
                              ''.join(f'{name}={value}\n' for name, value in values.items()), '')
        if command == 'is-active':
            return _completed(args, 0 if self.service == 'active' else 3,
                              ('active' if self.service == 'active' else 'inactive') + '\n', '')
        if command == 'stop':
            self.service = 'inactive'
            self.refresh()
            return _completed(args, 0, '', '')
        if command == 'start':
            if args[-1] in ('qmi-supervisor.service',) or '--no-block' not in args:
                self.service = 'active'
                self.refresh()
            return _completed(args, 0, '', '')
        raise AssertionError(f'unexpected systemctl command: {args}')
