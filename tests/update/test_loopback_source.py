"""End-to-end check against the in-process loopback release server (never GitHub)."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
from io import StringIO
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from modules.runtime import version as runtime_version
from modules.update import cli, releases, state, transport
from modules.update.state import UpdatePaths
from tools.update.test_release_server import make_server

NOW = datetime(2026, 11, 21, 3, 7, 41, tzinfo=timezone.utc)


class LoopbackSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release_dir = self.root / 'releases'
        self.content = os.urandom(1024)
        self.digest = self.build_release('v9.9.0')
        self.paths = UpdatePaths(config=self.root / 'u2.ini',
                                 state_dir=self.root / 'state',
                                 token=self.root / 'qmi-supervisor-update-token',
                                 lock=self.root / 'run/qmi-supervisor-update.lock')
        self.server = None
        self.thread = None
        self.addCleanup(self._stop)
        self.current = ['1.2.0-1']
        version = patch.object(runtime_version, 'installed_version', side_effect=lambda: self.current[0])
        version.start()
        self.addCleanup(version.stop)
        environment = patch.dict(os.environ, {transport.TEST_SOURCE_ENV: '1'}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('JOURNAL_STREAM', None)

    def build_release(self, tag):
        directory = self.release_dir / tag
        directory.mkdir(parents=True)
        name = f'qmi-supervisor_{tag.lstrip("v")}-1_all.deb'
        (directory / name).write_bytes(self.content)
        (directory / 'SHA256SUMS').write_text(f'{hashlib.sha256(self.content).hexdigest()}  {name}\n')
        (directory / 'SHA256SUMS.asc').write_text('dummy\n')
        return hashlib.sha256(self.content).hexdigest()

    def start(self, *, token=None):
        self.server = make_server(self.release_dir, token=token)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.paths.config.write_text(f'[update]\nsource = {self.server.releases_url}\n')

    def _stop(self):
        if self.server is not None and self.thread is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.server = None
        self.thread = None

    def run_cli(self, argv, *, stdin=None):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--config', str(self.paths.config), '--state-dir', str(self.paths.state_dir), *argv],
                            paths=self.paths, clock=lambda: NOW, stdin=stdin)
        return code, out.getvalue(), err.getvalue()

    def document(self):
        return state.load_state(self.paths.state)

    def test_first_check_stores_digest_and_second_is_not_modified(self):
        self.start()
        code, out, err = self.run_cli(['check'])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.document()['releases'][0]['deb']['sha256'], self.digest)
        self.assertEqual(self.server.log[-1]['status'], 200)
        code, out, err = self.run_cli(['check'])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.log[-1]['status'], 304)
        self.assertIn('update available: v9.9.0 (9.9.0-1)', out)

    def test_releases_request_headers_are_minimal(self):
        self.start()
        self.assertEqual(self.run_cli(['check'])[0], 0)
        entry = [item for item in self.server.log if item['path'].endswith('/releases')][-1]
        headers = {key.lower(): value for key, value in entry['headers'].items()}
        self.assertEqual(headers['user-agent'], 'qmi-supervisor/1.2.0-1')
        allowed = {'host', 'user-agent', 'accept', 'accept-encoding', 'connection',
                   'if-none-match', 'authorization', 'x-github-api-version'}
        self.assertLessEqual(set(headers), allowed)

    def test_token_authentication_and_asset_redirect(self):
        token = 'ghp_' + 'b' * 36
        self.start(token=token)
        code, _, _ = self.run_cli(['check'])
        self.assertEqual(code, 1)
        self.assertEqual(self.document()['check_error']['code'], 'unauthorized')
        with patch.object(cli, '_is_root', return_value=True):
            self.assertEqual(self.run_cli(['set-token'], stdin=StringIO(token + '\n'))[0], 0)
        code, _, err = self.run_cli(['check'])
        self.assertEqual(code, 0, err)
        entry = [item for item in self.server.log if item['path'].endswith('/releases')][-1]
        self.assertEqual({key.lower(): value for key, value in entry['headers'].items()}['authorization'],
                         'Bearer ' + token)

        deb = self.document()['releases'][0]['deb']
        response = transport.http_get(deb['api_url'], headers={'Accept': 'application/octet-stream'},
                                      token=token, limit=2 * 1024 * 1024)
        self.assertEqual(response.status, 200)
        asset_entry = [item for item in self.server.log if '/assets/' in item['path']][-1]
        self.assertEqual({key.lower(): value for key, value in asset_entry['headers'].items()}['authorization'],
                         'Bearer ' + token)
        download_entry = [item for item in self.server.log if item['path'].startswith('/download/')][-1]
        self.assertNotIn('authorization', {key.lower() for key in download_entry['headers']})

    def test_download_paths_cannot_escape_the_release_directory(self):
        self.start()
        base = self.server.base_url
        for path in ('/download/v9.9.0//etc/hostname',
                     '/download/v9.9.0/%2Fetc%2Fhostname',
                     '/download/v9.9.0/../../etc/hostname'):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(base + path, timeout=5)
                self.assertEqual(raised.exception.code, 404)

    def test_a_symlinked_asset_outside_the_release_directory_is_not_served(self):
        self.start()
        outside = self.root / 'secret.txt'
        outside.write_text('secret')
        (self.release_dir / 'v9.9.0' / 'linked.txt').symlink_to(outside)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.server.base_url + '/download/v9.9.0/linked.txt', timeout=5)
        self.assertEqual(raised.exception.code, 404)

    def test_an_out_of_range_http_status_is_recorded_as_null(self):
        self.start()
        self.server.fault = {'status': 999}
        code, _, err = self.run_cli(['check'])
        self.assertEqual(code, 1)
        self.assertIn('http_error', err)
        document = self.document()
        self.assertEqual(document['check_error']['code'], 'http_error')
        self.assertIsNone(document['check_error']['status'])

    def test_wrong_token_is_unauthorized(self):
        token = 'ghp_' + 'b' * 36
        self.start(token=token)
        with patch.object(cli, '_is_root', return_value=True):
            self.assertEqual(self.run_cli(['set-token'], stdin=StringIO('ghp_' + 'c' * 36 + '\n'))[0], 0)
        code, _, _ = self.run_cli(['check'])
        self.assertEqual(code, 1)
        self.assertEqual(self.document()['check_error']['code'], 'unauthorized')

    def test_loopback_source_requires_the_test_variable(self):
        self.start()
        os.environ.pop(transport.TEST_SOURCE_ENV, None)
        code, _, _ = self.run_cli(['check'])
        self.assertEqual(code, 1)
        self.assertEqual(self.document()['check_error']['code'], 'bad_source')
        self.assertEqual(self.server.log, [])

    def test_faults_map_to_check_error_codes(self):
        self.start()
        cases = (
            ({'status': 403, 'headers': {'X-RateLimit-Remaining': '0'}}, 'rate_limited', 403),
            ({'status': 404}, 'not_found', 404),
            ({'status': 500}, 'http_error', 500),
            ({'body': b'not json'}, 'bad_response', None),
            ({'body': b'[' * 100000}, 'bad_response', None),
            ({'body': b'x' * (2 * 1024 * 1024 + 1)}, 'too_large', None),
            ({'status': 302, 'headers': {'Location': 'http://example.invalid/'}}, 'bad_redirect', None),
        )
        for fault, expected_code, expected_status in cases:
            with self.subTest(fault=fault):
                self.server.fault = fault
                self.server.log.clear()
                code, _, _ = self.run_cli(['check'])
                self.assertEqual(code, 1)
                error = self.document()['check_error']
                self.assertEqual(error['code'], expected_code)
                self.assertEqual(error['status'], expected_status)

    def test_timeout_becomes_a_network_error(self):
        self.start()
        self.server.fault = {'delay': 2}
        with self.assertRaises(transport.UpdateCheckError) as caught:
            releases.fetch_releases(self.server.releases_url, timeout=0.5)
        self.assertEqual(caught.exception.code, 'network')


if __name__ == '__main__':
    unittest.main()
