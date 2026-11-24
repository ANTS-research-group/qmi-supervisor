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
