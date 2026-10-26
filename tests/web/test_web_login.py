"""Real HTTP tests for account sessions, browser protections and migration."""
from dataclasses import replace
import http.client
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from modules.runtime.settings import Settings, load_settings, validate_settings
from modules.web.server import build_server, _write_config, _Handler
from modules.web.auth import hash_password, verify_password
from tests.web.static_fixture import make_export


class LoginHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "Test account passphrase 2026"
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.ini'
        self.static_root = make_export(self.root / 'frontend/out')
        self.settings = Settings()
        self.settings = replace(self.settings, web=replace(self.settings.web, enabled=True,
            username='admin', password_hash=self.password_hash))
        _write_config(self.path, self.settings)
        self.servers = []
        self.addCleanup(self.close)
        self.server = self.start()

    def start(self):
        server = build_server(load_settings(self.path), self.path, self.root/'status.json',
                              server_address=('127.0.0.1', 0), static_root=self.static_root)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    def close(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def request(self, method, path, payload=None, headers=None, server=None):
        server = server or self.server
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        headers = dict(headers or {})
        body = json.dumps(payload) if payload is not None else None
        if payload is not None:
            headers.setdefault('Content-Type', 'application/json')
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def login(self):
        status, headers, body = self.request('POST', '/api/login', {'username':'admin', 'password':self.password})
        self.assertEqual(status, 200, body)
        return {'Cookie':headers['Set-Cookie'].split(';', 1)[0], 'X-CSRF-Token':body['csrf_token']}, headers, body

    def test_login_cookie_and_secrets_are_private(self):
        status, _, public = self.request('GET', '/api/session')
        self.assertEqual(status, 200)
        self.assertFalse(public['authenticated'])
        self.assertTrue(public['methods']['password'])
        self.assertEqual(self.request('GET', '/api/config')[0], 401)
        auth, headers, body = self.login()
        self.assertTrue(body['authenticated'])
        self.assertEqual(body['username'], 'admin')
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/', 'Max-Age='):
            self.assertIn(flag, headers['Set-Cookie'])
        self.assertNotIn('session_id', body)
        status, _, config = self.request('GET', '/api/config', headers=auth)
        self.assertEqual(status, 200)
        self.assertTrue(config['editable'])
        serialized = json.dumps(config)
        self.assertNotIn(self.password_hash, serialized)
        self.assertNotIn('password_hash', serialized)
        self.assertNotIn(self.password, serialized)
        self.assertEqual(self.request('GET', '/api/session', headers=auth)[2]['csrf_token'], body['csrf_token'])

    def test_invalid_login_and_origin_cannot_create_session(self):
        for username, password in [('admin', 'wrong'), ('other', self.password)]:
            status, headers, body = self.request('POST', '/api/login', {'username':username, 'password':password})
            self.assertEqual(status, 401, body)
            self.assertNotIn('Set-Cookie', headers)
            self.assertNotIn(username, body['error'])
        for extra in ({'Origin':'https://evil.example'}, {'Origin':'null'}, {'Host':f'evil.example:{self.server.server_port}'}):
            self.assertEqual(self.request('POST', '/api/login', {'username':'admin','password':self.password}, extra)[0], 403)
        self.assertEqual(self.request('POST', '/api/login', {'username':'admin','password':self.password,'extra':1})[0], 400)

    def test_cookie_writes_require_csrf_and_logout_revokes_session(self):
        auth, _, _ = self.login()
        payload = {'config':{'modem':{'apn':'private'}}}
        for headers in ({'Cookie':auth['Cookie']}, dict(auth, **{'X-CSRF-Token':'wrong'}), dict(auth, Origin='https://evil.example')):
            self.assertEqual(self.request('POST', '/api/config', payload, headers)[0], 403)
        self.assertEqual(load_settings(self.path).modem.apn, 'internet')
        self.assertEqual(self.request('POST', '/api/config', payload, auth)[0], 200)
        self.assertEqual(self.request('GET', '/api/status', headers=auth)[0], 200)
        status, headers, _ = self.request('POST', '/api/logout', {}, auth)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.request('GET', '/api/config', headers=auth)[0], 401)

    def test_restart_preserves_session_but_password_change_revokes_it(self):
        auth, _, _ = self.login()
        # A fresh web process reads the same private state on this boot.
        second = self.start()
        self.assertEqual(self.request('GET', '/api/config', headers=auth, server=second)[0], 200)
        payload = {'config':{'web':{'password':'A different passphrase 2026'}}}
        status, _, result = self.request('POST', '/api/config', payload, auth)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.request('GET', '/api/config', headers=auth)[0], 401)
        self.assertEqual(self.request('GET', '/api/config', headers=auth, server=second)[0], 401)
        self.assertTrue(verify_password('A different passphrase 2026', load_settings(self.path).web.password_hash))
        self.assertEqual(self.request('POST', '/api/login', {'username':'admin','password':self.password})[0], 401)

    def test_blank_password_preserves_hash_and_hash_injection_is_rejected(self):
        auth, _, _ = self.login()
        status, _, body = self.request('POST', '/api/config', {'config':{'web':{'password':''}}}, auth)
        self.assertEqual(status, 200, body)
        self.assertEqual(load_settings(self.path).web.password_hash, self.password_hash)
        for options in ({'password_hash':self.password_hash}, {'password':False}, {'password':'short'}):
            self.assertEqual(self.request('POST', '/api/config', {'config':{'web':options}}, auth)[0], 400)
        self.assertEqual(load_settings(self.path).web.password_hash, self.password_hash)

    def test_bearer_headers_never_authenticate(self):
        headers = {'Authorization': 'Bearer stand-in-session'}
        self.assertFalse(self.request('GET', '/api/session', headers=headers)[2]['authenticated'])
        self.assertEqual(self.request('GET', '/api/status', headers=headers)[0], 401)
        self.assertEqual(self.request('POST', '/api/config',
            {'config':{'network':{'route_metric':100}}}, headers)[0], 401)

    def test_reset_while_request_body_arrives_prevents_the_pending_write(self):
        auth, _, _ = self.login()
        reading = threading.Event()
        original = _Handler._read_payload
        def observe(handler):
            reading.set()
            return original(handler)
        body = json.dumps({'config':{'modem':{'apn':'unauthorized'}}}).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        with patch.object(_Handler, '_read_payload', observe):
            connection.putrequest('POST', '/api/config')
            for key, value in dict(auth, **{'Content-Type':'application/json','Content-Length':str(len(body))}).items():
                connection.putheader(key, value)
            connection.endheaders()
            connection.send(body[:1])
            self.assertTrue(reading.wait(2))
            with self.server.config_lock:
                changed = replace(self.settings, web=replace(self.settings.web, username='replacement'))
                _write_config(self.path, changed)
            connection.send(body[1:])
            response = connection.getresponse()
            self.assertEqual(response.status, 401, response.read())
        self.assertEqual(load_settings(self.path).modem.apn, 'internet')

    def test_settings_accept_password_for_lan_and_reject_invalid_accounts(self):
        validate_settings(replace(self.settings, web=replace(self.settings.web, host='auto')))
        for options in ({'username':''}, {'username':'a b'}, {'username':'a'*65}, {'password_hash':'plain'}, {'session_timeout':0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validate_settings(replace(self.settings, web=replace(self.settings.web, **options)))

    def test_check_config_output_redacts_credentials(self):
        settings = replace(self.settings, web=replace(self.settings.web, username='operator'))
        _write_config(self.path, settings)
        result = subprocess.run([sys.executable, 'qmi_supervisor.py', '--config', str(self.path), '--check-config'],
                                capture_output=True, text=True, check=True,
                                cwd=Path(__file__).resolve().parents[2])
        self.assertNotIn(self.password_hash, result.stdout)
        self.assertNotIn(self.password, result.stdout)
        self.assertEqual(json.loads(result.stdout)['web']['password_hash'], '[hidden]')


if __name__ == '__main__':
    unittest.main()
