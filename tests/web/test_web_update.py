"""Update adapter and HTTP API tests: unit names, state reading, systemd and /api/update*."""
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules.runtime.settings import Settings, load_settings
from modules.web import updates as update_module
from modules.web.auth import hash_password
from modules.web.server import (_write_config, build_server, SESSION_COOKIE)
from modules.web.updates import (CHECK_MANUAL_UNIT, CHECK_UNIT, INSTALL_PREFIX, ROLLBACK_UNIT, UpdateControl,
                                 build_info, install_unit, read_state, start_unit, unit_instance,
                                 unit_states, valid_version)
from tests.web.static_fixture import make_export

PASSWORD = 'update-password-2026'


def _completed(returncode=0, stdout='', stderr=''):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _release(version, published='2026-11-20T08:00:00+00:00', *, prerelease=False,
             installable=True, reason=None, notes='Changes'):
    # the updater's real shape: `version` is the upstream version and `deb.version` the package version.
    upstream = version.rsplit('-', 1)[0]
    deb = {'name': f'qmi-supervisor_{version}_all.deb', 'version': version, 'size': 1024,
           'url': 'https://example.invalid/deb', 'api_url': None, 'sha256': None}
    return {'tag': 'v' + upstream, 'version': upstream, 'name': version,
            'published_utc': published, 'prerelease': prerelease,
            'html_url': 'https://example.invalid/release', 'notes': notes,
            'notes_truncated': False, 'deb': deb,
            'installable': installable, 'reason': reason}


class UnitNameTests(unittest.TestCase):
    def test_systemd_escape_vectors(self):
        self.assertEqual(unit_instance('1.3.0-1'), '1.3.0\\x2d1')
        self.assertEqual(unit_instance('1.2.0+1.g1bfa441-1'), '1.2.0\\x2b1.g1bfa441\\x2d1')
        self.assertEqual(unit_instance('1.3.0~rc1-1'), '1.3.0\\x7erc1\\x2d1')
        self.assertEqual(install_unit('1.3.0-1'),
                         'qmi-supervisor-update-install@1.3.0\\x2d1.service')

    def test_invalid_versions_are_rejected(self):
        for value in ('', 'v1.3.0', '-1', '1.3.0 1', '1.3.0/1', '../1', '1.3.0\n',
                      '1.3.0-1;reboot', 'é', '1' * 65, None, 3, b'1.3.0-1'):
            with self.subTest(value=value):
                self.assertFalse(valid_version(value))
                with self.assertRaises(ValueError):
                    unit_instance(value)


class StateReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'state.json'

    def write(self, document):
        self.path.write_text(json.dumps(document))

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(read_state(self.path), (None, None))

    def test_invalid_or_untrusted_files_are_unreadable(self):
        for content in ('not json', '[]', 'null'):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertEqual(read_state(self.path), (None, 'unreadable'))
        target = self.root / 'target.json'
        target.write_text(json.dumps({'releases': []}))
        link = self.root / 'link.json'
        link.symlink_to(target)
        self.assertEqual(read_state(link), (None, 'unreadable'))
        self.path.write_bytes(b'x' * (512 * 1024 + 1))
        self.assertEqual(read_state(self.path), (None, 'unreadable'))
        self.path.write_text(json.dumps({'releases': []}))
        self.path.chmod(0o666)
        self.assertEqual(read_state(self.path), (None, 'unreadable'))

    def test_releases_are_sorted_trimmed_and_bounded(self):
        releases = [_release(f'1.{index}.0-1', notes='a' * 5000) for index in range(25)]
        releases.append(_release('v9.9.9', installable=True))
        self.write({'releases': releases, 'history': [],
                    'check_error': {'code': 'network', 'detail': 'x' * 1000}})
        state, error = read_state(self.path)
        self.assertIsNone(error)
        self.assertEqual(len(state['releases']), 20)
        self.assertEqual(state['releases'][0]['version'], '1.24.0-1')
        self.assertEqual(state['releases'][-1]['version'], '1.5.0-1')
        self.assertTrue(all(len(item['notes']) == 4000 for item in state['releases']))
        self.assertTrue(all(item['notes_truncated'] for item in state['releases']))
        self.assertEqual(len(state['check']['error']), 256)
        self.assertEqual(state['check']['state'], 'failed')

    def test_history_is_newest_first_and_bounded(self):
        history = [{'reason': 'manual', 'result': 'success', 'to': f'1.{index}.0-1',
                    'from': '1.0.0-1', 'at_utc': '2026-11-20T09:00:00+00:00',
                    'detail': 'y' * 1000} for index in range(15)]
        self.write({'releases': [], 'history': history, 'in_progress': None,
                    'last_result': None})
        state, _ = read_state(self.path)
        self.assertEqual(len(state['history']), 10)
        self.assertEqual(state['history'][0]['version'], '1.0.0-1')
        self.assertEqual(state['history'][0]['error_code'] if 'error_code' in state['history'][0]
                         else None, None)
        self.assertEqual(len(state['history'][0]['error']), 256)

    def test_install_and_rollback_are_normalised(self):
        self.write({
            'releases': [],
            'in_progress': {'to': '1.3.0-1', 'from': '1.2.0-1', 'reason': 'manual',
                            'step': 'download', 'started_at_utc': '2026-11-20T09:00:00+00:00',
                            'steps': [{'id': 'download', 'state': 'running'},
                                      {'id': 'verify', 'state': 'pending'}]},
            'last_result': {'to': '1.1.0-1', 'from': '1.2.0-1', 'reason': 'auto',
                            'result': 'success', 'at_utc': '2026-11-19T09:00:00+00:00',
                            'code': None, 'detail': None, 'steps': []},
            'history': [],
            'rollback': {'available': True, 'version': '1.2.0-1'},
        })
        state, _ = read_state(self.path)
        self.assertEqual(state['install']['state'], 'running')
        self.assertEqual(state['install']['action'], 'install')
        self.assertEqual(state['install']['from_version'], '1.2.0-1')
        self.assertEqual(state['install']['steps'][0], {'id': 'download', 'state': 'running'})
        self.assertEqual(state['rollback'], {'available': True, 'version': '1.2.0-1'})

        self.write({'releases': [], 'in_progress': None,
                    'last_result': {'to': '1.1.0-1', 'from': '1.2.0-1', 'reason': 'auto',
                                    'result': 'success', 'at_utc': '2026-11-19T09:00:00+00:00',
                                    'code': None, 'detail': None, 'steps': []},
                    'history': [], 'rollback': {'available': False, 'version': None}})
        state, _ = read_state(self.path)
        self.assertEqual(state['install']['state'], 'succeeded')
        self.assertEqual(state['install']['action'], 'automatic')
        self.assertEqual(state['install']['finished_at_utc'], '2026-11-19T09:00:00+00:00')
        self.assertEqual(state['rollback'], {'available': False, 'version': None})


class UnitStateTests(unittest.TestCase):
    def test_exact_argv_and_running_states(self):
        calls = []

        def run(command, **kwargs):
            calls.append(list(command))
            return _completed(stdout=json.dumps([
                {'unit': CHECK_MANUAL_UNIT, 'load': 'loaded', 'active': 'active', 'sub': 'running'},
                {'unit': install_unit('1.3.0-1'), 'load': 'loaded', 'active': 'activating', 'sub': 'start'},
                {'unit': install_unit('1.2.0-1'), 'load': 'loaded', 'active': 'failed', 'sub': 'failed'},
            ]))

        states = unit_states(run)
        self.assertEqual(calls[0], ['systemctl', 'list-units', '--all', '--output=json',
                                    '--no-pager', CHECK_UNIT, CHECK_MANUAL_UNIT,
                                    INSTALL_PREFIX + '*.service', ROLLBACK_UNIT])
        self.assertTrue(states['check_running'])
        self.assertTrue(states['install_running'])
        self.assertEqual(states['install_unit'], install_unit('1.3.0-1'))
        self.assertFalse(states['rollback_running'])

        failed = unit_states(lambda command, **kwargs: _completed(stdout=json.dumps([
            {'unit': install_unit('1.2.0-1'), 'load': 'loaded', 'active': 'failed', 'sub': 'failed'},
            {'unit': ROLLBACK_UNIT, 'load': 'loaded', 'active': 'inactive', 'sub': 'dead'},
        ])))
        self.assertFalse(failed['install_running'])
        self.assertIsNone(failed['install_unit'])
        self.assertFalse(failed['rollback_running'])

    def test_unavailable_systemd_is_none(self):
        for run in (
            lambda command, **kwargs: _completed(returncode=1),
            lambda command, **kwargs: (_ for _ in ()).throw(OSError('no systemd')),
            lambda command, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired('systemctl', 5)),
            lambda command, **kwargs: _completed(stdout='not json'),
            lambda command, **kwargs: _completed(stdout='{}'),
        ):
            with self.subTest(run=run):
                self.assertIsNone(unit_states(run))

    def test_start_unit_argv_and_failure(self):
        calls = []

        def run(command, **kwargs):
            calls.append(list(command))
            return _completed()

        start_unit(run, CHECK_UNIT)
        self.assertEqual(calls[-1], ['systemctl', 'start', '--no-block', CHECK_UNIT])
        with self.assertRaises(update_module.UpdateError) as failure:
            start_unit(lambda command, **kwargs: _completed(returncode=5, stderr='nope'),
                       CHECK_UNIT)
        self.assertEqual((failure.exception.status, failure.exception.code),
                         (503, 'UPDATER_UNAVAILABLE'))


class BuildInfoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'BUILD_INFO.json'
        patcher = patch.object(update_module, 'BUILD_INFO_PATH', self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(build_info.cache_clear)
        build_info.cache_clear()

    def test_valid_missing_and_invalid_files(self):
        self.assertEqual(build_info(), {'commit': None, 'built_utc': None})
        build_info.cache_clear()
        self.path.write_text(json.dumps({'version': '1.2.0-1', 'commit': 'abc1234',
                                         'built_utc': '2026-11-19T16:47:00Z'}))
        self.assertEqual(build_info(), {'commit': 'abc1234',
                                        'built_utc': '2026-11-19T16:47:00Z'})
        for content in ('not json', '[]', 'x' * 5000):
            with self.subTest(content=content):
                build_info.cache_clear()
                self.path.write_text(content)
                self.assertEqual(build_info(), {'commit': None, 'built_utc': None})


class UpdateHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(PASSWORD)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / 'state.json'
        self.config = self.root / 'config.ini'
        self.static_root = make_export(self.root / 'frontend/out')
        self.now = 1000.0
        self.installed = '1.2.0-1'
        self.calls = []
        self.units = []
        self.list_raises = None
        self.list_returncode = 0
        self.start_returncode = 0
        settings = replace(Settings(), web=replace(Settings().web, enabled=True,
                                                   username='admin',
                                                   password_hash=self.password_hash))
        _write_config(self.config, settings)
        self.server = self.start()
        self.servers = [(self.server, self.thread)]
        self.addCleanup(self.close)
        installed_patch = patch('modules.web.updates.installed_version',
                                side_effect=lambda: self.installed)
        installed_patch.start()
        self.addCleanup(installed_patch.stop)
        verifier = patch('modules.web.auth.verify_password',
                         side_effect=lambda password, encoded: password == PASSWORD)
        verifier.start()
        self.addCleanup(verifier.stop)

    def fake(self, command, **kwargs):
        self.calls.append(list(command))
        if command[:2] == ['systemctl', 'list-units']:
            if self.list_raises is not None:
                raise self.list_raises
            return _completed(returncode=self.list_returncode,
                              stdout=json.dumps(self.units))
        if command[:2] == ['systemctl', 'start']:
            return _completed(returncode=self.start_returncode, stderr='start failed')
        raise AssertionError(f'unexpected command: {command}')

    def start(self):
        control = UpdateControl(self.state_path, run=self.fake, clock=lambda: self.now)
        server = build_server(load_settings(self.config), self.config,
                              self.root / 'status.json',
                              server_address=('127.0.0.1', 0),
                              static_root=self.static_root, update_control=control)
        self.thread = threading.Thread(target=server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        return server

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def auth(self):
        session = self.server.auth.login('admin', PASSWORD, 'test-client')
        return {'Cookie': f'{SESSION_COOKIE}={session["session_id"]}',
                'X-CSRF-Token': session['csrf_token']}

    def request(self, method, path, payload=None, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        headers = dict(headers or {})
        if payload is not None:
            raw = json.dumps(payload)
            headers.setdefault('Content-Type', 'application/json')
        connection.request(method, path, raw, headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), json.loads(body) if body else None

    def write_state(self, releases=None, **fields):
        document = {
            'schema_version': 1, 'updated_utc': '2026-11-20T09:00:00+00:00',
            'current_version': self.installed, 'source': None, 'channel': 'stable',
            'require_signature': True, 'checked_utc': '2026-11-20T09:00:00+00:00',
            'last_success_utc': None, 'check_error': None, 'etag': None,
            'releases': releases or [], 'newest_available': None, 'newest_installable': None,
            'update_available': False, 'in_progress': None, 'last_result': None,
            'history': [], 'rollback': {'available': False, 'version': None},
        }
        document.update(fields)
        self.state_path.write_text(json.dumps(document))
        return document

    def write_policy(self, **update):
        lines = ['[web]', f'password_hash = {self.password_hash}', '[update]']
        for key, value in update.items():
            lines.append(f'{key} = {str(value).lower() if isinstance(value, bool) else value}')
        self.config.write_text('\n'.join(lines) + '\n')

    def install_body(self, version, allow=False, password=PASSWORD):
        return {'version': version, 'allow_downgrade': allow, 'password': password}

    def starts(self):
        return [call for call in self.calls if call[:2] == ['systemctl', 'start']]

    # -- authentication ----------------------------------------------------
    def test_every_update_endpoint_requires_a_session_before_doing_anything(self):
        self.assertEqual(self.request('GET', '/api/update')[0], 401)
        self.assertEqual(self.request('POST', '/api/update/check', {})[0], 401)
        self.assertEqual(self.request('POST', '/api/update/install',
                                      self.install_body('1.3.0-1'))[0], 401)
        self.assertEqual(self.request('POST', '/api/update/rollback',
                                      {'password': PASSWORD})[0], 401)
        self.assertEqual(self.calls, [])

    # -- GET ---------------------------------------------------------------
    def test_get_annotates_releases_and_publishes_settings(self):
        self.write_state(releases=[
            _release('1.3.0-1'),
            _release('1.3.0~rc1-1', prerelease=True),
            _release('1.2.0-1'),
            _release('1.1.0-1'),
        ])
        status, _, body = self.request('GET', '/api/update', headers=self.auth())
        self.assertEqual(status, 200, body)
        self.assertEqual(body['installed']['version'], '1.2.0-1')
        self.assertEqual(body['settings']['mode'], 'off')
        self.assertEqual(body['settings']['window_start'], '03:00')
        self.assertEqual(body['check']['state'], 'ok')
        self.assertEqual(body['check']['checked_at_utc'], '2026-11-20T09:00:00+00:00')
        self.assertIsNone(body['state_error'])
        self.assertIsNone(body['units_error'])
        by_version = {item['version']: item for item in body['releases']}
        self.assertEqual(by_version['1.3.0-1']['relation'], 'newer')
        self.assertTrue(by_version['1.3.0-1']['installable'])
        self.assertEqual(by_version['1.3.0~rc1-1']['reason'], 'prerelease_channel')
        self.assertFalse(by_version['1.3.0~rc1-1']['installable'])
        self.assertEqual(by_version['1.2.0-1']['relation'], 'installed',
                         'The updater stores the upstream version; the package version decides')
        self.assertEqual(by_version['1.2.0-1']['reason'], 'installed')
        self.assertEqual(by_version['1.1.0-1']['relation'], 'older')
        self.assertTrue(by_version['1.1.0-1']['installable'])
        self.assertEqual(body['releases'][0]['version'], '1.3.0-1',
                         'newest first by Debian ordering')

    def test_older_releases_are_not_installable_when_downgrades_are_off(self):
        self.write_policy(allow_downgrade=False)
        self.write_state(releases=[_release('1.1.0-1')])
        status, _, body = self.request('GET', '/api/update', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(body['releases'][0]['reason'], 'downgrade_disabled')
        self.assertFalse(body['releases'][0]['installable'])

    def test_no_state_file_means_never_checked(self):
        self.units = [{'unit': CHECK_MANUAL_UNIT, 'load': 'loaded',
                       'active': 'active', 'sub': 'running'}]
        status, _, body = self.request('GET', '/api/update', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(body['check']['state'], 'never')
        self.assertTrue(body['check']['running'],
                        'a check started before any state exists still runs')
        self.assertEqual(body['releases'], [])
        self.assertIsNone(body['install'])
        self.assertIsNone(body['state_error'])

    def test_units_unavailable_still_serves_state(self):
        self.list_raises = OSError('no systemd')
        self.write_state(releases=[])
        status, _, body = self.request('GET', '/api/update', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(body['units_error'], 'unavailable')

    def test_query_strings_are_not_supported(self):
        headers = self.auth()
        self.assertEqual(self.request('GET', '/api/update?x=1', headers=headers)[0], 404)
        self.assertEqual(self.request('POST', '/api/update/check?x=1', {},
                                      headers=headers)[0], 404)

    # -- check -------------------------------------------------------------
    def test_check_requires_csrf_and_origin_and_runs_one_unit(self):
        self.write_state(releases=[])
        cookie = self.auth()
        for headers in ({'Cookie': cookie['Cookie']},
                        dict(cookie, **{'X-CSRF-Token': 'wrong'}),
                        dict(cookie, Origin='https://evil.example')):
            self.assertEqual(self.request('POST', '/api/update/check', {}, headers)[0], 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request('POST', '/api/update/check', {'x': 1}, cookie)[0], 400)
        self.assertEqual(self.calls, [])
        status, _, body = self.request('POST', '/api/update/check', {}, cookie)
        self.assertEqual(status, 202, body)
        self.assertTrue(body['started'])
        self.assertEqual(self.calls[-1],
                         ['systemctl', 'start', '--no-block', CHECK_MANUAL_UNIT])

    def test_check_is_rate_limited_per_process(self):
        self.write_state(releases=[])
        headers = self.auth()
        self.assertEqual(self.request('POST', '/api/update/check', {}, headers)[0], 202)
        status, response_headers, body = self.request('POST', '/api/update/check', {}, headers)
        self.assertEqual(status, 429)
        self.assertEqual(body['code'], 'RATE_LIMITED')
        self.assertIn('Retry-After', response_headers)
        self.assertLessEqual(body['retry_after'], 60)
        self.now += 60
        self.assertEqual(self.request('POST', '/api/update/check', {}, headers)[0], 202)

    def test_check_is_refused_while_an_install_runs(self):
        self.write_state(releases=[])
        headers = self.auth()
        self.units = [{'unit': install_unit('1.3.0-1'), 'load': 'loaded',
                       'active': 'activating', 'sub': 'start'}]
        status, _, body = self.request('POST', '/api/update/check', {}, headers)
        self.assertEqual(status, 409)
        self.assertEqual(body['code'], 'UPDATE_IN_PROGRESS')

    # -- install validation ------------------------------------------------
    def test_install_is_validated_before_the_password_is_checked(self):
        self.write_state(releases=[
            _release('1.3.0-1'),
            _release('1.2.0-1'),
            _release('1.4.0-1', installable=False, reason='no_package'),
            _release('1.1.0-1'),
        ])
        headers = self.auth()
        cases = [
            (self.install_body('9.9.9-1'), 409, 'UNKNOWN_VERSION'),
            (self.install_body('1.2.0-1'), 409, 'ALREADY_INSTALLED'),
            (self.install_body('1.4.0-1'), 409, 'NOT_INSTALLABLE'),
            (self.install_body('1.1.0-1'), 400, 'DOWNGRADE_NOT_CONFIRMED'),
            (dict(self.install_body('1.3.0-1'), extra=1), 400, 'INVALID_REQUEST'),
            ({'version': '1.3.0-1', 'allow_downgrade': 1, 'password': PASSWORD},
             400, 'INVALID_REQUEST'),
        ]
        with patch('modules.web.auth.verify_password',
                   side_effect=lambda password, encoded: password == PASSWORD) as verify:
            for payload, expected, code in cases:
                with self.subTest(payload=payload):
                    status, _, body = self.request('POST', '/api/update/install', payload, headers)
                    self.assertEqual((status, body['code']), (expected, code), body)
                    if code == 'NOT_INSTALLABLE':
                        self.assertEqual(body['reason'], 'no_package')
            verify.assert_not_called()
        self.assertEqual(self.starts(), [])

    def test_install_without_a_release_list_and_with_downgrades_disabled(self):
        headers = self.auth()
        with patch('modules.web.auth.verify_password',
                   side_effect=lambda password, encoded: password == PASSWORD) as verify:
            self.assertEqual(self.request('POST', '/api/update/install',
                                          self.install_body('1.3.0-1'), headers)[0], 409)
            verify.assert_not_called()
            self.write_policy(allow_downgrade=False)
            self.write_state(releases=[_release('1.3.0-1'), _release('1.1.0-1')])
            status, _, body = self.request('POST', '/api/update/install',
                                           self.install_body('1.1.0-1', allow=True), headers)
            self.assertEqual((status, body['code']), (403, 'DOWNGRADE_DISABLED'))
            verify.assert_not_called()
        self.assertEqual(self.starts(), [])

    # -- password re-entry -------------------------------------------------
    def test_a_wrong_password_is_403_and_keeps_the_session(self):
        self.write_state(releases=[_release('1.3.0-1')])
        headers = self.auth()
        status, _, body = self.request('POST', '/api/update/install',
                                       self.install_body('1.3.0-1', password='wrong password'),
                                       headers)
        self.assertEqual((status, body['code']), (403, 'INVALID_PASSWORD'))
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.request('GET', '/api/update', headers=headers)[0], 200)

    def test_confirmations_and_logins_share_one_failure_window(self):
        self.write_state(releases=[_release('1.3.0-1')])
        headers = self.auth()
        for _ in range(5):
            status, _, body = self.request('POST', '/api/update/install',
                                           self.install_body('1.3.0-1', password='wrong one'),
                                           headers)
            self.assertEqual((status, body['code']), (403, 'INVALID_PASSWORD'))
        status, response_headers, body = self.request('POST', '/api/update/install',
                                                      self.install_body('1.3.0-1',
                                                                        password='wrong one'),
                                                      headers)
        self.assertEqual((status, body['code']), (429, 'RATE_LIMITED'))
        self.assertIn('Retry-After', response_headers)
        status, _, body = self.request('POST', '/api/login',
                                       {'username': 'admin', 'password': PASSWORD})
        self.assertEqual(status, 429, body)

    # -- successful starts -------------------------------------------------
    def test_install_starts_the_escaped_instance_and_enforces_a_start_grace(self):
        self.write_policy(channel='prerelease')
        self.write_state(releases=[_release('1.3.0~rc1-1', prerelease=True)])
        headers = self.auth()
        status, _, body = self.request('POST', '/api/update/install',
                                       self.install_body('1.3.0~rc1-1'), headers)
        self.assertEqual(status, 202, body)
        self.assertEqual(body['version'], '1.3.0~rc1-1')
        self.assertEqual(body['from_version'], '1.2.0-1')
        self.assertEqual(self.calls[-1], ['systemctl', 'start', '--no-block',
                                          'qmi-supervisor-update-install@1.3.0\\x7erc1\\x2d1.service'])
        status, _, body = self.request('POST', '/api/update/install',
                                       self.install_body('1.3.0~rc1-1'), headers)
        self.assertEqual((status, body['code']), (409, 'UPDATE_IN_PROGRESS'))

    def test_downgrade_requires_confirmation_and_the_setting(self):
        self.write_state(releases=[_release('1.1.0-1')])
        headers = self.auth()
        status, _, body = self.request('POST', '/api/update/install',
                                       self.install_body('1.1.0-1', allow=True), headers)
        self.assertEqual(status, 202, body)

    def test_rollback_requires_a_kept_version_and_the_password(self):
        headers = self.auth()
        status, _, body = self.request('POST', '/api/update/rollback',
                                       {'password': PASSWORD}, headers)
        self.assertEqual((status, body['code']), (409, 'NO_ROLLBACK'))
        self.write_state(releases=[], rollback={'available': True, 'version': '1.2.0-1'})
        status, _, body = self.request('POST', '/api/update/rollback',
                                       {'password': 'wrong password'}, headers)
        self.assertEqual((status, body['code']), (403, 'INVALID_PASSWORD'))
        self.assertEqual(self.starts(), [])
        status, _, body = self.request('POST', '/api/update/rollback',
                                       {'password': PASSWORD}, headers)
        self.assertEqual(status, 202, body)
        self.assertEqual(body['version'], '1.2.0-1')
        self.assertEqual(self.calls[-1], ['systemctl', 'start', '--no-block', ROLLBACK_UNIT])

    def test_a_failing_systemctl_start_is_a_503(self):
        self.start_returncode = 5
        self.write_state(releases=[_release('1.3.0-1')])
        status, _, body = self.request('POST', '/api/update/install',
                                       self.install_body('1.3.0-1'), self.auth())
        self.assertEqual((status, body['code']), (503, 'UPDATER_UNAVAILABLE'))

    def test_logs_record_the_request_but_never_secrets(self):
        self.write_state(releases=[_release('1.3.0-1')])
        headers = self.auth()
        with self.assertLogs('modules.web.updates', 'INFO') as captured:
            status, _, body = self.request('POST', '/api/update/install',
                                           self.install_body('1.3.0-1'), headers)
        lines = '\n'.join(captured.output)
        self.assertEqual(status, 202)
        self.assertEqual(sum('Web request: install' in line for line in captured.output), 1)
        for secret in (PASSWORD, headers['X-CSRF-Token'], headers['Cookie'].split('=', 1)[1]):
            self.assertNotIn(secret, lines)
            self.assertNotIn(secret, json.dumps(body))

    # -- settings policy ---------------------------------------------------
    def test_config_writes_cannot_loosen_update_verification(self):
        headers = self.auth()
        original = self.config.read_text()
        for changes, field in (({'update': {'require_signature': False}},
                                'update.require_signature'),
                               ({'update': {'source': 'http://127.0.0.1:9/'}},
                                'update.source')):
            with self.subTest(field=field):
                status, _, body = self.request('POST', '/api/config',
                                               {'config': changes}, headers)
                self.assertEqual(status, 400, body)
                self.assertEqual(body['field'], field)
                self.assertEqual(self.config.read_text(), original)
        self.write_policy(require_signature=False)
        status, _, body = self.request('POST', '/api/config',
                                       {'config': {'update': {'require_signature': True}}},
                                       headers)
        self.assertEqual(status, 200, body)

    def test_update_policy_changes_are_saved_and_reported(self):
        headers = self.auth()
        status, _, body = self.request('POST', '/api/config',
                                       {'config': {'update': {'mode': 'auto'}}}, headers)
        self.assertEqual(status, 200, body)
        self.assertIn('update', body['effects'])
        self.assertEqual(load_settings(self.config).update.mode, 'auto')


if __name__ == '__main__':
    unittest.main()
