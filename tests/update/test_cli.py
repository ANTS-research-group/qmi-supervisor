"""The command line: check/auto scheduling, failures, token handling, list/status."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from modules.runtime import version as runtime_version
from modules.update import cli, releases, state, transport
from modules.update.state import UpdatePaths
from tests.update.fixtures import GITHUB_V120, release_item

NOW = datetime(2026, 11, 21, 3, 7, 41, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def _forbidden(*args, **kwargs):
    raise AssertionError('fetch must not be called')


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = UpdatePaths(config=root / 'etc/qmi-supervisor.ini',
                                 state_dir=root / 'var/update',
                                 token=root / 'etc/qmi-supervisor-update-token',
                                 lock=root / 'run/qmi-supervisor-update.lock',
                                 requests=root / 'run/qmi-supervisor-update',
                                 runtime=root / 'run/qmi-supervisor')
        self.paths.config.parent.mkdir(parents=True, exist_ok=True)
        self.write_ini('[update]\nmode = notify\n')
        self.current = ['1.2.0-1']
        version = patch.object(runtime_version, 'installed_version', side_effect=lambda: self.current[0])
        version.start()
        self.addCleanup(version.stop)
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('JOURNAL_STREAM', None)

    def write_ini(self, text):
        self.paths.config.write_text(text)

    def run_cli(self, argv, *, fetch=None, clock=lambda: NOW, stdin=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', str(self.paths.config), '--state-dir', str(self.paths.state_dir), *argv],
                            paths=self.paths, fetch=fetch, clock=clock, stdin=stdin)
        return code, out.getvalue(), err.getvalue()

    def successful_fetch(self, items, etag='W/"etag"'):
        calls = []

        def fetch(source, **kwargs):
            calls.append((source, kwargs))
            return releases.FetchResult(items, etag, False)
        return fetch, calls

    def test_check_records_an_available_update(self):
        fetch, calls = self.successful_fetch([GITHUB_V120, release_item('v9.9.0')])
        code, out, err = self.run_cli(['check'], fetch=fetch)
        self.assertEqual(code, 0, err)
        document = state.load_state(self.paths.state)
        self.assertEqual(document['newest_installable'], {'tag': 'v9.9.0', 'version': '9.9.0-1'})
        self.assertTrue(document['update_available'])
        self.assertEqual(document['etag'], 'W/"etag"')
        self.assertIsNone(document['check_error'])
        for key in ('schema_version', 'updated_utc', 'current_version', 'source', 'channel', 'require_signature',
                    'checked_utc', 'last_success_utc', 'check_error', 'etag', 'releases', 'newest_available',
                    'newest_installable', 'update_available', 'in_progress', 'last_result'):
            self.assertIn(key, document)
        self.assertEqual(out.strip(), 'Update check: installed 1.2.0-1; update available: v9.9.0 (9.9.0-1).')
        self.assertEqual(calls[0][1]['token'], None)
        self.assertEqual(calls[0][1]['etag'], None)

    def test_auto_skips_when_not_due_and_rewrites_after_a_version_change(self):
        fetch, calls = self.successful_fetch([release_item('v9.9.0')])
        self.assertEqual(self.run_cli(['check'], fetch=fetch)[0], 0)
        before = state.load_state(self.paths.state)
        self.assertEqual(self.run_cli(['check', '--auto'], fetch=_forbidden), (0, '', ''))
        after = state.load_state(self.paths.state)
        # A timer run always records state.auto; nothing else changes.
        self.assertIn(after.pop('auto')['reason'], ('mode_not_auto', 'channel_not_stable',
                                                    'signature_not_required', 'outside_window'))
        before.pop('auto', None)
        self.assertEqual({key: value for key, value in after.items() if key != 'updated_utc'},
                         {key: value for key, value in before.items() if key != 'updated_utc'})

        later = NOW + timedelta(hours=23, minutes=51)
        code, _, err = self.run_cli(['check', '--auto'], fetch=fetch, clock=lambda: later)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 2)

        before_clock = NOW - timedelta(hours=2)
        self.assertEqual(self.run_cli(['check', '--auto'], fetch=fetch, clock=lambda: before_clock)[0], 0)
        self.assertEqual(len(calls), 3)

        self.current[0] = '1.2.1-1'
        calls_before = len(calls)
        self.assertEqual(self.run_cli(['check', '--auto'], fetch=_forbidden)[0], 0)
        self.assertEqual(len(calls), calls_before)
        document = state.load_state(self.paths.state)
        self.assertEqual(document['current_version'], '1.2.1-1')
        self.assertTrue(document['update_available'])

    def test_off_mode_skips_auto_but_manual_still_checks(self):
        self.write_ini('[update]\nmode = off\n')
        self.assertEqual(self.run_cli(['check', '--auto'], fetch=_forbidden), (0, '', ''))
        self.assertFalse(self.paths.state.exists())
        fetch, calls = self.successful_fetch([release_item('v9.9.0')])
        self.assertEqual(self.run_cli(['check'], fetch=fetch)[0], 0)
        self.assertEqual(len(calls), 1)

    def test_failure_reporting_and_repeat_suppression(self):
        fetch, _ = self.successful_fetch([GITHUB_V120])
        self.assertEqual(self.run_cli(['check'], fetch=fetch)[0], 0)

        def failing(*args, **kwargs):
            raise transport.UpdateCheckError('network', 'x')

        code, _, err = self.run_cli(['check'], fetch=failing)
        self.assertEqual(code, 1)
        self.assertIn('Update check failed (network): x', err)
        document = state.load_state(self.paths.state)
        self.assertEqual(document['check_error']['attempts'], 1)
        self.assertEqual(len(document['releases']), 1)  # previous releases kept
        since = document['check_error']['since_utc']

        due = NOW + timedelta(hours=24)
        code, out, err = self.run_cli(['check', '--auto'], fetch=failing, clock=lambda: due)
        self.assertEqual((code, out, err), (0, '', ''))
        document = state.load_state(self.paths.state)
        self.assertEqual(document['check_error']['attempts'], 2)
        self.assertEqual(document['check_error']['since_utc'], since)

        def other(*args, **kwargs):
            raise transport.UpdateCheckError('tls', 'y')

        _, _, err = self.run_cli(['check', '--auto'], fetch=other, clock=lambda: due)
        self.assertIn('Update check failed (tls): y', err)

    def test_not_modified_reuses_releases_and_updates_last_success(self):
        calls = []

        def fetch(source, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return releases.FetchResult([GITHUB_V120], 'W/"e"', False)
            return releases.FetchResult(None, kwargs['etag'], True)

        self.assertEqual(self.run_cli(['check'], fetch=fetch)[0], 0)
        saved = state.load_state(self.paths.state)['releases']
        later = NOW + timedelta(minutes=1)
        self.assertEqual(self.run_cli(['check'], fetch=fetch, clock=lambda: later)[0], 0)
        self.assertEqual(calls[1]['etag'], 'W/"e"')
        document = state.load_state(self.paths.state)
        self.assertEqual(document['releases'], saved)
        self.assertEqual(document['last_success_utc'], state.stamp(later))

    def test_changing_the_source_clears_the_etag(self):
        calls = []

        def fetch(source, **kwargs):
            calls.append(kwargs)
            return releases.FetchResult([GITHUB_V120], 'W/"new"', False)

        self.assertEqual(self.run_cli(['check'], fetch=fetch)[0], 0)
        self.write_ini('[update]\nsource = https://example.invalid/releases\n')
        self.assertEqual(self.run_cli(['check'], fetch=fetch)[0], 0)
        self.assertEqual(calls[0]['etag'], None)
        self.assertIsNone(calls[1]['etag'])

    def test_a_held_update_lock_makes_the_check_skip_quietly(self):
        # One lock for every writer; a check never waits and never fails.
        with state.state_lock(self.paths.lock) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.run_cli(['check', '--auto'], fetch=_forbidden), (0, '', ''))
            self.assertEqual(self.run_cli(['check'], fetch=_forbidden), (0, '', ''))

    def test_set_token_is_private_never_printed_and_used_by_check(self):
        token = 'ghp_' + 'a' * 36
        with patch.object(cli, '_is_root', return_value=True):
            code, out, err = self.run_cli(['set-token'], stdin=StringIO(token + '\n'))
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(self.paths.token.stat().st_mode), 0o600)
        self.assertEqual(self.paths.token.read_text(), token + '\n')

        seen = []

        def fetch(source, **kwargs):
            seen.append(kwargs.get('token'))
            return releases.FetchResult([], None, False)

        code, out, err = self.run_cli(['check'], fetch=fetch)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [token])
        self.assertNotIn(token, out + err)
        self.assertNotIn(token, self.paths.state.read_text())

        with patch.object(cli, '_is_root', return_value=True):
            self.assertEqual(self.run_cli(['clear-token'])[0], 0)
        self.assertFalse(self.paths.token.exists())

    def test_set_token_requires_root_and_rejects_invalid_tokens(self):
        with patch.object(cli, '_is_root', return_value=False):
            code, _, err = self.run_cli(['set-token'], stdin=StringIO('ghp_' + 'a' * 36 + '\n'))
        self.assertEqual(code, 1)
        self.assertIn('Run set-token with sudo.', err)
        self.assertFalse(self.paths.token.exists())
        with patch.object(cli, '_is_root', return_value=True):
            code, _, err = self.run_cli(['set-token'], stdin=StringIO('short\n'))
        self.assertEqual(code, 1)
        self.assertFalse(self.paths.token.exists())

    def test_insecure_token_file_blocks_the_check(self):
        self.paths.token.parent.mkdir(parents=True, exist_ok=True)
        self.paths.token.write_text('ghp_' + 'a' * 36 + '\n')
        os.chmod(self.paths.token, 0o644)
        code, _, _ = self.run_cli(['check'], fetch=_forbidden)
        self.assertEqual(code, 1)
        document = state.load_state(self.paths.state)
        self.assertEqual(document['check_error']['code'], 'bad_token_file')

    def test_invalid_configuration_writes_no_state(self):
        self.write_ini('[update]\nmode = sometimes\n')
        code, _, err = self.run_cli(['check'], fetch=_forbidden)
        self.assertEqual(code, 1)
        self.assertIn('Cannot use the configuration', err)
        self.assertFalse(self.paths.state.exists())

    def test_list_and_status_json_and_text(self):
        code, out, _ = self.run_cli(['status', '--json'])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload['installed_version'], '1.2.0-1')
        self.assertIsNone(payload['state'])
        self.assertFalse(payload['token_configured'])
        code, out, _ = self.run_cli(['status'])
        self.assertIn('Last check: never', out)
        code, out, _ = self.run_cli(['list', '--json'])
        self.assertEqual(json.loads(out), [])
        code, out, _ = self.run_cli(['list'])
        self.assertIn('No check has been made yet.', out)

    def test_a_symlinked_state_directory_is_a_warning_not_a_traceback(self):
        target = self.paths.state_dir.parent / 'real-state'
        target.mkdir(parents=True)
        self.paths.state_dir.symlink_to(target, target_is_directory=True)
        code, _, err = self.run_cli(['check'], fetch=_forbidden)
        self.assertEqual(code, 1)
        self.assertIn('Cannot write', err)
        self.assertIn('symlink', err)

    def test_status_uses_the_attempt_time_after_a_failed_first_check(self):
        def failing(source, **kwargs):
            raise transport.UpdateCheckError('network', 'offline')
        self.assertEqual(self.run_cli(['check'], fetch=failing)[0], 1)
        checked = state.load_state(self.paths.state)['checked_utc']
        code, out, _ = self.run_cli(['status'])
        self.assertEqual(code, 0)
        self.assertIn(f'Last check: {checked}, failed (network:', out)

    def test_the_token_is_only_attached_to_the_configured_source_host(self):
        self.write_ini('[update]\nsource = https://api.example/releases\n')
        token = 'ghp_' + 'a' * 36
        self.paths.token.parent.mkdir(parents=True, exist_ok=True)
        self.paths.token.write_text(token + '\n')
        self.paths.token.chmod(0o600)
        seen = []

        def fake(url, *, accept, timeout, token=None):
            seen.append(token)
            return object()

        with patch.object(cli.transport, 'open_url', side_effect=fake):
            opener = cli._asset_opener(self.paths)
            opener('https://api.example/asset', accept='application/octet-stream', timeout=1)
            opener('https://other.example/asset', accept='application/octet-stream', timeout=1)
        self.assertEqual(seen, [token, None])
        self.paths.token.unlink()

    def test_check_uses_no_subprocess(self):
        fetch, _ = self.successful_fetch([release_item('v9.9.0')])
        with patch('subprocess.run', side_effect=AssertionError('no subprocess')), \
                patch('subprocess.Popen', side_effect=AssertionError('no subprocess')):
            code, _, err = self.run_cli(['check'], fetch=fetch)
        self.assertEqual(code, 0, err)

    def test_updater_package_is_gi_free(self):
        program = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import modules.update.cli; "
                   "print(any(m == 'gi' or m.startswith('gi.') for m in sys.modules))")
        result = subprocess.run([sys.executable, '-I', '-c', program], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), 'False', result.stderr)

    def test_entry_point_help_lists_check(self):
        result = subprocess.run([sys.executable, '-I', '-B', 'tools/update/updater.py', '--help'],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('check', result.stdout)


if __name__ == '__main__':
    unittest.main()
