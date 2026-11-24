"""Local plugin bus: handshake, capabilities, status, events and bounded queues."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from modules.deployment.provisioning import write_atomic
from modules.runtime.bus import BusServer, Client, write_secrets
from modules.runtime.fleet_config import Configuration
from modules.runtime.settings import Settings

SYNTHETIC_IMSI = '001010000000001'


class BusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root/'run'
        self.runtime.mkdir()
        self.telemetry = self.root/'telemetry'
        self.telemetry.mkdir()
        base = Settings()
        self.settings = base
        configuration = Configuration(replace(base, telemetry=replace(
            base.telemetry, directory=str(self.telemetry))), {})
        self.config = self.root/'settings.ini'
        write_atomic(self.config, configuration.render())
        self.secrets = self.root/'secrets.json'
        write_secrets({'demo': {'secret': 's3cret', 'capabilities': ['read:status', 'read:events']}},
                      self.secrets)
        profile = self.runtime/'modems'/'default'
        profile.mkdir(parents=True)
        (profile/'status.json').write_text(json.dumps({
            'id': 'default', 'state': 'ONLINE', 'supervisor_version': '1.3.0',
            'device': {'serial': 'sn-0123abcd'}, 'network': {}, 'diagnosis': None,
            'features': {}, 'sim': {'code': 'sim.no_atr'}, 'registration': None,
            'log_label': '18d94aeb',
            'identity': {'dms_value_imsi': SYNTHETIC_IMSI}}))
        (self.telemetry/'events.jsonl').write_text('')
        self.stop = threading.Event()
        self.server = BusServer(self.config, self.runtime, self.settings, self.stop,
                                secrets_path=self.secrets,
                                socket_path=self.root/'ext'/'bus.sock')
        self.server._next_ping = time.monotonic() + 100
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        for _ in range(50):
            if (self.root/'ext'/'bus.sock').exists():
                break
            time.sleep(0.02)
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.connection.close)
        self.connection.settimeout(3)
        self.connection.connect(str(self.root/'ext'/'bus.sock'))
        self.addCleanup(self.stop.set)
        self.addCleanup(self.server.close)

    def send(self, value):
        self.connection.sendall(json.dumps(value).encode() + b'\n')

    def receive(self):
        buffer = b''
        while True:
            while b'\n' not in buffer:
                chunk = self.connection.recv(4096)
                if not chunk:
                    raise AssertionError('bus closed the connection')
                buffer += chunk
            raw, buffer = buffer.split(b'\n', 1)
            message = json.loads(raw)
            if message.get('op') == 'ping':
                self.send({'op': 'pong'})
                continue
            return message

    def receive_raw(self):
        buffer = b''
        while b'\n' not in buffer:
            chunk = self.connection.recv(4096)
            if not chunk:
                raise AssertionError('bus closed the connection')
            buffer += chunk
        return json.loads(buffer.split(b'\n', 1)[0])

    def hello(self, token='s3cret'):
        self.send({'op': 'hello', 'protocol': 1, 'plugin': 'demo', 'token': token})
        return self.receive()

    def test_a_wrong_secret_is_refused_before_welcome(self):
        self.assertEqual(self.hello('wrong'), {'op': 'error', 'code': 'unauthorized'})
        self.assertEqual(self.connection.recv(1), b'', 'the bus closes an unauthorized client')

    def test_welcome_names_the_effective_capabilities_and_subscriptions_are_checked(self):
        self.assertEqual(self.hello(), {'op': 'welcome', 'capabilities': ['read:status', 'read:events'],
                                        'contract_version': 1})
        self.send({'op': 'subscribe', 'topics': ['status'], 'interval': 5})
        self.assertEqual(self.receive()['op'], 'subscribed')
        self.send({'op': 'subscribe', 'topics': ['metrics'], 'interval': 5})
        self.assertEqual(self.receive(), {'op': 'error', 'code': 'protocol'})

    def test_a_client_without_the_scope_cannot_subscribe(self):
        write_secrets({'demo': {'secret': 's3cret', 'capabilities': ['read:status']}}, self.secrets)
        self.hello()
        self.send({'op': 'subscribe', 'topics': ['events'], 'interval': 5})
        self.assertEqual(self.receive(), {'op': 'error', 'code': 'capability'})

    def test_status_pushes_the_contract_record_without_identities(self):
        self.hello()
        self.send({'op': 'subscribe', 'topics': ['status'], 'interval': 5})
        self.receive()
        self.server.poll()
        message = self.receive()
        self.assertEqual(message['op'], 'status')
        record = message['data']['modems'][0]
        self.assertEqual((record['id'], record['state'], record['sim']),
                         ('default', 'ONLINE', {'code': 'sim.no_atr'}))
        self.assertNotIn('identity', record)
        self.assertNotIn(SYNTHETIC_IMSI, json.dumps(message))

    def test_only_new_event_lines_are_broadcast(self):
        self.hello()
        self.send({'op': 'subscribe', 'topics': ['events'], 'interval': 5})
        self.receive()
        self.server.poll()
        event = {'schema_version': 1, 'seq': 7, 'at_utc': datetime.now(timezone.utc).isoformat(),
                 'instance': 'a', 'profile': 'default', 'modem': '18d94aeb',
                 'type': 'diagnosis.opened', 'data': {'code': 'sim.no_atr', 'severity': 'warning'}}
        (self.telemetry/'events.jsonl').write_text(json.dumps(event) + '\n')
        self.server.poll()
        message = self.receive()
        self.assertEqual((message['op'], message['data']['seq']), ('event', 7))

        rotated = dict(event, seq=8, instance='b')
        (self.telemetry/'events.jsonl').rename(self.telemetry/'events.jsonl.1')
        (self.telemetry/'events.jsonl').write_text(json.dumps(rotated) + '\n')
        self.server.poll()
        message = self.receive()
        self.assertEqual((message['op'], message['data']['seq']), ('event', 8))

    def test_ping_and_pong_keep_the_client_healthy(self):
        self.hello()
        self.server._next_ping = 0
        self.server.poll()
        self.assertEqual(self.receive_raw(), {'op': 'ping'})
        self.send({'op': 'pong'})
        self.server.poll()
        status = self.server.status()
        self.assertEqual([client['plugin'] for client in status['clients']], ['demo'])

    def test_oversized_messages_are_rejected(self):
        self.connection.sendall(b'x' * (65 * 1024) + b'\n')
        self.assertIn(self.receive().get('code'), {'message-too-large', 'invalid-json'})


class QueueTests(unittest.TestCase):
    def test_a_slow_client_loses_the_oldest_messages_and_is_told_once(self):
        class Connection:
            def sendall(self, data):
                pass
            def shutdown(self, how):
                pass
            def close(self):
                pass
        client = Client(Connection(), ('local',))
        for index in range(105):
            client.send({'op': 'event', 'index': index})
        self.assertGreaterEqual(client.drops_total, 5)
        queued = list(client.queue)
        self.assertTrue(any(item.get('op') == 'dropped' for item in queued))
        self.assertEqual(queued[-1]['index'], 104)


class ExtensionBusTests(unittest.TestCase):
    """Granted actions, plugin issues and declarative cards."""

    def setUp(self):
        from modules.runtime.plugins import set_grant
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root/'run'
        self.runtime.mkdir()
        self.telemetry = self.root/'telemetry'
        self.telemetry.mkdir()
        base = Settings()
        configuration = Configuration(replace(base, telemetry=replace(
            base.telemetry, directory=str(self.telemetry))), {})
        self.config = self.root/'settings.ini'
        write_atomic(self.config, configuration.render())
        (self.runtime/'modems'/'default').mkdir(parents=True)
        (self.runtime/'modems'/'default'/'status.json').write_text(json.dumps(
            {'id': 'default', 'state': 'ONLINE'}))
        (self.telemetry/'events.jsonl').write_text('')
        self.secrets = self.root/'secrets.json'
        write_secrets({'demo': {'secret': 's3cret',
                                'capabilities': ['read:status', 'read:events', 'act:objective',
                                                 'act:reconnect', 'ui:issues', 'ui:cards']}},
                      self.secrets)
        self.manager = self._manager()
        self.stop = threading.Event()
        self.server = BusServer(self.config, self.runtime, base, self.stop,
                                secrets_path=self.secrets,
                                socket_path=self.root/'ext'/'bus.sock')
        self.server.manager_socket = self.root/'manager.sock'
        self.server._next_ping = time.monotonic() + 100
        thread = threading.Thread(target=self.server.serve, daemon=True)
        thread.start()
        for _ in range(50):
            if (self.root/'ext'/'bus.sock').exists():
                break
            time.sleep(0.02)
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.connection.close)
        self.connection.settimeout(3)
        self.connection.connect(str(self.root/'ext'/'bus.sock'))
        self.addCleanup(self.stop.set)
        self.addCleanup(self.server.close)

    def _manager(self):
        import socket as socket_module
        listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        listener.bind(str(self.root/'manager.sock'))
        listener.listen(4)
        listener.settimeout(0.2)
        requests = []
        def serve():
            while not getattr(self, 'stop', None) or not self.stop.is_set():
                try:
                    connection, _ = listener.accept()
                except (socket_module.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                with connection:
                    buffer = b''
                    while b'\n' not in buffer:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                    try:
                        requests.append(json.loads(buffer.split(b'\n', 1)[0]))
                        connection.sendall(b'{"ok":true,"error":null}\n')
                    except ValueError:
                        pass
        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(listener.close)
        return requests

    def send(self, value):
        self.connection.sendall(json.dumps(value).encode() + b'\n')

    def receive(self):
        buffer = b''
        while b'\n' not in buffer:
            chunk = self.connection.recv(4096)
            if not chunk:
                raise AssertionError('bus closed')
            buffer += chunk
        return json.loads(buffer.split(b'\n', 1)[0])

    def hello(self):
        self.send({'op': 'hello', 'protocol': 1, 'plugin': 'demo', 'token': 's3cret'})
        self.assertEqual(self.receive()['op'], 'welcome')

    def test_an_action_needs_the_effective_capability_and_is_audited(self):
        self.hello()
        self.send({'op': 'request', 'id': '1', 'action': 'objective', 'modem': 'default',
                   'objective': 'telemetry', 'enabled': False})
        reply = self.receive()
        self.assertEqual((reply['op'], reply['ok'], reply['error']), ('result', True, None))
        self.assertTrue(any(item.get('action') == 'objective' and item.get('modem') == 'default'
                            for item in self.manager))
        deadline = time.monotonic() + 3
        while not any(item.get('action') == 'audit' for item in self.manager) \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(any(item.get('action') == 'audit' for item in self.manager))
        write_secrets({'demo': {'secret': 's3cret', 'capabilities': ['read:status']}}, self.secrets)
        self.connection.close()
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection.settimeout(3)
        self.connection.connect(str(self.root/'ext'/'bus.sock'))
        self.hello()
        self.send({'op': 'request', 'id': '2', 'action': 'reconnect', 'modem': 'default'})
        reply = self.receive()
        self.assertEqual((reply['ok'], reply['error']), (False, 'refused'))

    def test_reconnect_is_limited_per_modem(self):
        self.hello()
        for index in range(2):
            self.send({'op': 'request', 'id': str(index), 'action': 'reconnect',
                       'modem': 'default'})
            reply = self.receive()
        self.assertEqual(reply['error'], 'rate_limited')

    def test_text_limits_and_card_ttl(self):
        self.hello()
        self.send({'op': 'issue', 'id': 'backhaul', 'modem': 'default', 'tone': 'warning',
                   'title': 'Backhaul unreachable', 'text': '3 checks failed',
                   'action': 'Check the router', 'active': True})
        self.assertEqual(self.receive(), {'op': 'ok', 'id': 'backhaul'})
        self.send({'op': 'issue', 'id': 'bad', 'modem': 'default', 'tone': 'error',
                   'title': 'x', 'active': True})
        self.assertEqual(self.receive(), {'op': 'error', 'code': 'protocol'})
        self.send({'op': 'card', 'id': 'signal', 'modem': 'default', 'title': 'Site',
                   'rows': [{'label': 'Ping', 'value': '23 ms', 'tone': 'good'}],
                   'link': {'label': 'More', 'href': 'https://example.invalid/'}, 'ttl': 60})
        self.assertEqual(self.receive(), {'op': 'ok', 'id': 'signal'})
        self.send({'op': 'card', 'id': 'bad', 'title': 'x', 'rows': [],
                   'link': {'label': 'More', 'href': 'javascript:alert(1)'}, 'ttl': 60})
        self.assertEqual(self.receive(), {'op': 'error', 'code': 'protocol'})
        self.server._publish_extensions()
        published = json.loads((self.runtime/'extensions.json').read_text())
        self.assertEqual(published['issues'][0]['title'], 'Backhaul unreachable')
        self.assertEqual(published['cards'][0]['link']['href'], 'https://example.invalid/')

    def published(self):
        return json.loads((self.runtime/'extensions.json').read_text())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        self.fail('condition not reached')

    def test_a_started_bus_publishes_a_clean_state(self):
        self.wait_for(lambda: self.published()['issues'] == [] and self.published()['cards'] == [])

    def test_issues_and_cards_leave_with_the_plugin_and_with_the_bus(self):
        self.hello()
        self.send({'op': 'issue', 'id': 'backhaul', 'modem': 'default', 'tone': 'warning',
                   'title': 'Backhaul unreachable', 'active': True})
        self.assertEqual(self.receive(), {'op': 'ok', 'id': 'backhaul'})
        self.send({'op': 'card', 'id': 'site', 'modem': None, 'title': 'Site',
                   'rows': [{'label': 'Ping', 'value': '23 ms'}], 'ttl': 60})
        self.assertEqual(self.receive(), {'op': 'ok', 'id': 'site'})
        self.assertEqual(len(self.published()['issues']), 1)
        # A disabled or crashed plugin must not leave its warning on a modem.
        self.connection.close()
        self.wait_for(lambda: self.published()['issues'] == [] and self.published()['cards'] == [])
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection.settimeout(3)
        self.connection.connect(str(self.root/'ext'/'bus.sock'))
        self.hello()
        self.send({'op': 'issue', 'id': 'again', 'modem': None, 'tone': 'info',
                   'title': 'Back', 'active': True})
        self.assertEqual(self.receive(), {'op': 'ok', 'id': 'again'})
        self.server.close()
        self.assertFalse((self.runtime/'extensions.json').exists())
