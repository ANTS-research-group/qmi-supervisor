from dataclasses import replace
import http.client
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from modules.runtime.settings import Settings, load_settings
from modules.web.auth import hash_password
from modules.web import server as web_server
from modules.web.server import MAX_BODY, SESSION_COOKIE, _Handler, build_server, serve, snapshot_freshness
from modules.web.tools import DiagnosticRunner
from tests.web.static_fixture import make_export


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "project.ini"
        self.snapshot = self.root / "status.json"
        self.settings = Settings()
        self.password = "test password 2026"
        self.encoded = hash_password(self.password)
        self.static_root = make_export(self.root / 'frontend/out')
        self.servers = []
        self.addCleanup(self.close_servers)

    def close_servers(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def start(self, **web):
        web.setdefault('password_hash', self.encoded)
        settings = replace(self.settings, web=replace(self.settings.web, enabled=True, **web))
        server = build_server(settings, self.config, self.snapshot, server_address=("127.0.0.1", 0),
                              static_root=self.static_root)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    def auth(self, server):
        """A real signed-in session: cookie plus CSRF, as the browser holds them."""
        session = server.auth.login('admin', self.password, 'test-client')
        return {'Cookie': f'{SESSION_COOKIE}={session["session_id"]}', 'X-CSRF-Token': session['csrf_token']}

    def request(self, server, method, path, payload=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(connection.close)
        headers = dict(headers or {})
        if payload is not None:
            raw = json.dumps(payload)
            headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, raw, headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body) if body else None

    def test_loopback_without_credentials_cannot_read_or_write_private_apis(self):
        server = self.start(password_hash='')
        self.snapshot.write_text(json.dumps({'state':'ONLINE', 'sample':{'private':'private-marker'}}))
        self.assertEqual(self.request(server, "GET", "/api/status")[0], 401)
        status, body = self.request(server, "GET", "/api/config")
        self.assertEqual(status, 401)
        self.assertNotIn('config', body)
        self.assertNotIn('private-marker', json.dumps(body))
        self.assertEqual(self.request(server, "POST", "/api/config", {"config": {"modem": {"apn": "private"}}})[0], 401)
        status, body = self.request(server, 'GET', '/api/session')
        self.assertEqual(status, 200)
        self.assertFalse(body['authenticated'])
        self.assertFalse(body['readonly'])
        self.assertEqual(body['methods'], {'password': False})
        self.assertFalse(self.config.exists())

    def test_requests_without_a_session_are_rejected(self):
        server = self.start()
        self.assertEqual(self.request(server, "GET", "/api/config")[0], 401)
        self.assertEqual(self.request(server, "GET", "/api/status",
                                         headers={"Authorization": "Bearer test-secret"})[0], 401)
        status, body = self.request(server, "GET", "/api/config", headers=self.auth(server))
        self.assertEqual(status, 200)
        self.assertTrue(body["editable"])
        self.assertNotIn(self.password, json.dumps(body))

    def test_diagnostics_reject_bad_input_and_run_one_process(self):
        server = self.start()
        self.assertEqual(self.request(server, "GET", "/api/tools")[0], 401)
        status, body = self.request(server, "GET", "/api/tools", headers=self.auth(server))
        self.assertEqual(status, 200)
        self.assertFalse(body["tools"]["running"])
        self.assertEqual(set(body["capabilities"]), {"ping", "iperf3", "iperf3_bind_dev", "ping_options", "ping_pmtu_probe", "iperf3_options", "congestion"})
        for payload in ({"kind": "shell"}, {"kind": "ping", "target": "bad target"}, {"kind": "ping", "target": "1.1.1.1", "count": 0},
                        {"kind": "iperf", "port": 80}, {"kind": "ping", "target": "1.1.1.1", "extra": 1},
                        {"kind": "iperf", "mode": "both"}, {"kind": "iperf", "mode": "client"},
                        {"kind": "iperf", "mode": "client", "target": "1.1.1.1", "duration": 0},
                        {"kind": "ping", "target": "1.1.1.1", "flood": True}, {"kind": "iperf", "mode": "client", "target": "1.1.1.1", "file": "/etc/shadow"},
                        ["ping"]):
            with self.subTest(payload=payload):
                self.assertEqual(self.request(server, "POST", "/api/tools/start", payload, self.auth(server))[0], 400)
        self.assertEqual(self.request(server, "POST", "/api/tools/stop", {}, self.auth(server))[0], 200)

    def test_system_log_requires_a_session_and_reads_the_unit_journal(self):
        server = self.start()
        self.assertEqual(self.request(server, "GET", "/api/logs")[0], 401)
        completed = SimpleNamespace(returncode=0, stdout="2026-10-26T10:00:00+0000 line one\nline two\n", stderr="")
        with patch("modules.web.server.subprocess.run", return_value=completed) as runner:
            status, body = self.request(server, "GET", "/api/logs?lines=50", headers=self.auth(server))
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["2026-10-26T10:00:00+0000 line one", "line two"])
        self.assertEqual(runner.call_args.args[0][:3], ["journalctl", "--unit", "qmi-supervisor"])
        self.assertIn("500", self.request(server, "GET", "/api/logs?lines=0", headers=self.auth(server))[1]["error"])

    def test_snapshot_is_read_without_commands_or_modem_queries(self):
        server = self.start()
        headers = self.auth(server)
        snapshot = {"state": "ONLINE", "sample": {"dynamic": {"rsrp": -95}}, "timing": {"duration": 0.1}}
        self.snapshot.write_text(json.dumps(snapshot))
        with patch("subprocess.run", side_effect=AssertionError("No commands permitted")):
            status, body = self.request(server, "GET", "/api/status", headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(body['snapshot'], snapshot)
            self.assertIsNone(body['freshness']['state_fresh'])
        self.snapshot.write_text("partially written")
        status, body = self.request(server, "GET", "/api/status", headers=headers)
        self.assertEqual(status, 200)
        self.assertIsNone(body["snapshot"])

    def test_revision_conflicts_do_not_overwrite_a_newer_configuration(self):
        server = self.start()
        headers = self.auth(server)
        status, original = self.request(server, 'GET', '/api/config', headers=headers)
        self.assertEqual(status, 200)
        first_headers = dict(headers, **{'If-Match': original['revision']})
        status, saved = self.request(server, 'POST', '/api/config',
                                    {'config': {'modem': {'apn': 'private'}}}, headers=first_headers)
        self.assertEqual(status, 200)
        self.assertNotEqual(saved['revision'], original['revision'])
        before = self.config.read_bytes()
        status, failure = self.request(server, 'POST', '/api/config',
                                      {'config': {'modem': {'apn': 'stale'}}}, headers=first_headers)
        self.assertEqual(status, 412)
        self.assertEqual(failure['code'], 'CONFIGURATION_CONFLICT')
        self.assertEqual(self.config.read_bytes(), before)

    def test_telemetry_only_patch_identifies_its_effect_and_preserves_connection(self):
        server = self.start()
        status, body = self.request(server, 'POST', '/api/config',
            {'config': {'telemetry': {'enabled': False}}}, headers=self.auth(server))
        self.assertEqual(status, 200)
        self.assertEqual(body['effects'], ['telemetry'])
        self.assertEqual(load_settings(self.config).modem, self.settings.modem)
        self.assertFalse(body['restart_required'])

    def test_validation_errors_include_a_stable_code_and_field(self):
        server = self.start()
        status, body = self.request(server, 'POST', '/api/config',
            {'config': {'network': {'route_metric': -1}}}, headers=self.auth(server))
        self.assertEqual(status, 400)
        self.assertEqual(body['code'], 'INVALID_CONFIGURATION')
        self.assertEqual(body['field'], 'network.route_metric')

    def test_valid_partial_config_is_atomic_private_and_preserves_password_hash(self):
        server = self.start()
        encoded = self.encoded
        self.config.write_text(f"[modem]\napn = old\n[web]\npassword_hash = {encoded}\n")
        status, body = self.request(server, "POST", "/api/config", {"config": {
            "modem": {"apn": "private.example"}, "telemetry": {"interval": 5.5}}},
            {**self.auth(server), "Origin": f"http://127.0.0.1:{server.server_port}"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["restart_required"])
        self.assertIn("revision", body)
        self.assertIn("effects", body)
        self.assertTrue(body["auto_apply"])
        saved = load_settings(self.config)
        self.assertEqual(saved.modem.apn, "private.example")
        self.assertEqual(saved.web.password_hash, encoded)
        self.assertEqual(saved.telemetry.interval, 5.5)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertNotIn("password_hash", body["config"]["web"])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ["frontend", "project.ini", "web-sessions.json"])
        self.assertEqual(stat.S_IMODE((self.root/'web-sessions.json').stat().st_mode), 0o600)

    def test_invalid_configuration_does_not_change_existing_file(self):
        server = self.start()
        original = f"[modem]\napn = original\n[web]\npassword_hash = {self.encoded}\n"
        self.config.write_text(original)
        payloads = [
            {"network": {"route_metric": -1}}, {"network": {"route_metric": True}},
            {"telemetry": {"interval": "5"}}, {"web": {"enabled": "yes"}},
            {"modem": {"apnn": "typo"}}, {"other": {}}, {"modem": {"apn": "bad\noption = x"}},
            {"web": {"port": 70000}},
        ]
        for changes in payloads:
            with self.subTest(changes=changes):
                status, body = self.request(server, "POST", "/api/config", {"config": changes}, self.auth(server))
                self.assertEqual(status, 400, body)
                self.assertEqual(self.config.read_text(), original)

    def test_json_duplicates_and_nan_are_rejected_without_writes(self):
        server = self.start()
        for raw in ('{"config":{"modem":{"apn":"x","apn":"y"}}}', '{"config":{"telemetry":{"interval":NaN}}}'):
            status, body = self.request(server, "POST", "/api/config",
                headers={**self.auth(server), "Content-Type": "application/json"}, raw=raw)
            self.assertEqual(status, 400, body)
            self.assertFalse(self.config.exists())

    def test_oversized_payload_is_rejected_before_reading(self):
        server = self.start()
        status, _ = self.request(server, "POST", "/api/config", headers={
            **self.auth(server), "Content-Type": "application/json", "Content-Length": str(MAX_BODY + 1)}, raw="")
        self.assertEqual(status, 413)
        self.assertFalse(self.config.exists())

    def test_cross_origin_and_dns_rebinding_are_rejected(self):
        server = self.start()
        base = self.auth(server)
        for extra in ({"Origin": "https://evil.example"}, {"Host": f"evil.example:{server.server_port}"}, {"Origin": "null"}):
            status, _ = self.request(server, "POST", "/api/config", {"config": {}}, {**base, **extra})
            self.assertEqual(status, 403)
        self.assertEqual(self.request(server, "GET", "/api/status", headers={"Host": f"evil.example:{server.server_port}", **base})[0], 403)
        self.assertFalse(self.config.exists())

    def test_non_loopback_requires_a_web_password_at_start(self):
        with self.assertRaises(ValueError):
            settings = replace(self.settings, web=replace(self.settings.web, host="0.0.0.0", password_hash=''))
            build_server(settings, self.config, self.snapshot, server_address=("127.0.0.1", 0))
        server = self.start(host="0.0.0.0")
        self.assertEqual(self.request(server, "GET", "/api/status")[0], 401)

    def test_unknown_routes_do_not_expose_files(self):
        server = self.start()
        headers = self.auth(server)
        self.assertEqual(self.request(server, "GET", "/../../etc/passwd", headers=headers)[0], 404)
        self.assertEqual(self.request(server, "GET", "/api/status?modem=../../etc/passwd", headers=headers)[0], 400)

    def test_frozen_snapshot_and_recent_resource_publish_do_not_refresh_heartbeat(self):
        server = self.start()
        headers = self.auth(server)
        snapshot = {'state': 'ONLINE', 'boot_id': server.boot_id,
                    'heartbeat_monotonic': 10, 'published_monotonic': 29,
                    'resources': {'sampled_monotonic': 29},
                    'sample': {'timing': {'acquired_end_monotonic': 9}}}
        self.snapshot.write_text(json.dumps(snapshot))
        for now, expected in ((30, 20000), (50, 40000)):
            with patch('modules.web.server.snapshot_freshness',
                       side_effect=lambda s, b: snapshot_freshness(s, b, now=now)):
                status, body = self.request(server, 'GET', '/api/status', headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(body['snapshot']['state'], 'ONLINE')
            self.assertEqual(body['freshness']['heartbeat_age_ms'], expected)
            self.assertFalse(body['freshness']['state_fresh'])
        self.assertEqual(json.loads(self.snapshot.read_text()), snapshot)

    def test_public_assets_are_explicit_and_do_not_require_a_session(self):
        server = self.start()
        # The shell and its scripts/styles have no device data or credentials.
        for path, mime in (('/', 'text/html'), ('/dashboard/', 'text/html'),
                           ('/_next/static/chunks/0123456789abcdef.js', 'application/javascript'),
                           ('/_next/static/css/abcdef0123456789.css', 'text/css')):
            with self.subTest(path=path):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                self.addCleanup(connection.close)
                connection.request('GET', path)
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertIn(mime, response.getheader('Content-Type'))
                self.assertNotIn(self.password.encode(), body)
        for path in ('/../frontend/app.js', '/app.js?debug=1', '/settings.py', '/status.json'):
            self.assertIn(self.request(server, 'GET', path)[0], (401, 404))

    def test_auto_listeners_share_one_diagnostic_runner_and_stop_it_on_exit(self):
        created, stop = [], threading.Event()
        real_build = build_server

        def build(settings, *args, **kwargs):
            # Ephemeral ports keep the test off the real 8080.
            server = real_build(settings, *args, server_address=(settings.web.host, 0), **kwargs)
            created.append(server)
            return server

        settings = replace(self.settings, web=replace(self.settings.web, enabled=True, host='auto', password_hash=self.encoded))
        with patch.object(web_server, 'build_server', build), patch.object(DiagnosticRunner, 'stop') as stopped:
            thread = threading.Thread(target=serve, args=(settings, self.config, self.snapshot, stop),
                                      kwargs={'address_resolver': lambda: ('eth9', '127.0.0.2'), 'refresh_interval': 60,
                                              'static_root': self.static_root}, daemon=True)
            thread.start()
            for _ in range(200):
                if len(created) == 2:
                    break
                threading.Event().wait(0.01)
            stop.set()
            thread.join(timeout=5)
        self.assertEqual([server.server_address[0] for server in created], ['127.0.0.1', '127.0.0.2'])
        self.assertIs(created[0].tools, created[1].tools, 'one test at a time for the whole host')
        stopped.assert_called_once()


class FreshnessTests(unittest.TestCase):
    def test_monotonic_age_ignores_utc_and_preserves_zero(self):
        snapshot = {'boot_id': 'boot', 'heartbeat_monotonic': 0, 'published_monotonic': 1,
                    'updated_at': '2099-01-01T00:00:00Z',
                    'sample': {'timing': {'acquired_end_monotonic': 2}}}
        result = snapshot_freshness(snapshot, 'boot', now=3)
        self.assertEqual(result, {'heartbeat_age_ms': 3000, 'snapshot_age_ms': 2000,
                                  'sample_age_ms': 1000, 'state_fresh': True})

    def test_another_boot_or_unverifiable_clock_is_unknown(self):
        for saved_boot, current_boot in (('old', 'new'), ('boot', None), (None, 'boot')):
            result = snapshot_freshness({'boot_id': saved_boot, 'heartbeat_monotonic': 1},
                                        current_boot, now=2)
            self.assertIsNone(result['heartbeat_age_ms'])
            self.assertIsNone(result['state_fresh'])


class AnonymousAccessTests(unittest.TestCase):
    """Exercise rejection before any private data read or system operation."""
    def handler(self, path):
        handler = object.__new__(_Handler)
        handler.path = path
        handler.headers = http.client.HTTPMessage()
        handler.server = SimpleNamespace(config_lock=threading.Lock(), loopback=True,
            status_path=Mock(), auth=Mock())
        handler.server.auth.authenticate.return_value = None
        handler._host_allowed = Mock(return_value=True)
        handler._settings = Mock(return_value=Settings())
        handler._send = Mock()
        handler._read_payload = Mock(side_effect=AssertionError('Unauthenticated body must not be read'))
        handler.server.status_path.open.side_effect = AssertionError('Private status must not be read')
        return handler

    def test_private_api_has_no_anonymous_loopback_exception(self):
        with patch('subprocess.run', side_effect=AssertionError('No system/QMI commands permitted')), \
                patch('modules.web.server._public_config', side_effect=AssertionError('Private config must not be exposed')):
            for method, path in (('GET', '/api/status'), ('GET', '/api/config'),
                                 ('POST', '/api/config'), ('POST', '/api/logout')):
                with self.subTest(method=method, path=path):
                    handler = self.handler(path)
                    handler.server.static_root = Path('/nonexistent-public-export')
                    getattr(handler, 'do_' + method)()
                    self.assertEqual(handler._send.call_args.args, (401, {'error':'Authentication required', 'code':'AUTHENTICATION_REQUIRED'}))
                    handler.server.status_path.open.assert_not_called()
                    handler._read_payload.assert_not_called()

    def test_public_session_reports_setup_state_without_private_data_or_readonly_access(self):
        handler = self.handler('/api/session')
        handler.server.static_root = Path('/nonexistent-public-export')
        handler.do_GET()
        status, body = handler._send.call_args.args
        self.assertEqual(status, 200)
        self.assertFalse(body['authenticated'])
        self.assertFalse(body['readonly'])
        self.assertEqual(body['methods'], {'password': False})
        self.assertIsNone(body['username'])
        self.assertIsNone(body['csrf_token'])
        self.assertNotIn('config', body)
        self.assertNotIn('snapshot', body)
        for timestamp in (True, '1', -1, float('nan'), float('inf'), 3, 10**400):
            result = snapshot_freshness({'boot_id': 'boot', 'heartbeat_monotonic': timestamp},
                                        'boot', now=2)
            self.assertIsNone(result['state_fresh'])


if __name__ == "__main__":
    unittest.main()
