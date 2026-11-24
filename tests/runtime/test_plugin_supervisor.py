"""Plugin supervisor: start, backoff, disable and the systemd capability path."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from modules.runtime.plugin_supervisor import PluginSupervisor
from modules.runtime.plugins import load_manifests
from modules.runtime.settings import Settings


class FakeProcess:
    def __init__(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        self.pid, self.returncode, self.terminated = 4321, None, False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FakePopen:
    def __init__(self):
        self.instances = []

    def __call__(self, command, **kwargs):
        process = FakeProcess(command, **kwargs)
        self.instances.append(process)
        return process


MANIFEST = """[plugin]
name = demo
description = Demo plugin
exec = {exec}
enabled = true
capabilities = read:events
os_capabilities = {caps}

[settings]
url = https://example.invalid/hook
"""


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root/'plugins.d'
        self.directory.mkdir()
        self.executable = self.root/'plugin.py'
        self.executable.write_text('#!/bin/sh\nexit 0\n')
        self.executable.chmod(0o755)
        self.now = 0.0
        self.stop = threading.Event()
        self.popen = FakePopen()

    def manifest(self, caps=''):
        path = self.directory/'demo.ini'
        path.write_text(MANIFEST.format(exec=self.executable, caps=caps))
        return path

    def supervisor(self):
        return PluginSupervisor(self.root/'settings.ini', self.root/'run', self.stop,
                                Settings(), manifest_dir=self.directory,
                                bus_socket=self.root/'bus.sock', user='', group='',
                                popen=self.popen, clock=lambda: self.now)

    def test_an_enabled_plugin_starts_and_is_reported_running(self):
        path = self.manifest()
        supervisor = self.supervisor()
        with patch('modules.runtime.plugin_supervisor.write_secrets') as secrets:
            supervisor.reconcile(load_manifests(self.directory, require_root=False))
        self.assertEqual(len(self.popen.instances), 1)
        command = self.popen.instances[0].command
        self.assertIn('modules.runtime.plugin_host', command)
        self.assertIn(str(path), command)
        self.assertEqual(secrets.call_count, 1)
        self.assertIn('demo', secrets.call_args.args[0])
        manifests = load_manifests(self.directory, require_root=False)
        self.assertEqual(supervisor.status(manifests)['demo']['state'], 'running')

    def test_an_exited_plugin_restarts_with_backoff(self):
        self.manifest()
        supervisor = self.supervisor()
        manifests = load_manifests(self.directory, require_root=False)
        with patch('modules.runtime.plugin_supervisor.write_secrets'):
            supervisor.reconcile(manifests)
            process = self.popen.instances[0]
            process.returncode = 1
            supervisor.reconcile(manifests)
            self.assertEqual(len(self.popen.instances), 1, 'the first retry waits')
            status = supervisor.status(manifests)['demo']
            self.assertEqual((status['state'], status['restarts']), ('backoff', 1))
            self.now += 5
            supervisor.reconcile(manifests)
            self.assertEqual(len(self.popen.instances), 2)

    def test_disabling_the_manifest_stops_the_plugin(self):
        path = self.manifest()
        supervisor = self.supervisor()
        manifests = load_manifests(self.directory, require_root=False)
        with patch('modules.runtime.plugin_supervisor.write_secrets'):
            supervisor.reconcile(manifests)
            process = self.popen.instances[0]
            path.write_text(path.read_text().replace('enabled = true', 'enabled = false'))
            supervisor.reconcile(load_manifests(self.directory, require_root=False))
        self.assertTrue(process.terminated)

    def test_system_capabilities_use_a_transient_unit_with_only_those_capabilities(self):
        self.manifest(caps='net_raw')
        supervisor = self.supervisor()
        manifests = load_manifests(self.directory, require_root=False)
        completed = type('Completed', (), {'returncode': 0})()
        with patch('modules.runtime.plugin_supervisor.write_secrets'), \
                patch('modules.runtime.plugin_supervisor.subprocess.run',
                      return_value=completed) as runner:
            supervisor.reconcile(manifests)
        commands = [call.args[0] for call in runner.call_args_list]
        systemd = next(command for command in commands if command[0] == 'systemd-run')
        self.assertIn('--property=AmbientCapabilities=CAP_NET_RAW', systemd)
        self.assertIn('--property=CapabilityBoundingSet=CAP_NET_RAW', systemd)
        self.assertIn('--property=User=', ' '.join(systemd))
        self.assertTrue(any('--property=NoNewPrivileges=yes' == part
                            for part in systemd if part.startswith('--property=')))
