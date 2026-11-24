"""E1.2/E1.3: token-authenticated /api/v1 endpoints and Prometheus /metrics."""
from dataclasses import replace
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from modules.deployment.provisioning import write_atomic
from modules.runtime.fleet_config import Configuration
from modules.runtime.settings import Settings
from modules.web.server import build_server
from modules.web.tokens import TokenStore
from tests.contract.schema_check import assert_valid
from tests.web.static_fixture import make_export

SCHEMAS = Path(__file__).resolve().parents[2]/'doc'/'schemas'


def schema(name):
    return json.loads((SCHEMAS/name).read_text())

SYNTHETIC_IMSI = '001010000000001'
SYNTHETIC_IMEI = '356938035643809'


class ApiV1Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root/'project.ini'
        self.status = self.root/'run'/'status.json'
        self.status.parent.mkdir(parents=True)
        self.telemetry = self.root/'telemetry'
        self.telemetry.mkdir()
        self.settings = replace(
            Settings(), web=replace(Settings().web, enabled=True, api=True),
            telemetry=replace(Settings().telemetry, directory=str(self.telemetry)))
        write_atomic(self.config, Configuration(self.settings, {}).render())
        self.snapshot()
        self.static_root = make_export(self.root/'frontend/out')
        self.servers = []
        self.addCleanup(self.close_servers)

    def close_servers(self):
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def snapshot(self):
        status = {'id': 'default', 'state': 'ONLINE', 'supervisor_version': '1.3.0',
                  'device': {'path': '/dev/cdc-wdm1', 'interface': 'wwan0',
                             'serial': 'sn-0123abcd'},
                  'network': {'address': '192.0.2.2/30', 'interface': 'wwan0'},
                  'diagnosis': {'code': 'sim.no_atr', 'severity': 'warning', 'active': True,
                                'since_utc': '2026-11-24T10:00:00+00:00', 'params': {},
                                'attempts': 4, 'failing_seconds': 240},
                  'features': {}, 'sim': {'code': 'sim.no_atr'}, 'registration': None,
                  'log_label': '18d94aeb',
                  'identity': {'dms_value_imsi': SYNTHETIC_IMSI,
                               'dms_value_imei': SYNTHETIC_IMEI,
                               'dms_value_sim_card_status': {'state': 'not_ready'}}}
        profile = self.status.parent/'modems'/'default'
        profile.mkdir(parents=True, exist_ok=True)
        (profile/'status.json').write_text(json.dumps(status))
        self.status.write_text(json.dumps({
            'supervisor_version': '1.3.0', 'modems': [{
                'id': 'default', 'configured': True, 'detected': True, 'modem_alive': True,
                'state': 'ONLINE', 'diagnosis': status['diagnosis'], 'log_label': '18d94aeb',
                'selector': {'device': 'auto', 'serial': '18d94aeb'},
                'device': status['device'], 'network': status['network'], 'features': {}}]}))
        (self.telemetry/'health.json').write_text(json.dumps({
            'schema_version': 1, 'updated_at_utc': '2026-11-24T10:00:00+00:00',
            'modem': '18d94aeb', 'profile': 'default', 'connection_state': 'BACKOFF',
            'supervisor_version': '1.3.0', 'last_sample_at_utc': datetime.now(timezone.utc).isoformat(),
            'diagnosis': None, 'stall': None, 'sim': {'code': 'sim.no_atr'}, 'registration': None}))
        lines = [json.dumps({'schema_version': 1, 'seq': index, 'instance': 'a',
                             'at_utc': '2026-11-24T10:00:00+00:00', 'profile': 'default',
                             'modem': '18d94aeb', 'type': 'connection.online',
                             'data': {'state': 'ONLINE'}}) for index in (1, 2, 3)]
        (self.telemetry/'events.jsonl').write_text('\n'.join(lines) + '\n')

    def start(self, api=True):
        settings = replace(self.settings, web=replace(self.settings.web, api=api))
        write_atomic(self.config, Configuration(settings, {}).render())
        server = build_server(settings, self.config, self.status,
                              server_address=('127.0.0.1', 0), static_root=self.static_root)
        server.tokens = TokenStore(self.root/'api-tokens.json')
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01},
                                  daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server

    def token(self, server, scopes=('read:status', 'read:events', 'read:metrics')):
        return server.tokens.create('test', list(scopes))

    def request(self, server, path, token=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        self.addCleanup(connection.close)
        values = dict(headers or {})
        if token is not None:
            values['Authorization'] = 'Bearer ' + token
        connection.request('GET', path, headers=values)
        response = connection.getresponse()
        body = response.read()
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = body.decode('utf-8', 'replace')
        return response.status, parsed, dict(response.getheaders())

    def test_every_v1_path_and_metrics_are_404_when_the_api_is_off(self):
        server = self.start(api=False)
        for path in ('/api/v1/modems', '/api/v1/system', '/api/v1/modems/default',
                     '/api/v1/modems/default/health', '/api/v1/modems/default/events', '/metrics'):
            with self.subTest(path=path):
                status, body, _ = self.request(server, path)
                self.assertEqual(status, 404)
                self.assertEqual(body['contract_version'], 1)

    def test_a_valid_token_reads_the_contract_objects_and_the_identities(self):
        server = self.start()
        token = self.token(server)
        status, body, _ = self.request(server, '/api/v1/modems', token)
        self.assertEqual(status, 200)
        self.assertEqual(body['contract_version'], 1)
        modem = body['data'][0]
        self.assertEqual((modem['id'], modem['state'], modem['serial_label']),
                         ('default', 'ONLINE', '18d94aeb'))
        self.assertEqual(modem['diagnosis']['code'], 'sim.no_atr')
        self.assertEqual(modem['identity']['serial'], '18d94aeb')
        self.assertEqual(modem['identity']['imsi'], SYNTHETIC_IMSI)
        self.assertEqual(modem['identity']['imei'], SYNTHETIC_IMEI)
        status, body, _ = self.request(server, '/api/v1/modems/default', token)
        self.assertEqual(status, 200)
        self.assertTrue(body['data']['supervisor_version'])
        self.assertEqual(body['data']['sim'], {'code': 'sim.no_atr'})
        self.assertEqual(body['data']['identity']['imsi'], SYNTHETIC_IMSI)
        assert_valid(self, body['data'], schema('modem-status.schema.json'),
                     'api v1 modem record')
        status, body, _ = self.request(server, '/api/v1/modems/default/health', token)
        self.assertEqual((status, body['data']['sim']), (200, {'code': 'sim.no_atr'}))
        assert_valid(self, body['data'], schema('health.schema.json'), 'api v1 health')
        status, body, _ = self.request(server, '/api/v1/system', token)
        self.assertEqual(status, 200)
        self.assertTrue(body['data']['supervisor_version'])
        self.assertEqual(set(body['data']['update']),
                         {'update_available', 'newest_installable', 'checked_utc', 'last_result'})

    def test_missing_invalid_and_under_scoped_tokens_are_refused(self):
        server = self.start()
        status, body, _ = self.request(server, '/api/v1/modems')
        self.assertEqual((status, body['error']['code']), (401, 'AUTHENTICATION_REQUIRED'))
        status, body, _ = self.request(server, '/api/v1/modems', 'qms_deadbeef_' + 'A' * 43)
        self.assertEqual((status, body['error']['code']), (401, 'INVALID_TOKEN'))
        events_only = self.token(server, ('read:events',))
        status, body, _ = self.request(server, '/api/v1/modems', events_only)
        self.assertEqual((status, body['error']['code']), (403, 'FORBIDDEN'))
        metrics_only = self.token(server, ('read:metrics',))
        status, body, _ = self.request(server, '/api/v1/modems', metrics_only)
        self.assertEqual(status, 403)

    def test_a_revoked_token_stops_working_immediately(self):
        server = self.start()
        token = self.token(server)
        self.assertEqual(self.request(server, '/api/v1/modems', token)[0], 200)
        server.tokens.revoke(token.split('_')[1])
        self.assertEqual(self.request(server, '/api/v1/modems', token)[0], 401)

    def test_cookie_sessions_do_not_authenticate_the_machine_api(self):
        server = self.start()
        status, _, _ = self.request(server, '/api/v1/modems',
                                    headers={'Cookie': 'qmi_session=not-a-session'})
        self.assertEqual(status, 401)
        token = self.token(server)
        status, _, _ = self.request(server, '/api/status', token)
        self.assertEqual(status, 401, 'browser endpoints never accept bearer tokens')

    def test_events_page_by_instance_and_sequence(self):
        server = self.start()
        token = self.token(server)
        status, body, _ = self.request(server, '/api/v1/modems/default/events', token)
        self.assertEqual(status, 200)
        self.assertEqual(body['data']['instance'], 'a')
        self.assertEqual([event['seq'] for event in body['data']['events']], [1, 2, 3])
        for event in body['data']['events']:
            assert_valid(self, event, schema('event.schema.json'), 'api v1 event')
        status, body, _ = self.request(server, '/api/v1/modems/default/events?after=1&limit=1', token)
        self.assertEqual([event['seq'] for event in body['data']['events']], [2])
        status, body, _ = self.request(server, '/api/v1/modems/default/events?instance=old', token)
        self.assertEqual(body['data'], {'instance': 'old', 'events': []})
        status, body, _ = self.request(
            server, '/api/v1/modems/default/events?limit=0', token)
        self.assertEqual((status, body['error']['code']), (400, 'INVALID_ARGUMENT'))

    def test_unknown_profiles_and_invalid_identifiers(self):
        server = self.start()
        token = self.token(server)
        self.assertEqual(self.request(server, '/api/v1/modems/unknown', token)[0], 404)
        self.assertEqual(self.request(server, '/api/v1/modems/..%2Fx', token)[0], 400)
        self.assertEqual(self.request(server, '/api/v1/nope', token)[0], 404)

    def test_metrics_expose_the_contract_values_without_secrets(self):
        server = self.start()
        token = self.token(server)
        status, body, headers = self.request(server, '/metrics', token)
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/plain; version=0.0.4'))
        self.assertIn('qmi_supervisor_info{version="', body)
        self.assertIn('qmi_supervisor_modem_up{modem="18d94aeb",profile="default"} 1', body)
        self.assertIn('qmi_supervisor_modem_state{modem="18d94aeb",profile="default",state="ONLINE"} 1', body)
        self.assertIn('code="sim.no_atr"', body)
        self.assertIn('qmi_supervisor_last_sample_age_seconds{modem="18d94aeb",profile="default"}', body)
        self.assertNotIn(SYNTHETIC_IMSI, body)
        self.assertNotIn(SYNTHETIC_IMEI, body)
        events_only = self.token(server, ('read:events',))
        self.assertEqual(self.request(server, '/metrics', events_only)[0], 403)

    def test_a_token_is_limited_to_ten_requests_per_second(self):
        server = self.start()
        token = self.token(server)
        with patch('modules.web.server.time.monotonic', return_value=100.0):
            codes = [self.request(server, '/api/v1/system', token)[0] for _ in range(11)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[-1], 429)
