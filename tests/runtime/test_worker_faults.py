"""Fault injection uses executable synthetic actors, never host routes or QMI."""
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from modules.deployment.provisioning import render_config, write_atomic
from modules.runtime.processes import ProcessManager
from modules.runtime.settings import Settings
from modules.web.auth import hash_password
from tests.web.static_fixture import make_export


class ExecutableFaultTests(unittest.TestCase):
    def test_storage_crashes_and_unresponsive_children_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            mode = root/'storage-mode'
            mode.write_text('healthy')
            environment = dict(os.environ, QMI_TEST_STORAGE_MODE_FILE=str(mode),
                               QMI_TEST_STATIC_ROOT=str(make_export(root/'static')))
            defaults = Settings()
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            password = 'synthetic-fault-password'
            settings = replace(defaults,
                telemetry=replace(defaults.telemetry, interval=.1, collection_budget=.08,
                                  queue_size=2, directory=str(root/'captures')),
                web=replace(defaults.web, enabled=True, port=port, password_hash=hash_password(password)))
            config = root/'settings.ini'
            write_atomic(config, render_config(settings))
            stop = threading.Event()
            def launch(command, **kwargs):
                command[2] = 'tests.runtime.actor_fixture'
                return subprocess.Popen(command, env=environment, **kwargs)
            manager = ProcessManager(config, settings, stop, runtime=runtime, popen=launch)
            outcome = []
            process = threading.Thread(target=lambda: outcome.append(manager.run()))
            process.start()
            def wait(condition, timeout=8):
                deadline = time.monotonic()+timeout
                while time.monotonic() < deadline:
                    if condition():
                        return
                    time.sleep(.05)
                self.fail('Executable platform did not recover within its deadline')
            def stats():
                return manager.children['telemetry'].status.get('stats', {})
            session = {}
            def request_status():
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                try:
                    if 'cookie' not in session:
                        body = json.dumps({'username': 'admin', 'password': password}).encode()
                        connection.request('POST', '/api/login', body, {'Content-Type': 'application/json'})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        session['cookie'] = (response.getheader('Set-Cookie') or '').split(';', 1)[0]
                        response.read()
                        connection.close()
                        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                    connection.request('GET', '/api/status', headers={'Cookie': session['cookie']})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())
                finally:
                    connection.close()
            try:
                wait(lambda: stats().get('persisted', 0) > 3)
                wait(lambda: manager.children['web'].status.get('phase') == 'RUNNING')
                modem = manager.children['modem'].process
                web = manager.children['web'].process
                self.assertIsNotNone(request_status()['snapshot'])
                mode.write_text('full')
                wait(lambda: stats().get('persistence_failed', 0) >= 3)
                acquired = stats()['acquired']
                wait(lambda: stats().get('acquired', 0) > acquired+3)
                self.assertEqual(manager.children['telemetry'].restarts, 0)
                self.assertLessEqual(stats()['last_write_sample_id'], stats()['last_sample_id'])
                self.assertFalse(stats()['last_write_success'])
                mode.write_text('slow')
                wait(lambda: stats().get('persistence_dropped', 0) > 0)
                wait(lambda: stats().get('last_write_success') is True)
                self.assertLessEqual(stats()['queued'], 2)
                self.assertEqual(manager.children['telemetry'].restarts, 0)
                old_telemetry = manager.children['telemetry'].process
                mode.write_text('blocked')
                wait(lambda: manager.children['telemetry'].restarts >= 1, timeout=43)
                mode.write_text('healthy')
                wait(lambda: manager.children['telemetry'].alive and stats().get('persisted', 0) > 2)
                self.assertIsNot(manager.children['telemetry'].process, old_telemetry)
                self.assertIs(manager.children['modem'].process, modem)
                self.assertIs(manager.children['web'].process, web)
                self.assertIsNone(modem.poll())
                self.assertIsNotNone(request_status()['snapshot'])
                # A modem crash is recoverable without replacing its consumers.
                telemetry = manager.children['telemetry'].process
                generation = manager.children['modem'].status['snapshot']['device_generation']
                modem.kill()
                wait(lambda: manager.children['modem'].restarts == 1)
                self.assertIs(manager.children['web'].process, web)
                self.assertIsNotNone(request_status()['snapshot'])
                wait(lambda: manager.children['modem'].alive)
                wait(lambda: manager.children['telemetry'].status.get('sample', {}).get('device_generation') not in {None, generation})
                self.assertIs(manager.children['telemetry'].process, telemetry)
                # A completely stopped web loop is reaped and restarted alone.
                os.kill(web.pid, signal.SIGSTOP)
                wait(lambda: manager.children['web'].restarts == 1, timeout=23)
                wait(lambda: manager.children['web'].alive and manager.children['web'].status.get('phase') == 'RUNNING')
                self.assertIs(manager.children['telemetry'].process, telemetry)
                self.assertIsNotNone(request_status()['snapshot'])
            finally:
                stop.set()
                process.join(30)
            self.assertFalse(process.is_alive())
            self.assertEqual(outcome, [0])
            self.assertEqual(config.read_text(), render_config(settings))
