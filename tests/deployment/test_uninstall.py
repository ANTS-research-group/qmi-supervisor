"""Exercise uninstall against a temporary installation and an in-memory host."""
from contextlib import redirect_stderr, redirect_stdout
import fcntl
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.deployment import install_state, provisioning, uninstall
from tests.deployment.test_provisioning import FakeRunner


class UninstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qmi-uninstall-tests-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = provisioning.Paths(
            source=self.root/'source', app=self.root/'opt/qmi-supervisor',
            config=self.root/'etc/qmi-supervisor.ini',
            unit=self.root/'etc/systemd/system/qmi-supervisor.service',
            command=self.root/'usr/local/sbin/qmi-supervisor-configure',
            state=self.root/'var/lib/qmi-supervisor',
            rules=self.root/'etc/udev/rules.d/85-qmi-supervisor.rules',
            runtime=self.root/'run/qmi-supervisor',
        )
        for path, content in (
            (self.paths.app/'qmi_supervisor.py', '# installed application'),
            (self.paths.config, '[invalid configuration'),
            (self.paths.unit, '[Service]\n'),
            (self.paths.command, '# configure wrapper'),
            (self.paths.uninstall_command, '# uninstall wrapper'),
            (self.paths.rules, provisioning.QMI_NETWORK_RULE),
            (self.paths.runtime/'status.json', '{}'),
            (self.paths.runtime/'web-sessions.json', '{}'),
            (self.paths.state/'modem_test/dynamic_stats_v2_20261021.csv', 'captured data'),
        ):
            self.write(path, content)
        self.runner = FakeRunner(active=True, enabled=True, manager_masked=True,
                                 packages={'python3', 'preexisting:amd64', 'qmi-dependency', 'libqmi-test:amd64'})
        self.output = StringIO()
        self.receipt = {
            'version': 1,
            'packages': ['qmi-dependency', 'libqmi-test:amd64'],
            'modem_manager': {'active': True, 'enabled': True, 'masked': False, 'changed': True},
            'network_rule': {'existed': False, 'content': None, 'mode': 0o644, 'changed': True},
            'artifacts': {'app_backups': [], 'config_backups': []},
            'data_directories': [{'path': str(self.paths.state), 'existed': False}],
            'completed': True,
        }
        install_state.save(self.paths, self.receipt)

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def invoke(self, *, purge=False, dry_run=False):
        args = SimpleNamespace(action='uninstall', purge=purge, dry_run=dry_run)
        with patch.object(uninstall.os, 'geteuid', return_value=0), \
                patch.object(uninstall, 'scan_devices', return_value=[]), redirect_stdout(self.output):
            return uninstall.uninstall(self.paths, args, runner=self.runner)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file() and not path.is_symlink()}

    def real_purge_calls(self):
        return [command for command in self.runner.calls if command[0] == 'apt-get'
                and 'purge' in command and '--simulate' not in command and '-s' not in command]

    def test_default_removes_application_but_preserves_captures_packages_and_receipt(self):
        before_packages = set(self.runner.packages)
        self.assertEqual(self.invoke(), 0)
        for path in (self.paths.app, self.paths.config, self.paths.unit, self.paths.command,
                     self.paths.uninstall_command, self.paths.rules):
            self.assertFalse(path.exists(), path)
        self.assertEqual((self.paths.state/'modem_test/dynamic_stats_v2_20261021.csv').read_text(), 'captured data')
        self.assertEqual(self.runner.packages, before_packages)
        self.assertTrue(self.paths.manifest.exists())
        self.assertFalse(self.runner.active)
        self.assertFalse(self.runner.enabled)
        self.assertFalse(any(command[0] == 'apt-get' for command in self.runner.calls))
        self.assertFalse(self.paths.runtime.exists())
        self.assertFalse(self.paths.lock.exists())  # no lock was created to inspect an absent lock

    def test_purge_removes_only_packages_introduced_by_installation(self):
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertEqual(self.runner.packages, {'python3', 'preexisting:amd64'})
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.manifest.exists())
        self.assertTrue(self.real_purge_calls())
        for command in self.runner.calls:
            self.assertNotIn('autoremove', command)
            if command[0] == 'apt-get':
                self.assertNotIn('python3', command)
                self.assertNotIn('preexisting:amd64', command)

    def test_shared_dependency_simulation_refuses_removing_unrelated_packages(self):
        self.runner.simulation_extras = {'preexisting:amd64'}
        with self.assertRaises((ValueError, RuntimeError)):
            self.invoke(purge=True)
        self.assertFalse(self.real_purge_calls())
        self.assertTrue(self.paths.app.is_dir())
        self.assertTrue(self.paths.manifest.exists())
        self.assertIn('preexisting:amd64', self.runner.packages)

    def test_original_modem_manager_and_udev_rule_are_restored(self):
        self.receipt['network_rule'] = {
            'existed': True, 'content': '# administrator rule\n', 'mode': 0o640, 'changed': True}
        install_state.save(self.paths, self.receipt)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.paths.rules.read_text(), '# administrator rule\n')
        self.assertEqual(self.paths.rules.stat().st_mode & 0o777, 0o640)
        self.assertTrue(self.runner.manager_active)
        self.assertTrue(self.runner.manager_enabled)
        self.assertFalse(self.runner.manager_masked)
        self.assertIn(('udevadm', 'control', '--reload-rules'), self.runner.calls)

    def test_previously_masked_modem_manager_remains_masked_and_inactive(self):
        self.receipt['modem_manager'] = {
            'active': False, 'enabled': False, 'masked': True, 'changed': True}
        install_state.save(self.paths, self.receipt)
        self.assertEqual(self.invoke(), 0)
        self.assertTrue(self.runner.manager_masked)
        self.assertFalse(self.runner.manager_active)
        self.assertFalse(self.runner.manager_enabled)
        self.assertNotIn(('systemctl', 'start', 'ModemManager.service'), self.runner.calls)

    def test_unchanged_manager_and_rule_are_left_as_found(self):
        self.receipt['modem_manager']['changed'] = False
        self.receipt['network_rule']['changed'] = False
        self.paths.rules.write_text('# unrelated existing rule\n')
        install_state.save(self.paths, self.receipt)
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.paths.rules.read_text(), '# unrelated existing rule\n')
        manager_mutations = [command for command in self.runner.calls
                             if command[0] == 'systemctl' and command[-1] == 'ModemManager.service'
                             and command[1] not in ('is-active', 'is-enabled', 'show', 'cat')]
        self.assertEqual(manager_mutations, [])

    def test_dry_run_never_writes_or_issues_mutating_commands(self):
        before = self.snapshot()
        packages = set(self.runner.packages)
        self.assertEqual(self.invoke(purge=True, dry_run=True), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.runner.packages, packages)
        for command in self.runner.calls:
            if command[0] == 'apt-get':
                self.assertTrue('--simulate' in command or '-s' in command, command)
            elif command[0] == 'systemctl':
                self.assertIn(command[1], ('is-active', 'is-enabled', 'show', 'cat'), command)
            else:
                self.assertEqual(command[0], 'dpkg-query', command)
        self.assertIn(str(self.paths.app), self.output.getvalue())

    def test_malformed_configuration_does_not_block_cli_uninstall(self):
        with patch.object(provisioning, 'Paths', return_value=self.paths), \
                patch.object(provisioning, 'candidate_settings', side_effect=AssertionError('INI must not be read')), \
                patch.object(provisioning, 'supported_system', side_effect=AssertionError('No OS install check')), \
                patch.object(provisioning, 'packages_for', side_effect=AssertionError('No package install plan')), \
                patch.object(provisioning, 'web_credentials', side_effect=AssertionError('No credentials')), \
                patch.object(uninstall, 'uninstall', return_value=0) as remove:
            self.assertEqual(provisioning.main(['uninstall']), 0)
        self.assertEqual(remove.call_args.args[0], self.paths)
        self.assertFalse(remove.call_args.args[1].purge)

    def test_purge_option_is_rejected_for_install_and_configure(self):
        for action in ('install', 'configure'):
            with self.subTest(action=action), patch.object(provisioning, 'Paths', return_value=self.paths), \
                    patch.object(provisioning, 'apply') as apply, \
                    patch.object(provisioning, 'candidate_settings', side_effect=AssertionError('No settings read')), \
                    redirect_stderr(self.output):
                with self.assertRaises(SystemExit):
                    provisioning.main([action, '--purge'])
                apply.assert_not_called()

    def test_install_only_options_cannot_trigger_uninstall_by_being_ignored(self):
        for flags in (('--doctor',), ('--skip-packages',), ('--no-start',), ('--start',),
                      ('--apn=internet',), ('--modem-manager', 'keep'), ('--interactive',),
                      ('--web-password-stdin',)):
            with self.subTest(flags=flags), patch.object(provisioning, 'Paths', return_value=self.paths), \
                    patch.object(provisioning, 'candidate_settings', side_effect=AssertionError('No settings read')), \
                    patch.object(uninstall, 'uninstall') as remove, redirect_stderr(self.output):
                with self.assertRaises(SystemExit):
                    provisioning.main(['uninstall', *flags])
                remove.assert_not_called()

    def test_cli_purge_dry_run_routes_both_options_without_reading_settings(self):
        with patch.object(provisioning, 'Paths', return_value=self.paths), \
                patch.object(provisioning, 'candidate_settings', side_effect=AssertionError('No settings read')), \
                patch.object(uninstall, 'uninstall', return_value=0) as remove:
            self.assertEqual(provisioning.main(['uninstall', '--purge', '--dry-run']), 0)
        self.assertTrue(remove.call_args.args[1].purge)
        self.assertTrue(remove.call_args.args[1].dry_run)

    def test_custom_preexisting_directory_removes_only_known_qmi_capture_files(self):
        directory = self.root/'shared captures'
        captures = directory/'modem_one'
        for name in ('static_stats_20261021.csv', 'dynamic_stats_v2_20261021.csv',
                     'dynamic_stats_20261020.csv', 'stats.json'):
            self.write(captures/name, 'owned capture')
        self.write(captures/'notes.txt', 'user data')
        self.write(captures/'dynamic_stats_custom.csv', 'user custom file')
        self.write(directory/'unrelated.csv', 'user data')
        self.write(directory/'another_project/stats.json', 'unrelated JSON')
        self.receipt['data_directories'].append({'path': str(directory), 'existed': True})
        install_state.save(self.paths, self.receipt)
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertEqual({item.name for item in captures.iterdir()}, {'notes.txt', 'dynamic_stats_custom.csv'})
        self.assertEqual((directory/'unrelated.csv').read_text(), 'user data')
        self.assertEqual((directory/'another_project/stats.json').read_text(), 'unrelated JSON')

    def test_capture_symlink_cannot_delete_files_outside_recorded_directory(self):
        directory = self.root/'shared'
        outside = self.root/'another-project'
        self.write(outside/'stats.json', 'unrelated data')
        directory.mkdir()
        (directory/'modem_link').symlink_to(outside, target_is_directory=True)
        self.receipt['data_directories'].append({'path': str(directory), 'existed': True})
        install_state.save(self.paths, self.receipt)
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertEqual((outside/'stats.json').read_text(), 'unrelated data')

    def test_purge_removes_only_recorded_project_backup_paths(self):
        app_backup = self.paths.app.with_name(self.paths.app.name + '.previous-20261021-120000-000001')
        config_backup = self.paths.config.with_name(self.paths.config.name + '.bak-20261021-120000-000001')
        unrelated = self.paths.app.with_name('other-application.previous-20261021-120000-000001')
        untracked = self.paths.app.with_name(self.paths.app.name + '.previous-untracked')
        self.write(app_backup/'old.py', 'old project')
        self.write(config_backup, '[old]\n')
        self.write(unrelated/'old.py', 'other application')
        self.write(untracked/'old.py', 'not in receipt')
        self.receipt['artifacts'] = {
            'app_backups': [str(app_backup)], 'config_backups': [str(config_backup)]}
        install_state.save(self.paths, self.receipt)
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertFalse(app_backup.exists())
        self.assertFalse(config_backup.exists())
        self.assertTrue(unrelated.is_dir())
        self.assertTrue(untracked.is_dir())

    def test_unresolved_bearer_checkpoint_prevents_deleting_installation(self):
        for checkpoint in (self.paths.checkpoint, self.paths.attach_checkpoint,
                           self.paths.runtime/'ownership/physical-ue/connection-state.json',
                           self.paths.runtime/'ownership/physical-ue/connection-state-attach.json',
                           self.paths.runtime/'ownership/physical-ue/migration.json'):
            with self.subTest(checkpoint=checkpoint):
                self.write(checkpoint, json.dumps({
                    'version': 1, 'identity': 'test', 'cid': 9, 'handle': 72, 'network': {}}))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.invoke()
                self.assertTrue(checkpoint.exists())
                self.assertTrue(self.paths.app.is_dir())
                self.assertTrue(self.paths.manifest.exists())
                checkpoint.unlink()

    def test_active_supervisor_lock_prevents_deleting_installation(self):
        self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.lock.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises((ValueError, RuntimeError, BlockingIOError)):
                self.invoke()
        self.assertTrue(self.paths.app.is_dir())
        self.assertTrue(self.paths.manifest.exists())

    def test_failed_service_stop_preserves_files_and_receipt_for_retry(self):
        failure = ('systemctl', 'stop', provisioning.SERVICE)
        self.runner.failures.add(failure)
        with self.assertRaises((ValueError, RuntimeError, subprocess.SubprocessError)):
            self.invoke(purge=True)
        self.assertTrue(self.paths.app.is_dir())
        self.assertTrue(self.paths.manifest.exists())
        self.assertFalse(self.real_purge_calls())
        self.runner.failures.clear()
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertFalse(self.paths.manifest.exists())

    def test_package_purge_failure_retains_receipt_and_can_be_retried(self):
        original = self.runner

        def fail_purge(args, **kwargs):
            if args[0] == 'apt-get' and 'purge' in args and '--simulate' not in args and '-s' not in args:
                original.calls.append(tuple(args))
                raise subprocess.CalledProcessError(100, args, stderr='simulated APT failure')
            return original(args, **kwargs)

        with patch.object(self, 'runner', side_effect=fail_purge):
            with self.assertRaises(subprocess.SubprocessError):
                self.invoke(purge=True)
        self.assertTrue(self.paths.manifest.exists())
        self.assertIn('qmi-dependency', self.runner.packages)
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertFalse(self.paths.manifest.exists())

    def test_failed_purge_with_only_package_configs_remaining_is_retried(self):
        original = self.runner
        owned = set(self.receipt['packages'])

        def half_purge(args, **kwargs):
            if args[0] == 'apt-get' and 'purge' in args and '--simulate' not in args and '-s' not in args:
                original.calls.append(tuple(args))
                original.packages.difference_update(owned)
                original.residual_packages.update(owned)
                raise subprocess.CalledProcessError(100, args, stderr='maintainer script failed')
            return original(args, **kwargs)

        with patch.object(self, 'runner', side_effect=half_purge):
            with self.assertRaises(subprocess.SubprocessError):
                self.invoke(purge=True)
        self.assertTrue(self.paths.manifest.exists())
        self.assertEqual(self.runner.residual_packages, owned)
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertEqual(self.runner.residual_packages, set())
        self.assertFalse(self.paths.manifest.exists())

    def test_deactivating_service_is_not_treated_as_stopped(self):
        original = self.runner

        def deactivating(args, **kwargs):
            result = original(args, **kwargs)
            if args[:2] == ['systemctl', 'show']:
                return subprocess.CompletedProcess(args, 0, 'deactivating\n', '')
            return result

        with patch.object(self, 'runner', side_effect=deactivating):
            with self.assertRaises((ValueError, RuntimeError)):
                self.invoke()
        self.assertTrue(self.paths.app.is_dir())
        self.assertTrue(self.paths.manifest.exists())

    def test_unknown_runtime_files_are_preserved(self):
        self.write(self.paths.runtime/'administrator-notes.txt', 'keep this')
        self.assertEqual(self.invoke(), 0)
        self.assertEqual((self.paths.runtime/'administrator-notes.txt').read_text(), 'keep this')
        self.assertFalse((self.paths.runtime/'status.json').exists())
        self.assertFalse((self.paths.runtime/'web-sessions.json').exists())

    def test_backup_receipt_cannot_claim_a_different_application(self):
        unrelated = self.root/'opt/other-project.previous-20261021-120000-000001'
        self.write(unrelated/'important.txt', 'unrelated')
        self.receipt['artifacts']['app_backups'] = [str(unrelated)]
        install_state.save(self.paths, self.receipt)
        with self.assertRaises(ValueError):
            self.invoke(purge=True)
        self.assertEqual((unrelated/'important.txt').read_text(), 'unrelated')
        self.assertTrue(self.paths.app.is_dir())
        self.assertFalse(self.real_purge_calls())

    def test_resolver_symlink_is_restored_after_package_maintainer_rewrites_it(self):
        self.receipt['resolver_config'] = {
            'existed': True, 'kind': 'symlink', 'target': '../run/resolver/stub.conf',
            'mode': 0o777, 'changed': True}
        install_state.save(self.paths, self.receipt)
        original = self.runner

        def rewrite_resolver(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == 'apt-get' and 'purge' in args and '--simulate' not in args and '-s' not in args:
                self.paths.resolver.unlink(missing_ok=True)
                self.write(self.paths.resolver, '# rewritten by maintainer script')
            return result

        with patch.object(self, 'runner', side_effect=rewrite_resolver):
            self.assertEqual(self.invoke(purge=True), 0)
        self.assertTrue(self.paths.resolver.is_symlink())
        self.assertEqual(str(self.paths.resolver.readlink()), '../run/resolver/stub.conf')

    def test_default_then_source_purge_and_repeated_purge_are_safe(self):
        self.assertEqual(self.invoke(), 0)
        self.assertTrue(self.paths.manifest.exists())
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertFalse(self.paths.manifest.exists())
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertEqual(self.runner.packages, {'python3', 'preexisting:amd64'})

    def test_legacy_install_does_not_guess_package_or_modem_manager_ownership(self):
        self.paths.manifest.unlink()
        self.write(self.paths.state/'notes.txt', 'user notes')
        before_packages = set(self.runner.packages)
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertEqual(self.runner.packages, before_packages)
        self.assertFalse(any(command[0] == 'apt-get' for command in self.runner.calls))
        self.assertTrue(self.runner.manager_masked)
        self.assertFalse((self.paths.state/'modem_test/dynamic_stats_v2_20261021.csv').exists())
        self.assertEqual((self.paths.state/'notes.txt').read_text(), 'user notes')
        self.assertIn('receipt', self.output.getvalue().lower())

    def test_current_untracked_capture_directory_is_preserved_by_default_and_recorded_for_later_purge(self):
        directory = self.root/'manually configured captures'
        self.write(directory/'modem_manual/dynamic_stats_v2_20261021.csv', 'captured data')
        self.write(directory/'modem_manual/notes.txt', 'administrator notes')
        self.paths.config.write_text('[telemetry]\ndirectory = '+str(directory)+'\n')
        self.assertEqual(self.invoke(), 0)
        self.assertTrue((directory/'modem_manual/dynamic_stats_v2_20261021.csv').exists())
        receipt = install_state.load(self.paths)
        self.assertIn({'path': str(directory), 'existed': True}, receipt['data_directories'])
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertFalse((directory/'modem_manual/dynamic_stats_v2_20261021.csv').exists())
        self.assertEqual((directory/'modem_manual/notes.txt').read_text(), 'administrator notes')

    def test_relative_capture_directory_inside_app_is_preserved_by_default(self):
        directory = self.paths.app/'captures'
        self.write(directory/'modem_manual/stats.json', 'captured data')
        self.write(directory/'notes.txt', 'administrator notes')
        self.paths.config.write_text('[telemetry]\ndirectory = captures\n')
        self.assertEqual(self.invoke(), 0)
        self.assertFalse((self.paths.app/'qmi_supervisor.py').exists())
        self.assertEqual((directory/'modem_manual/stats.json').read_text(), 'captured data')
        self.assertEqual((directory/'notes.txt').read_text(), 'administrator notes')
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertFalse((directory/'modem_manual/stats.json').exists())
        self.assertEqual((directory/'notes.txt').read_text(), 'administrator notes')

    def test_fresh_owned_nested_capture_tree_is_removed_on_purge(self):
        directory = self.paths.app/'fresh-captures'
        self.write(directory/'modem_manual/stats.json', 'captured data')
        self.receipt['data_directories'].append({'path': str(directory), 'existed': False})
        install_state.save(self.paths, self.receipt)
        self.assertEqual(self.invoke(purge=True), 0)
        self.assertFalse(directory.exists())
        self.assertFalse(self.paths.app.exists())

    def test_capture_directory_equal_to_or_containing_app_blocks_before_mutation(self):
        for directory in (self.paths.app, self.paths.app.parent):
            with self.subTest(directory=directory):
                self.paths.config.write_text('[telemetry]\ndirectory = '+str(directory)+'\n')
                self.runner.calls.clear()
                with self.assertRaises(ValueError):
                    self.invoke(purge=True)
                self.assertTrue(self.runner.active)
                self.assertTrue((self.paths.app/'qmi_supervisor.py').exists())
                self.assertTrue(self.paths.config.exists())
                self.assertNotIn(('systemctl', 'stop', provisioning.SERVICE), self.runner.calls)

    def test_only_exact_orphaned_checkpoint_temporary_files_are_removed(self):
        for path in (self.paths.checkpoint.with_suffix('.tmp'), self.paths.attach_checkpoint.with_suffix('.tmp')):
            self.write(path, '{}')
        unrelated = self.paths.runtime.parent/'another-project-state.tmp'
        self.write(unrelated, 'keep')
        self.assertEqual(self.invoke(), 0)
        self.assertFalse(self.paths.checkpoint.with_suffix('.tmp').exists())
        self.assertFalse(self.paths.attach_checkpoint.with_suffix('.tmp').exists())
        self.assertEqual(unrelated.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
