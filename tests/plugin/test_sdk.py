"""Python SDK and the example webhook plugin against a fake bus."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT/'sdk'/'python'
sys.path.insert(0, str(SDK))

from qmi_supervisor_sdk import PluginError, connect  # noqa: E402
from qmi_supervisor_sdk.testing import FakeBus  # noqa: E402


class SdkTests(unittest.TestCase):
    def test_connect_reads_the_environment_and_receives_messages(self):
        with FakeBus(settings={'url': 'https://example.invalid/hook'}) as fake:
            bus = connect()
            self.assertEqual(bus.name, 'test-plugin')
            self.assertEqual(set(bus.capabilities), {'read:status', 'read:events'})
            self.assertEqual(bus.settings(), {'url': 'https://example.invalid/hook'})
            bus.subscribe(('events',), interval=5)
            deadline = time.monotonic() + 3
            while not fake.subscriptions and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(fake.subscriptions[-1]['topics'], ['events'])
            fake.push_event({'seq': 3, 'type': 'diagnosis.opened', 'data': {}})
            message = next(bus.messages())
            self.assertEqual((message['op'], message['data']['seq']), ('event', 3))
            bus.close()

    def test_pings_are_answered_and_do_not_appear_as_messages(self):
        with FakeBus() as fake:
            bus = connect()
            bus.subscribe(('status',))
            fake.ping()
            fake.push_status([{'id': 'default', 'state': 'ONLINE'}])
            message = next(bus.messages())
            self.assertEqual(message['op'], 'status')
            self.assertEqual(message['data']['modems'][0]['id'], 'default')
            bus.close()

    def test_actions_require_a_capability_until_they_are_granted(self):
        with FakeBus() as fake:
            bus = connect()
            for call in (lambda: bus.request('reconnect', modem='default'),
                         lambda: bus.card('signal', title='x'),
                         lambda: bus.issue('backhaul', title='x')):
                with self.assertRaises(PluginError) as raised:
                    call()
                self.assertEqual(raised.exception.code, 'capability')
            bus.close()

    def test_a_wrong_secret_is_unauthorized(self):
        with FakeBus(secret='server-secret') as fake:
            os.environ['QMI_SUPERVISOR_PLUGIN_SECRET'] = 'wrong-secret'
            with self.assertRaises(PluginError) as raised:
                connect()
            self.assertEqual(raised.exception.code, 'unauthorized')


class WebhookTests(unittest.TestCase):
    def test_the_webhook_plugin_posts_signed_events(self):
        received = []
        secret = 'hook-secret'

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                received.append((body, self.headers.get('X-QMI-Signature')))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *unused):
                return

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        event = {'schema_version': 1, 'seq': 11, 'at_utc': '2026-11-24T10:00:00+00:00',
                 'instance': 'a', 'profile': 'default', 'modem': '18d94aeb',
                 'type': 'diagnosis.opened', 'data': {'code': 'sim.no_atr'}}
        with FakeBus(capabilities=('read:events',),
                     settings={'url': f'http://127.0.0.1:{httpd.server_port}/hook',
                               'secret': secret}) as fake:
            environment = dict(os.environ, PYTHONPATH=str(SDK))
            process = subprocess.Popen(
                [sys.executable, str(ROOT/'examples'/'plugins'/'webhook'/'plugin.py')],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: process.poll() is None and process.terminate())
            fake.push_event(event)
            deadline = time.monotonic() + 10
            while not received and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertTrue(received, 'the webhook did not post the event')
        body, signature = received[0]
        expected = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(json.loads(body)['seq'], 11)
