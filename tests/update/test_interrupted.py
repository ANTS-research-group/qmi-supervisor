"""Detected interruptions, the cached-package repair and the forbidden `dpkg -a`."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from modules.runtime.settings import Settings, UpdateSettings
from modules.update import health, history, install, units
from modules.update.state import UpdatePaths, new_state, write_state
from tests.update.fakes import FakeClock, FakeUpdateHost

NOW = datetime(2026, 11, 21, 3, 10, 2, tzinfo=timezone.utc)
A, B = '1.0.0-1', '1.1.0-1'


class InterruptedTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = UpdatePaths(
            config=root / 'etc/qmi-supervisor.ini',
            state_dir=root / 'var/lib/qmi-supervisor/update',
            lock=root / 'run/qmi-supervisor-update.lock',
            requests=root / 'run/qmi-supervisor-update',
            runtime=root / 'run/qmi-supervisor',
            keyring=root / 'keys/release-signing.gpg')
        self.paths.config.parent.mkdir(parents=True)
        self.paths.config.write_text('[web]\nenabled = true\n')
        self.clock = FakeClock(NOW)

    def host(self, *, installed=A, status='ii'):
        return FakeUpdateHost(self.paths.runtime, self.clock, installed=installed, status=status,
                              online=())

    def cache_package(self, version):
        directory = self.paths.cache / version
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        deb = directory / f'qmi-supervisor_{version}_all.deb'
        deb.write_bytes(f'package {version}\n'.encode())
        os.chmod(deb, 0o600)
        digest = hashlib.sha256(deb.read_bytes()).hexdigest()
        sums = directory / 'SHA256SUMS'
        sums.write_bytes(f'{digest}  {deb.name}\n'.encode())
        os.chmod(sums, 0o600)
        verified = {'schema_version': 1, 'version': version, 'sha256': digest,
                    'size': deb.stat().st_size, 'signed_by': None,
                    'verified_at_utc': (NOW - timedelta(hours=1)).isoformat()}
        (directory / 'verified.json').write_text(json.dumps(verified))
        return directory

    def transaction(self, *, step='health', from_version=A, to_version=B, reason='manual',
                    rollback_available=True):
        return {
            'schema_version': 1, 'id': 'a' * 32, 'request_id': None,
            'from': from_version, 'to': to_version, 'reason': reason, 'step': step,
            'steps': [{'id': name, 'state': 'pending'} for name in history.STEPS],
            'started_at_utc': (NOW - timedelta(minutes=5)).isoformat(),
            'updated_at_utc': NOW.isoformat(), 'wait_until_utc': None,
            'pid': 999999, 'boot_id': 'boot-1', 'unit': units.install_unit(to_version),
            'rollback_available': rollback_available, 'signed_by': None,
            'online_before': [], 'web_before': False,
        }

    def state_with(self, tx, *, installed=A):
        document = new_state(installed, NOW)
        document['in_progress'] = tx
        write_state(self.paths.state, document, now=NOW)
        return document

    def reconcile(self, document, host):
        return install.reconcile_interrupted(self.paths, document, runner=host,
                                             clock=self.clock.monotonic,
                                             utcnow=self.clock.utcnow)


class ReconcileTests(InterruptedTestCase):
    def test_an_interrupted_transaction_is_recorded_and_cleared(self):
        host = self.host()
        document = self.state_with(self.transaction(step='health'))
        record = self.reconcile(document, host)

        self.assertEqual((record['result'], record['code']), ('interrupted', 'interrupted'))
        self.assertEqual(record['step_failed'], 'health')
        self.assertEqual(record['from'], A)
        self.assertEqual(record['to'], B)
        lines = [json.loads(line) for line in self.paths.history.read_text().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['result'], 'interrupted')
        self.assertIsNone(document['in_progress'])
        self.assertEqual(document['last_result']['id'], record['id'])
        self.assertFalse(document.get('repair'))

    def test_a_package_that_is_not_ii_needs_repair_from_the_cache(self):
        self.cache_package(A)
        host = self.host(installed=B, status='iF')
        document = self.state_with(self.transaction(step='install', to_version=B),
                                   installed=B)
        self.reconcile(document, host)
        repair = document['repair']
        self.assertTrue(repair['needed'])
        self.assertEqual(repair['status'], 'iF')
        self.assertEqual(repair['target'], A)
        self.assertFalse(repair['attempted'])

    def test_repair_starts_the_unit_exactly_once(self):
        self.cache_package(A)
        host = self.host(installed=B, status='iF')
        document = self.state_with(self.transaction(step='install', to_version=B),
                                   installed=B)
        self.reconcile(document, host)
        self.assertTrue(install.start_repair_if_needed(
            self.paths, document, runner=host, utcnow=self.clock.utcnow))
        starts = [call for call in host.calls if call[0] == 'systemctl' and 'start' in call]
        self.assertEqual(starts, [['systemctl', 'start', '--no-block', units.install_unit(A)]])
        request = units.request_path(self.paths, A)
        self.assertTrue(request.exists())
        self.assertEqual(json.loads(request.read_text())['reason'], 'repair')
        self.assertTrue(document['repair']['attempted'])
        self.assertFalse(install.start_repair_if_needed(
            self.paths, document, runner=host, utcnow=self.clock.utcnow))
        self.assertEqual(len([call for call in host.calls
                              if call[0] == 'systemctl' and 'start' in call]), 1)

    def test_an_interrupted_rollback_targets_the_old_version(self):
        self.cache_package(A)
        host = self.host(installed=B, status='ii')
        document = self.state_with(self.transaction(step='rollback', to_version=B),
                                   installed=B)
        self.reconcile(document, host)
        self.assertEqual(document['repair']['target'], A)

    def test_no_cached_package_warns_once_and_repairs_nothing(self):
        host = self.host(installed=B, status='iF')
        document = self.state_with(self.transaction(step='install', to_version=B),
                                   installed=B)
        self.reconcile(document, host)
        self.assertIsNone(document['repair']['target'])
        with self.assertLogs('modules.update.install', level='WARNING') as logs:
            self.assertFalse(install.start_repair_if_needed(
                self.paths, document, runner=host, utcnow=self.clock.utcnow))
        self.assertEqual(len(logs.output), 1)
        self.assertFalse(install.start_repair_if_needed(
            self.paths, document, runner=host, utcnow=self.clock.utcnow))
        self.assertEqual([call for call in host.calls if call[0] == 'systemctl'], [])


class RepairTransactionTests(InterruptedTestCase):
    def run_repair(self, host, *, modes):
        host.install_modes = list(modes)
        self.cache_package(A)
        # require_signature stays at its default (true): a repair only checks a
        # signature that is cached, never demands one the interrupted install lacks.
        settings = Settings(update=UpdateSettings(auto_rollback=True, allow_downgrade=False))
        document = new_state(B, NOW)
        document['repair'] = {'needed': True, 'status': 'iF', 'target': A, 'attempted': True,
                              'detected_at_utc': NOW.isoformat(), 'manual_command': None}
        write_state(self.paths.state, document, now=NOW)
        installer = install.Installer(self.paths, settings, runner=host,
                                      open_url=lambda *args, **kwargs: None,
                                      keyring=self.paths.keyring, clock=self.clock.monotonic,
                                      utcnow=self.clock.utcnow, sleep=self.clock.sleep)
        return installer.run(A, reason='repair')

    def test_apt_failure_then_dpkg_succeeds_and_clears_the_repair(self):
        host = self.host(installed=B, status='iF')
        record = self.run_repair(host, modes=['error', 'ok'])
        self.assertEqual(record['result'], 'success')
        self.assertEqual(record['installed_after'], A)
        self.assertEqual(host.status, 'ii')
        apt = [call for call in host.calls if call[0] == 'apt-get']
        self.assertEqual(len(apt), 1)
        self.assertIn('--allow-downgrades', apt[0])
        dpkg = [call for call in host.calls if call[0] == 'dpkg' and '-i' in call]
        self.assertEqual(len(dpkg), 1)
        document = json.loads(self.paths.state.read_text())
        self.assertIsNone(document['repair'])

    def test_both_installers_failing_is_repair_failed_with_a_manual_command(self):
        host = self.host(installed=B, status='iF')
        record = self.run_repair(host, modes=['error', 'error'])
        self.assertEqual((record['result'], record['code']), ('failed', 'repair_failed'))
        self.assertEqual(record['step_failed'], 'install')
        document = json.loads(self.paths.state.read_text())
        self.assertTrue(document['repair']['attempted'])
        self.assertIn('dpkg --configure -a', document['repair']['manual_command'])
        self.assertIn(str(self.paths.cache / A / f'qmi-supervisor_{A}_all.deb'),
                      document['repair']['manual_command'])

    def test_the_updater_never_runs_dpkg_configure_a(self):
        host = self.host(installed=B, status='iF')
        self.run_repair(host, modes=['error', 'ok'])
        # The fake raises AssertionError for such an argv, so the run above proves it.
        self.assertFalse(any('--configure' in call and '-a' in call for call in host.calls))


if __name__ == '__main__':
    unittest.main()
