"""The install/rollback command line: requester and --from-unit modes."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from modules.runtime import version as runtime_version
from modules.runtime.settings import Settings, UpdateSettings
from modules.update import cli, history, install, lock, releases, units
from modules.update.state import UpdatePaths, new_state, write_state
from tests.update.fakes import FakeClock, FakeOpener, FakeUpdateHost
from tests.update.fixtures import release_item
from tests.update.test_install import asset_release

NOW = datetime(2026, 11, 21, 3, 10, 2, tzinfo=timezone.utc)
A, B = '1.0.0-1', '1.1.0-1'
ROOT = Path(__file__).resolve().parents[2]


class CliInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = UpdatePaths(
            config=root / 'etc/qmi-supervisor.ini',
            state_dir=root / 'var/lib/qmi-supervisor/update',
            token=root / 'etc/qmi-supervisor-update-token',
            lock=root / 'run/qmi-supervisor-update.lock',
            requests=root / 'run/qmi-supervisor-update',
            runtime=root / 'run/qmi-supervisor',
            keyring=root / 'keys/release-signing.gpg')
        self.paths.config.parent.mkdir(parents=True)
        self.paths.config.write_text('[update]\nmode = notify\n')
        self.paths.keyring.parent.mkdir(parents=True)
        self.paths.keyring.write_bytes(b'binary keyring')
        self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        self.clock = FakeClock(NOW)
        self.host = FakeUpdateHost(self.paths.runtime, self.clock)
        self.release_a, files_a = asset_release(A)
        self.release_b, files_b = asset_release(B)
        self.files = {**files_a, **files_b}
        self.save_state(releases=[self.release_a, self.release_b])

    def save_state(self, *, installed=A, releases=(), **extra):
        document = new_state(installed, NOW)
        document['releases'] = list(releases)
        document.update(extra)
        write_state(self.paths.state, document, now=NOW)
        return document

    def run_cli(self, argv, *, cgroup=None, open_url=None, fetch=None, now_local=None):
        out, err = StringIO(), StringIO()
        cgroup_patch = patch.object(cli, '_cgroup_text', return_value=cgroup) if cgroup else None
        if cgroup_patch:
            cgroup_patch.start()
            self.addCleanup(cgroup_patch.stop)
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', str(self.paths.config),
                             '--state-dir', str(self.paths.state_dir), *argv],
                            paths=self.paths, runner=self.host, clock=self.clock.utcnow,
                            monotonic=self.clock.monotonic, sleep=self.clock.sleep,
                            open_url=open_url, fetch=fetch, now_local=now_local)
        return code, out.getvalue(), err.getvalue()

    def starts(self):
        return [call for call in self.host.calls if call[0] == 'systemctl' and 'start' in call]

    # -- requester -------------------------------------------------------
    def test_wait_for_result_treats_a_running_oneshot_as_activating(self):
        unit = units.install_unit(B)
        request_id = units.write_request(self.paths, B, reason='auto', requested_by='timer',
                                         utcnow=self.clock.utcnow)
        probes = []

        def runner(args, **kwargs):
            if args[:2] == ['systemctl', 'show']:
                probes.append(list(args))
                document = json.loads(self.paths.state.read_text())
                document['last_result'] = {
                    'id': 'tx-1', 'request_id': request_id, 'result': 'success',
                    'to': B, 'from': A, 'reason': 'auto', 'step_failed': None,
                    'code': None, 'detail': None}
                write_state(self.paths.state, document, now=NOW)
                return subprocess.CompletedProcess(args, 0, 'activating\n', '')
            raise AssertionError(f'unexpected command: {args}')

        with redirect_stdout(StringIO()):
            code = cli._wait_for_result(self.paths, unit, request_id=request_id,
                                        baseline_id=None, runner=runner,
                                        utcnow=self.clock.utcnow, sleep=self.clock.sleep)
        self.assertEqual(code, 0)
        self.assertEqual(probes, [['systemctl', 'show', '-p', 'ActiveState', '--value', unit]])

    def test_requester_writes_the_request_and_starts_the_unit(self):
        code, out, err = self.run_cli(['install', B])
        self.assertEqual(code, 0, err)
        unit = units.install_unit(B)
        self.assertIn(['systemctl', 'start', '--no-block', unit], self.starts())
        request = units.request_path(self.paths, B)
        self.assertTrue(request.exists())
        document = json.loads(request.read_text())
        self.assertEqual((document['version'], document['reason'], document['requested_by']),
                         (B, 'manual', 'cli'))
        self.assertIn(unit, out)
        self.assertIn('journalctl -fu', out)
        self.assertFalse(self.paths.history.exists())

    def test_requester_reports_a_refused_resolve_without_starting_anything(self):
        code, _, err = self.run_cli(['install', '9.9.9-1'])
        self.assertEqual(code, 2)
        self.assertIn('unknown_version: ', err)
        self.assertEqual(self.starts(), [])

    def test_a_downgrade_needs_the_confirmation_flag(self):
        self.host = FakeUpdateHost(self.paths.runtime, self.clock, installed='2.0.0-1')
        self.save_state(installed='2.0.0-1', releases=[self.release_a])
        code, _, err = self.run_cli(['install', A])
        self.assertEqual(code, 2)
        self.assertIn('downgrade_not_confirmed: ', err)
        self.assertEqual(self.starts(), [])
        code, _, err = self.run_cli(['install', A, '--allow-downgrade'])
        self.assertEqual(code, 0, err)
        self.assertIn(['systemctl', 'start', '--no-block', units.install_unit(A)], self.starts())

    def test_a_live_transaction_refuses_the_request(self):
        self.save_state(releases=[self.release_b],
                        in_progress={'boot_id': 'boot-1', 'pid': os.getpid(), 'step': 'install'})
        with patch.object(cli.resources, 'read_boot_id', return_value='boot-1'):
            code, _, err = self.run_cli(['install', B])
        self.assertEqual(code, 2)
        self.assertIn('in_progress: ', err)
        self.assertEqual(self.starts(), [])

    def test_a_pending_request_refuses_the_request(self):
        units.write_request(self.paths, A, reason='manual', requested_by='cli',
                            utcnow=self.clock.utcnow)
        # The pending request is for another version but is younger than 120 s.
        code, _, err = self.run_cli(['install', B])
        self.assertEqual(code, 2)
        self.assertIn('request_pending: ', err)
        self.assertEqual(self.starts(), [])

    def test_rollback_with_nothing_kept(self):
        code, _, err = self.run_cli(['rollback'])
        self.assertEqual(code, 2)
        self.assertIn('no_rollback: ', err)
        self.assertEqual(self.starts(), [])

    # -- inside the unit -------------------------------------------------
    def test_from_unit_outside_its_own_cgroup_is_refused_before_the_lock(self):
        with lock.update_lock(self.paths.lock):
            code, _, err = self.run_cli(
                ['install', B, '--from-unit'],
                cgroup='0::/system.slice/qmi-supervisor.service\n')
        self.assertEqual(code, 2)
        self.assertIn('--from-unit runs only inside its unit', err)
        self.assertFalse(self.paths.history.exists())

    def test_from_unit_waits_for_the_lock_then_exits_75_without_recording(self):
        cgroup = '0::/system.slice/' + units.install_unit(B) + '\n'
        with lock.update_lock(self.paths.lock):
            code, _, err = self.run_cli(['install', B, '--from-unit'], cgroup=cgroup)
        self.assertEqual(code, 75)
        self.assertIn('Another update operation holds the lock', err)
        self.assertEqual(self.clock.monotonic() - 1000.0, 120.0)
        self.assertFalse(self.paths.history.exists())

    def test_from_unit_runs_the_transaction_and_consumes_the_request(self):
        request_id = units.write_request(self.paths, B, reason='auto', requested_by='timer',
                                         utcnow=self.clock.utcnow)
        cgroup = '0::/system.slice/' + units.install_unit(B) + '\n'
        code, _, err = self.run_cli(['install', B, '--from-unit'], cgroup=cgroup,
                                    open_url=FakeOpener(self.files))
        self.assertEqual(code, 0, err)
        self.assertFalse(units.request_path(self.paths, B).exists())
        document = json.loads(self.paths.state.read_text())
        self.assertIsNone(document['in_progress'])
        self.assertEqual(document['last_result']['result'], 'success')
        self.assertEqual(document['last_result']['reason'], 'auto')
        self.assertEqual(document['last_result']['request_id'], request_id)
        history = [json.loads(line) for line in self.paths.history.read_text().splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['result'], 'success')

    def test_a_broken_configuration_is_recorded_and_fails(self):
        self.paths.config.write_text('[update]\nmode = sometimes\n')
        cgroup = '0::/system.slice/' + units.install_unit(B) + '\n'
        code, _, err = self.run_cli(['install', B, '--from-unit'], cgroup=cgroup)
        self.assertEqual(code, 1)
        self.assertIn('config_unreadable: ', err)
        document = json.loads(self.paths.state.read_text())
        self.assertEqual(document['last_result']['code'], 'config_unreadable')
        self.assertEqual(document['last_result']['step_failed'], 'resolve')
        self.assertEqual(len(self.paths.history.read_text().splitlines()), 1)

    def test_rollback_from_unit_with_nothing_kept_records_no_rollback(self):
        cgroup = '0::/system.slice/' + units.ROLLBACK_UNIT + '\n'
        code, _, err = self.run_cli(['rollback', '--from-unit'], cgroup=cgroup)
        self.assertEqual(code, 1)
        self.assertIn('no_rollback: ', err)
        document = json.loads(self.paths.state.read_text())
        self.assertEqual(document['last_result']['code'], 'no_rollback')

    def test_rollback_from_unit_restores_the_kept_version(self):
        self.assertEqual(self.run_cli(['install', B], open_url=FakeOpener(self.files))[0], 0)
        # Follow the install synchronously, as the unit would.
        cgroup = '0::/system.slice/' + units.install_unit(B) + '\n'
        request_id = units.write_request(self.paths, B, reason='manual', requested_by='cli',
                                         utcnow=self.clock.utcnow)
        with patch.object(cli.resources, 'read_boot_id', return_value=None):
            self.assertEqual(self.run_cli(['install', B, '--from-unit'], cgroup=cgroup,
                                          open_url=FakeOpener(self.files))[0], 0)
        cgroup = '0::/system.slice/' + units.ROLLBACK_UNIT + '\n'
        code, _, err = self.run_cli(['rollback', '--from-unit'], cgroup=cgroup,
                                    open_url=FakeOpener({}))
        self.assertEqual(code, 0, err)
        document = json.loads(self.paths.state.read_text())
        self.assertEqual(document['last_result']['reason'], 'rollback')
        self.assertEqual(document['current_version'], A)


    # -- check: reconcile, repair and automatic mode ---------------------
    def test_check_reconciles_and_starts_the_automatic_update(self):
        self.paths.config.write_text(
            '[update]\nmode = auto\nwindow_start = 00:00\nwindow_end = 23:59\n')

        def fetch(source, **kwargs):
            return releases.FetchResult([release_item('v1.0.0'), release_item('v1.1.0')],
                                        None, False)

        with patch.object(runtime_version, 'installed_version', return_value=A):
            code, _, err = self.run_cli(['check', '--auto'], fetch=fetch,
                                        now_local=datetime(2026, 11, 21, 3, 30))
        self.assertEqual(code, 0, err)
        self.assertIn(['systemctl', 'start', '--no-block', units.install_unit(B)], self.starts())
        request = units.request_path(self.paths, B)
        self.assertTrue(request.exists())
        self.assertEqual(json.loads(request.read_text())['reason'], 'auto')
        saved = json.loads(self.paths.state.read_text())
        self.assertEqual(saved['auto']['reason'], 'started')
        self.assertEqual(saved['auto']['version'], B)
        self.assertTrue(saved['rollback_ready'])

    def test_automatic_check_with_mode_off_still_records_an_interrupted_transaction(self):
        self.paths.config.write_text('[update]\nmode = off\n')
        document = json.loads(self.paths.state.read_text())
        document['in_progress'] = {'id': 'dead', 'to': B, 'from': A, 'reason': 'auto',
                                   'step': 'install', 'steps': [],
                                   'started_at_utc': '2026-11-21T03:00:00+00:00'}
        write_state(self.paths.state, document, now=NOW)
        self.host.status, self.host.installed = 'iF', B

        def forbidden(*args, **kwargs):
            raise AssertionError('a disabled mode must not use the network')

        code, _, err = self.run_cli(['check', '--auto'], fetch=forbidden)
        self.assertEqual(code, 0, err)
        saved = json.loads(self.paths.state.read_text())
        self.assertIsNone(saved['in_progress'])
        self.assertEqual([line['result'] for line in history.read(self.paths, limit=5)],
                         ['interrupted'])

    def test_check_returns_zero_when_the_lock_is_busy(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('fetch must not be called')

        with lock.update_lock(self.paths.lock):
            code, out, err = self.run_cli(['check', '--auto'], fetch=forbidden)
        self.assertEqual((code, out, err), (0, '', ''))

    def test_check_never_imports_the_version_nor_runs_configure_a(self):
        # The fake host raises on `dpkg --configure -a`; the check must not need it.
        self.assertEqual(self.run_cli(['check', '--auto'],
                                      fetch=lambda *a, **k: releases.FetchResult([], None, False),
                                      now_local=datetime(2026, 11, 21, 3, 30))[0], 0)
        self.assertFalse(any('--configure' in call and '-a' in call for call in self.host.calls))

    def test_rollback_from_unit_records_the_interruption_before_anything_else(self):
        tx = {
            'schema_version': 1, 'id': 'a' * 32, 'request_id': None,
            'from': A, 'to': B, 'reason': 'manual', 'step': 'health',
            'steps': [{'id': name, 'state': 'pending'} for name in history.STEPS],
            'started_at_utc': '2026-11-21T03:10:02+00:00',
            'updated_at_utc': '2026-11-21T03:10:02+00:00', 'wait_until_utc': None,
            'pid': 999999, 'boot_id': 'boot-1', 'unit': units.install_unit(B),
            'rollback_available': True, 'signed_by': None,
            'online_before': [], 'web_before': False,
        }
        self.save_state(in_progress=tx)
        cgroup = '0::/system.slice/' + units.ROLLBACK_UNIT + '\n'
        code, _, err = self.run_cli(['rollback', '--from-unit'], cgroup=cgroup)
        self.assertEqual(code, 1)
        self.assertIn('no_rollback: ', err)
        lines = [json.loads(line) for line in self.paths.history.read_text().splitlines()]
        self.assertEqual(lines[0]['result'], 'interrupted')
        self.assertEqual(lines[0]['step_failed'], 'health')
        self.assertEqual(lines[-1]['code'], 'no_rollback')
        self.assertIsNone(json.loads(self.paths.state.read_text())['in_progress'])


class EagerImportTests(unittest.TestCase):
    def test_every_transaction_module_is_imported_before_the_install(self):
        program = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); import modules.update.cli; "
            "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('modules.'))))")
        result = subprocess.run([sys.executable, '-I', '-c', program], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = set(result.stdout.split())
        for name in ('modules.runtime.configuration', 'modules.runtime.ipc',
                     'modules.runtime.version', 'modules.telemetry.resources',
                     'modules.update.auto', 'modules.update.commands', 'modules.update.files',
                     'modules.update.health', 'modules.update.history', 'modules.update.install',
                     'modules.update.lock', 'modules.update.units', 'modules.update.verify'):
            self.assertIn(name, imported)
        self.assertFalse(any(name == 'gi' or name.startswith('gi.') for name in imported))


if __name__ == '__main__':
    unittest.main()
