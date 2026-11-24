"""Plugin host: environment, limits, privilege drop and exec preparation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from modules.runtime import plugin_host


class PluginHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.executable = self.root/'plugin.py'
        self.executable.write_text('#!/bin/sh\nexit 0\n')
        self.executable.chmod(0o755)
        self.manifest = self.root/'plugins.d'/'demo.ini'
        self.manifest.parent.mkdir()
        self.manifest.write_text(f"""[plugin]
name = demo
description = Demo
exec = {self.executable}
enabled = true
capabilities = read:events
[settings]
url = https://example.invalid/hook
""")
        self.settings = self.root/'settings.json'
        self.settings.write_text(json.dumps({'url': 'https://example.invalid/hook'}))
        self.settings.chmod(0o600)

    def test_environment_is_limited_to_the_documented_variables(self):
        env = plugin_host.environment('demo', '/run/bus.sock', 'secret', self.settings)
        self.assertEqual(set(env), {'PATH', 'LANG', 'PYTHONPATH', 'QMI_SUPERVISOR_BUS',
                                    'QMI_SUPERVISOR_PLUGIN', 'QMI_SUPERVISOR_PLUGIN_SECRET',
                                    'QMI_SUPERVISOR_PLUGIN_SETTINGS'})
        self.assertEqual(env['PYTHONPATH'], '/opt/qmi-supervisor/sdk')
        self.assertEqual(env['QMI_SUPERVISOR_PLUGIN'], 'demo')

    def test_limits_and_privilege_drop_happen_in_order(self):
        calls = []
        with patch('modules.runtime.plugin_host.resource.setrlimit',
                   side_effect=lambda *args: calls.append(('rlimit', args[0]))), \
                patch('modules.runtime.plugin_host.os.nice',
                      side_effect=lambda value: calls.append(('nice', value))), \
                patch('modules.runtime.plugin_host.os.geteuid', return_value=0), \
                patch('modules.runtime.plugin_host._identity', return_value=(610, 611)), \
                patch('modules.runtime.plugin_host.os.setgroups',
                      side_effect=lambda *args: calls.append(('groups',))), \
                patch('modules.runtime.plugin_host.os.setgid',
                      side_effect=lambda value: calls.append(('gid', value))), \
                patch('modules.runtime.plugin_host.os.setuid',
                      side_effect=lambda value: calls.append(('uid', value))):
            plugin_host.prepare('qmi-supervisor-plugin', 'qmi-supervisor-ext')
        self.assertEqual(calls[-3:], [('groups',), ('gid', 611), ('uid', 610)])
        self.assertIn(('nice', 10), calls)
        self.assertEqual(sum(1 for item in calls if item[0] == 'rlimit'), 2)

    def test_a_non_root_host_skips_setuid(self):
        with patch('modules.runtime.plugin_host.resource.setrlimit'), \
                patch('modules.runtime.plugin_host.os.nice'), \
                patch('modules.runtime.plugin_host.os.geteuid', return_value=1000), \
                patch('modules.runtime.plugin_host.os.setuid') as drop:
            plugin_host.prepare()
        drop.assert_not_called()

    def test_the_program_is_executed_without_a_shell(self):
        with patch('modules.runtime.plugin_host.os.geteuid', return_value=1000), \
                patch('modules.runtime.plugin_host.resource.setrlimit'), \
                patch('modules.runtime.plugin_host.os.nice'), \
                patch('modules.runtime.plugin_host.os.execve') as execute:
            code = plugin_host.main(['--manifest', str(self.manifest),
                                     '--settings', str(self.settings),
                                     '--bus', '/run/bus.sock', '--secret', 'secret'])
        self.assertEqual(code, 0)
        argv, env = execute.call_args.args[1], execute.call_args.args[2]
        self.assertEqual(argv, [str(self.executable)])
        self.assertEqual(env['QMI_SUPERVISOR_PLUGIN'], 'demo')
        self.assertEqual(env['QMI_SUPERVISOR_BUS'], '/run/bus.sock')

    def test_invalid_manifests_and_settings_are_refused_before_exec(self):
        self.manifest.write_text(self.manifest.read_text().replace('enabled = true', 'enabled = maybe'))
        with patch('modules.runtime.plugin_host.os.geteuid', return_value=1000), \
                patch('modules.runtime.plugin_host.os.execve') as execute, \
                patch('modules.runtime.plugin_host.logging.error'):
            code = plugin_host.main(['--manifest', str(self.manifest),
                                     '--settings', str(self.settings), '--bus', '/run/x',
                                     '--secret', 's'])
        self.assertEqual(code, 2)
        execute.assert_not_called()
        self.settings.write_text('["not", "an", "object"]')
        with patch('modules.runtime.plugin_host.os.geteuid', return_value=1000), \
                patch('modules.runtime.plugin_host.os.execve') as execute, \
                patch('modules.runtime.plugin_host.logging.error'):
            code = plugin_host.main(['--manifest', str(self.manifest),
                                     '--settings', str(self.settings), '--bus', '/run/x',
                                     '--secret', 's'])
        self.assertEqual(code, 2)
        execute.assert_not_called()
