"""Supervise enabled plugins: manifests, secrets, restart backoff and status.

The supervisor runs as root. It never imports plugin code; it starts the plugin
host, which drops to the unprivileged plugin user and execs the program. A
plugin that declares system capabilities is started as a transient systemd
unit with only those capabilities.
"""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from modules.runtime.bus import new_secret, write_secrets
from modules.runtime.plugins import (MANIFEST_DIR, OS_CAPABILITIES, load_manifests)
from modules.runtime.workers import Heartbeat

LOG = logging.getLogger(__name__)
MAX_PLUGINS = 16
RESTART_LIMIT = 60


class Running:
    def __init__(self, manifest, secret):
        self.manifest = manifest
        self.secret = secret
        self.process = None
        self.unit = None
        self.failures = 0
        self.restarts = 0
        self.next_start = 0.0
        self.last_error = None
        self.started_at = 0.0
        self.failed = False


def _settings_file(directory, manifest, user):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        import grp
        os.chown(directory, 0, grp.getgrnam('qmi-supervisor-ext').gr_gid)
    except (KeyError, OSError, ImportError):
        pass
    os.chmod(directory, 0o750)
    path = directory/(manifest.name + '.json')
    temporary = directory/('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(manifest.settings, sort_keys=True), encoding='utf-8')
    os.chmod(temporary, 0o400)
    try:
        import grp
        import pwd
        os.chown(temporary, pwd.getpwnam(user).pw_uid, grp.getgrnam('qmi-supervisor-ext').gr_gid)
    except (KeyError, OSError, ImportError):
        pass
    temporary.replace(path)
    return path


class PluginSupervisor:
    def __init__(self, config_path, runtime, stop, settings, *, manifest_dir=MANIFEST_DIR,
                 bus_socket='/run/qmi-supervisor-ext/bus.sock', user='qmi-supervisor-plugin',
                 group='qmi-supervisor-ext', popen=subprocess.Popen, clock=time.monotonic):
        self.runtime = Path(runtime)
        self.stop = stop
        self.settings = settings
        self.manifest_dir = Path(manifest_dir)
        self.bus_socket = str(bus_socket)
        self.settings_dir = Path(self.bus_socket).parent/'plugin-settings'
        self.user, self.group = user, group
        self.popen = popen
        self.clock = clock
        self.running = {}
        self.secrets = {}

    def _publish_secrets(self):
        write_secrets({name: {'secret': item.secret,
                              'capabilities': list(item.manifest.capabilities)}
                       for name, item in self.running.items()})
        self.secrets = {name: item.secret for name, item in self.running.items()}

    def _systemd_command(self, manifest, settings_path, secret):
        capabilities = [OS_CAPABILITIES[name] for name in manifest.os_capabilities]
        unit = 'qmi-plugin-' + manifest.name
        command = ['systemd-run', '--unit=' + unit, '--collect', '--quiet',
                   '--property=User=' + self.user, '--property=Group=' + self.group,
                   '--property=NoNewPrivileges=yes', '--property=ProtectSystem=strict',
                   '--property=PrivateTmp=yes', '--property=ProtectHome=yes',
                   '--property=RestrictSUIDSGID=yes',
                   '--setenv=QMI_SUPERVISOR_BUS=' + self.bus_socket,
                   '--setenv=QMI_SUPERVISOR_PLUGIN=' + manifest.name,
                   '--setenv=QMI_SUPERVISOR_PLUGIN_SECRET=' + secret,
                   '--setenv=QMI_SUPERVISOR_PLUGIN_SETTINGS=' + str(settings_path),
                   '--setenv=PYTHONPATH=/opt/qmi-supervisor:/opt/qmi-supervisor/sdk',
                   '--setenv=PATH=/usr/local/bin:/usr/bin:/bin']
        if capabilities:
            command += ['--property=AmbientCapabilities=' + ' '.join(capabilities),
                        '--property=CapabilityBoundingSet=' + ' '.join(capabilities)]
        command += [sys.executable, '-m', 'modules.runtime.plugin_host',
                    '--manifest', str(manifest.path), '--settings', str(settings_path),
                    '--bus', self.bus_socket, '--secret', secret,
                    '--user', self.user, '--group', self.group]
        return command

    def _start(self, manifest):
        item = self.running.get(manifest.name) or Running(manifest, new_secret())
        item.manifest = manifest
        if manifest.os_capabilities:
            item.unit = 'qmi-plugin-' + manifest.name
            subprocess.run(['systemctl', 'reset-failed', item.unit], capture_output=True,
                           check=False, timeout=5)
            subprocess.run(self._systemd_command(
                manifest, _settings_file(self.settings_dir, manifest, self.user), item.secret),
                capture_output=True, timeout=10, check=False)
            item.process = None
            item.started_at = self.clock()
            item.last_error = None
        else:
            settings_path = _settings_file(self.settings_dir, manifest, self.user)
            item.process = self.popen(
                [sys.executable, '-m', 'modules.runtime.plugin_host',
                 '--manifest', str(manifest.path), '--settings', str(settings_path),
                 '--bus', self.bus_socket, '--secret', item.secret,
                 '--user', self.user, '--group', self.group],
                stdin=subprocess.DEVNULL, start_new_session=True, cwd='/',
                env={'PATH': '/usr/local/bin:/usr/bin:/bin',
                     'PYTHONPATH': str(Path(__file__).resolve().parents[2])})
            item.started_at = self.clock()
            item.last_error = None
        item.failed = False
        self.running[manifest.name] = item
        self._publish_secrets()
        LOG.info('Started plugin %s', manifest.name)
        return item

    def _stop(self, name):
        item = self.running.pop(name, None)
        if item is None:
            return
        if item.unit:
            subprocess.run(['systemctl', 'stop', item.unit], capture_output=True, check=False, timeout=5)
        if item.process is not None and item.process.poll() is None:
            try:
                item.process.terminate()
                item.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    item.process.kill()
                except OSError:
                    pass
        self._publish_secrets()
        LOG.info('Stopped plugin %s', name)

    @staticmethod
    def _alive(item):
        if item.unit:
            state = subprocess.run(['systemctl', 'is-active', '--quiet', item.unit],
                                   check=False).returncode == 0
            return state
        return item.process is not None and item.process.poll() is None

    def reconcile(self, manifests):
        enabled = [manifest for manifest in manifests if manifest.enabled and manifest.valid][:MAX_PLUGINS]
        wanted = {manifest.name for manifest in enabled}
        for name in list(self.running):
            if name not in wanted:
                self._stop(name)
        now = self.clock()
        for manifest in enabled:
            item = self.running.get(manifest.name)
            if item is None:
                self._start(manifest)
                continue
            if self._alive(item):
                item.failed = False
                continue
            if not item.failed:
                item.failed = True
                item.failures += 1
                item.restarts += 1
                item.last_error = 'Plugin exited'
                item.next_start = now + min(2 * 2 ** min(item.failures, 5), RESTART_LIMIT)
                if item.unit:
                    subprocess.run(['systemctl', 'reset-failed', item.unit], capture_output=True,
                                   check=False, timeout=5)
                item.process = None
            if now >= item.next_start:
                self._start(manifest)

    def status(self, manifests):
        plugins = {}
        for manifest in manifests:
            item = self.running.get(manifest.name)
            entry = manifest.public()
            if not manifest.valid:
                entry.update(state='invalid', restarts=0, last_error=manifest.error)
            elif not manifest.enabled:
                entry.update(state='stopped', restarts=item.restarts if item else 0,
                             last_error=item.last_error if item else None)
            elif item is None:
                entry.update(state='pending', restarts=0, last_error=None)
            else:
                entry.update(state='running' if self._alive(item) else 'backoff',
                             restarts=item.restarts, last_error=item.last_error,
                             pid=item.process.pid if item.process is not None else None)
            plugins[manifest.name] = entry
        return plugins

    def run(self, heartbeat):
        while not self.stop.is_set():
            try:
                manifests = load_manifests(self.manifest_dir)
                self.reconcile(manifests)
                status = self.status(manifests)
            except Exception as error:
                LOG.exception('Plugin supervision failed')
                status = {'error': str(error)[:200]}
            heartbeat.beat(phase='RUNNING', plugins=status)
            self.stop.wait(1.0)
        for name in list(self.running):
            self._stop(name)


def run_plugins(config_path, runtime, worker_id, settings, stop, *, heartbeat=None,
                manifest_dir=MANIFEST_DIR, bus_socket='/run/qmi-supervisor-ext/bus.sock',
                user='qmi-supervisor-plugin', group='qmi-supervisor-ext'):
    runtime = Path(runtime)
    heartbeat = heartbeat or Heartbeat(runtime/'plugins.json', worker_id, settings)
    supervisor = PluginSupervisor(config_path, runtime, stop, settings,
                                  manifest_dir=manifest_dir, bus_socket=bus_socket,
                                  user=user, group=group)
    try:
        supervisor.run(heartbeat)
    finally:
        heartbeat.beat(force=True, phase='STOPPED')
        heartbeat.close()
    return 0
