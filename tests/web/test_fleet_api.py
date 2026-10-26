"""Authenticated profile transactions and scoped histories never mix UEs."""
from dataclasses import replace
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from modules.runtime.fleet_config import Configuration
from modules.runtime.settings import Settings
from modules.web.auth import hash_password
from modules.web.server import SESSION_COOKIE, build_server, _write_config
from tests.web.static_fixture import make_export


class FleetAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config, self.status = self.root/'settings.ini', self.root/'status.json'
        self.password = 'fleet api password'
        self.encoded = hash_password(self.password)
        defaults = Settings()
        base = replace(defaults, web=replace(defaults.web, enabled=True, password_hash=self.encoded))
        private = replace(base, modem=replace(base.modem, apn='tenten', serial='ue-2'))
        _write_config(self.config, Configuration(base, {'private': private}))
        self.server = build_server(base, self.config, self.status, server_address=('127.0.0.1', 0),
                                   static_root=make_export(self.root/'static'))
        session = self.server.auth.login('admin', self.password, 'test-client')
        self.session_headers = {'Cookie': f'{SESSION_COOKIE}={session["session_id"]}',
                                'X-CSRF-Token': session['csrf_token']}
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.directory.cleanup()

    def request(self, method, path, payload=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(payload) if payload is not None else None,
                headers={**self.session_headers, 'Content-Type': 'application/json', **headers})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_scoped_apn_edits_preserve_other_profiles_credentials_and_global_revision(self):
        status, original = self.request('GET', '/api/config?modem=private')
        self.assertEqual(status, 200)
        self.assertEqual(original['config']['modem']['apn'], 'tenten')
        self.assertNotIn('test-secret', json.dumps(original))
        status, saved = self.request('POST', '/api/config?modem=private',
            {'config': {'modem': {'apn': 'private-new'}}}, **{'If-Match': original['revision']})
        self.assertEqual(status, 200, saved)
        stored = Configuration.load(self.config)
        self.assertEqual(stored.base.modem.apn, 'internet')
        self.assertEqual(stored.profiles['private'].modem.apn, 'private-new')
        self.assertEqual(stored.base.web.password_hash, self.encoded)
        self.assertEqual(saved['revision'], stored.revision)
        status, _ = self.request('POST', '/api/config', {'config': {'telemetry': {'enabled': False}}},
                                 **{'If-Match': original['revision']})
        self.assertEqual(status, 412)
        status, _ = self.request('POST', '/api/config', {'config': {'modem': {'apn': 'base-new'}}})
        self.assertEqual(status, 200)
        self.assertEqual(Configuration.load(self.config).profiles['private'].modem.apn, 'private-new')

    def test_scoped_histories_are_isolated_and_unknown_or_malicious_ids_cannot_read_files(self):
        entries = [dict(id=identifier, detected=True, selector={'device': 'auto', 'serial': ''})
                   for identifier in ('default', 'private')]
        cleanup = [dict(profile='retired', state='WAIT_DEVICE', error='Owned cleanup pending')]
        self.status.write_text(json.dumps(dict(fleet=True, modems=entries, state='ONLINE', config_revision='fleet',
                                               ownership_cleanup=cleanup)))
        for identifier, sample in [('default', 41), ('private', 8)]:
            target = self.root/'modems'/identifier/'status.json'
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(dict(state='ONLINE', history=[{'sample_id': sample}],
                                             sample={'log_id': sample})))
        for identifier, sample in [('default', 41), ('private', 8)]:
            status, body = self.request('GET', '/api/status?modem='+identifier)
            self.assertEqual(status, 200)
            self.assertEqual(body['snapshot']['history'], [{'sample_id': sample}])
            self.assertEqual(body['snapshot']['selected_modem'], identifier)
            self.assertEqual(body['snapshot']['config_revision'], 'fleet')
            self.assertEqual(body['snapshot']['ownership_cleanup'], cleanup)
        for path, expected in [('/api/status?modem=unknown', 404), ('/api/status?modem=../private', 400),
                               ('/api/status?modem=private&modem=default', 400),
                               ('/api/config?modem=%2Fetc%2Fpasswd', 400)]:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], expected)

    def test_web_changes_are_shared_and_a_detected_auto_profile_can_be_saved(self):
        self.status.write_text(json.dumps(dict(modems=[dict(id='auto-new', detected=True,
            selector={'device': 'auto', 'serial': 'ue-3'})])))
        status, original = self.request('GET', '/api/config?modem=auto-new')
        self.assertEqual(status, 200)
        self.assertEqual(original['config']['modem']['serial'], 'ue-3')
        status, saved = self.request('POST', '/api/config?modem=auto-new',
            {'config': {'modem': {'apn': 'third'}, 'web': {'history_size': 60}}})
        self.assertEqual(status, 200, saved)
        configuration = Configuration.load(self.config)
        self.assertEqual(configuration.profiles['auto-new'].modem.apn, 'third')
        self.assertEqual(configuration.base.web.history_size, 60)
        self.assertEqual(configuration.profiles['private'].web.history_size, 60)

    def test_old_unscoped_snapshot_is_never_served_as_another_modems_data(self):
        self.status.write_text(json.dumps(dict(state='ONLINE', sample={'log_id': 99},
                                              history=[{'sample_id': 99}])))
        status, response = self.request('GET', '/api/status?modem=private')
        self.assertEqual(status, 200)
        self.assertIsNone(response['snapshot']['sample'])
        self.assertEqual(response['snapshot']['history'], [])
        status, response = self.request('GET', '/api/status?modem=default')
        self.assertEqual(status, 200)
        self.assertEqual(response['snapshot']['sample']['log_id'], 99)

    def test_requested_state_and_profile_application_errors_override_an_older_worker_snapshot(self):
        entry = dict(id='private', detected=True, device={'path': '/dev/synthetic'},
                     features={'connection': {'enabled': False, 'pending': True, 'effective': True}},
                     error='Profile application failed: synthetic write error')
        self.status.write_text(json.dumps(dict(fleet=True, modems=[entry])))
        target = self.root/'modems/private/status.json'
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(dict(state='ONLINE', sample={'log_id': 5},
            features={'connection': {'enabled': True, 'pending': False, 'effective': True}})))
        status, response = self.request('GET', '/api/status?modem=private')
        self.assertEqual(status, 200)
        value = response['snapshot']
        self.assertTrue(value['features']['connection']['pending'])
        self.assertFalse(value['features']['connection']['enabled'])
        self.assertTrue(value['features']['connection']['effective'])
        self.assertEqual(value['error'], entry['error'])
