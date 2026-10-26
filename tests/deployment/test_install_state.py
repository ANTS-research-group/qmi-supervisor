"""Private installation receipts retain ownership across failure and retry."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from modules.deployment import install_state
from tests.deployment.test_provisioning import FakeRunner


class InstallStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qmi-install-state-tests-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = SimpleNamespace(manifest=self.root/'qmi-supervisor-install/manifest.json')
        self.state = install_state.new_state(
            {'active': True, 'enabled': True, 'masked': False, 'changed': False},
            {'existed': False, 'content': None, 'mode': 0o644, 'changed': False})

    def test_receipt_roundtrips_with_private_permissions_and_missing_receipt_is_none(self):
        self.assertIsNone(install_state.load(self.paths))
        self.state['packages'] = ['libqmi-glib5:arm64']
        install_state.save(self.paths, self.state)
        self.assertEqual(install_state.load(self.paths), self.state)
        self.assertEqual(self.paths.manifest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.manifest.parent.stat().st_mode & 0o777, 0o700)

    def test_invalid_version_schema_or_package_name_is_rejected(self):
        self.paths.manifest.parent.mkdir()
        invalid = []
        for change in ({'version': 2}, {'version': True}, {'packages': ['--remove-all']},
                       {'packages': ['python3; touch /tmp/unowned']},
                       {'data_directories': [{'path': '../outside', 'existed': False}]},
                       {'completed': 'yes'}):
            invalid.append(dict(self.state, **change))
        invalid.extend(({}, [], None))
        for value in invalid:
            with self.subTest(value=value):
                self.paths.manifest.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    install_state.load(self.paths)

    def test_receipt_and_parent_symlinks_are_never_followed(self):
        outside = self.root/'unrelated.json'
        outside.write_text(json.dumps(self.state))
        self.paths.manifest.parent.mkdir()
        self.paths.manifest.symlink_to(outside)
        for operation in (lambda: install_state.load(self.paths),
                          lambda: install_state.save(self.paths, self.state)):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(json.loads(outside.read_text()), self.state)
        self.paths.manifest.unlink()
        self.paths.manifest.parent.rmdir()
        directory = self.root/'outside-directory'
        directory.mkdir()
        self.paths.manifest.parent.symlink_to(directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            install_state.save(self.paths, self.state)
        self.assertEqual(list(directory.iterdir()), [])

    def test_inventory_keeps_multiarch_and_excludes_removed_packages(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0,
            'python3\tinstall ok installed\nlibqmi-glib5:arm64\tinstall ok installed\n'
            'old-package\tdeinstall ok config-files\n'))
        self.assertEqual(install_state.installed_packages(runner), {'python3', 'libqmi-glib5:arm64'})
        arguments = runner.call_args.args[0]
        self.assertEqual(arguments[:2], ['dpkg-query', '-W'])
        self.assertIn('${binary:Package}', arguments[2])
        self.assertIn('${Status}', arguments[2])

    def test_inventory_includes_present_partial_packages_and_held_packages(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0,
            'partial:arm64\tinstall ok unpacked\nheld\thold ok installed\n'
            'configuring\tinstall ok half-configured\nremoved\tdeinstall ok config-files\n'))
        self.assertEqual(install_state.installed_packages(runner), {'partial:arm64', 'held', 'configuring'})

    def test_repairing_preexisting_partial_package_or_config_files_does_not_claim_it(self):
        runner = FakeRunner(packages=set(), residual_packages={'old-config'},
                            package_statuses={'preexisting-partial': 'install ok unpacked'})
        with install_state.record_packages(self.paths, self.state, runner):
            runner.package_statuses.clear()
            runner.residual_packages.clear()
            runner.packages.update({'preexisting-partial', 'old-config'})
            runner.package_statuses['new-unpacked:arm64'] = 'install ok unpacked'
        self.assertEqual(install_state.load(self.paths)['packages'], ['new-unpacked:arm64'])

    def test_invalid_inventory_cannot_become_package_ownership(self):
        for output in ('python3', '--danger\tinstall ok installed\n',
                       'python3;echo secret\tinstall ok installed\n'):
            with self.subTest(output=output):
                runner = Mock(return_value=subprocess.CompletedProcess([], 0, output))
                with self.assertRaises(ValueError):
                    install_state.installed_packages(runner)

    def test_partial_apt_failure_is_recorded_and_unrelated_later_packages_are_not_claimed(self):
        runner = FakeRunner(packages={'preexisting'})
        with self.assertRaisesRegex(RuntimeError, 'APT failed'):
            with install_state.record_packages(self.paths, self.state, runner):
                self.assertTrue(self.paths.manifest.exists())  # saved before a transaction can fail
                runner.packages.add('new-dependency:arm64')
                raise RuntimeError('APT failed after unpack')
        self.assertEqual(install_state.load(self.paths)['packages'], ['new-dependency:arm64'])
        runner.packages.add('installed-by-someone-else')
        with install_state.record_packages(self.paths, self.state, runner):
            runner.packages.add('second-dependency')
        self.assertEqual(set(install_state.load(self.paths)['packages']),
                         {'new-dependency:arm64', 'second-dependency'})

    def test_bootstrap_uses_immediate_before_after_pair_and_keeps_receipt(self):
        self.paths.manifest.parent.mkdir()
        before = self.paths.manifest.parent/'bootstrap-packages.before'
        after = self.paths.manifest.parent/'bootstrap-packages.after'
        before.write_text('preexisting\tinstall ok installed\n')
        after.write_text('preexisting\tinstall ok installed\npython3\tinstall ok installed\n'
                         'python3-minimal:arm64\tinstall ok installed\n')
        self.assertTrue(install_state.consume_bootstrap(self.paths, self.state))
        self.assertEqual(set(install_state.load(self.paths)['packages']), {'python3', 'python3-minimal:arm64'})
        self.assertFalse(before.exists())
        self.assertFalse(after.exists())
        self.assertFalse(install_state.consume_bootstrap(self.paths, self.state))

    def test_incomplete_bootstrap_snapshot_does_not_guess_from_current_system(self):
        self.paths.manifest.parent.mkdir()
        before = self.paths.manifest.parent/'bootstrap-packages.before'
        before.write_text('preexisting\tinstall ok installed\n')
        with self.assertRaises(ValueError):
            install_state.consume_bootstrap(self.paths, self.state)
        self.assertTrue(before.exists())
        self.assertEqual(self.state['packages'], [])

    def test_capture_directory_keeps_original_existence_on_reconfiguration(self):
        fresh = self.root/'fresh-captures'
        existing = self.root/'existing-captures'
        existing.mkdir()
        install_state.add_data_directory(self.state, fresh)
        install_state.add_data_directory(self.state, existing)
        fresh.mkdir()
        install_state.add_data_directory(self.state, fresh)
        self.assertEqual(self.state['data_directories'],
                         [{'path': str(fresh), 'existed': False}, {'path': str(existing), 'existed': True}])


if __name__ == '__main__':
    unittest.main()
