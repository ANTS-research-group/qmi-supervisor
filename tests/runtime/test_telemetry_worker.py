"""The telemetry actor names a stall once and reports the connection reason."""
from dataclasses import replace
from pathlib import Path
import hashlib
import json
import logging
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modules.deployment.provisioning import render_config, write_atomic
from modules.device.device import Device
from modules.runtime.ipc import AcquisitionUnavailable, read_json
from modules.runtime.settings import Settings
from modules.runtime.workers import telemetry_worker


class TelemetryWorkerTests(unittest.TestCase):
    def wait(self, condition, timeout=5):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.01)
        self.fail('Telemetry worker did not reach the expected condition')

    def settings(self, root):
        base = Settings()
        return replace(base, telemetry=replace(base.telemetry, interval=.2, collection_budget=.16,
                                               directory=str(root/'captures'), gps=False, csv=False))

    def settings_with(self, root, interval=.1, collection_budget=.08, **telemetry):
        base = Settings()
        return replace(base, telemetry=replace(base.telemetry, interval=interval,
                                               collection_budget=collection_budget,
                                               directory=str(root/'captures'), gps=False, csv=False,
                                               **telemetry))

    def run_worker(self, root, settings):
        runtime = root/'run'
        runtime.mkdir(exist_ok=True)
        path = root/'config.ini'
        write_atomic(path, render_config(settings))
        stop, outcome = threading.Event(), []

        def execute():
            try:
                outcome.append(telemetry_worker(path, runtime, 'telemetry-test', settings, stop))
            except BaseException as error:
                outcome.append(error)
        thread = threading.Thread(target=execute)
        return thread, stop, outcome

    def test_a_coded_stall_is_logged_once_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            calls = {'done': 0}
            unavailable = AcquisitionUnavailable(
                'Modem session is not readable while connection step OPEN is retried',
                code='modem_not_readable',
                details={'state': 'BACKOFF',
                         'diagnosis': {'code': 'qmi.no_answer', 'severity': 'error', 'attempts': 3}})

            def metrics(self):
                calls['done'] += 1
                if calls['done'] < 10:
                    raise unavailable
                self.device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', 'telemetry-serial', 'UE')
                self.generation, self.state, self.diagnosis = 1, 'ONLINE', None
                self.collection_report, self.acquisition_window = {}, None
                return {}, {'nas_value_5g_signal_strength_rsrp': -90}

            thread, stop, outcome = self.run_worker(root, settings)
            with patch('modules.runtime.workers.RemoteBackend.metrics', metrics), \
                    patch('modules.runtime.diagnosis.stall_seconds', lambda interval: .15), \
                    self.assertLogs('modules.runtime.workers', level='INFO') as captured:
                thread.start()
                try:
                    heartbeat = {}
                    deadline = time.monotonic()+5
                    while time.monotonic() < deadline and not heartbeat.get('stall'):
                        heartbeat = read_json(root/'run'/'telemetry.json') or {}
                        time.sleep(.01)
                    self.assertTrue(heartbeat.get('stall'), heartbeat)
                    stall = read_json(root/'run'/'telemetry.json')['stall']
                    self.assertEqual(stall['reason_code'], 'modem_not_readable')
                    self.assertEqual(stall['connection_code'], 'qmi.no_answer')
                    # health.json carries codes and timestamps only, never the
                    # bounded raw reason text of the heartbeat.
                    health = {}
                    deadline = time.monotonic()+5
                    while time.monotonic() < deadline and not health.get('stall'):
                        health = read_json(root/'captures'/'health.json') or {}
                        time.sleep(.01)
                    self.assertEqual(set(health['stall']), {'since_utc', 'reason_code'})
                    self.assertEqual(health['stall']['reason_code'], 'modem_not_readable')
                    self.wait(lambda: calls['done'] >= 10)
                    self.wait(lambda: (read_json(root/'run'/'telemetry.json') or {}).get('stall') is None)
                finally:
                    stop.set()
                    thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [0])
            stalled = [record for record in captured.records if 'Telemetry stalled' in record.getMessage()]
            resumed = [record for record in captured.records if 'Telemetry resumed after' in record.getMessage()]
            self.assertEqual([record.levelno for record in stalled], [logging.WARNING])
            self.assertEqual([record.levelno for record in resumed], [logging.INFO])

    def test_health_is_written_on_transition_but_not_on_every_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compact = {'code': 'qmi.no_answer', 'severity': 'warning', 'attempts': 3,
                       'failing_since_utc': '2026-11-23T00:00:00+00:00'}
            state = {'calls': 0, 'failing': False}

            def metrics(backend):
                state['calls'] += 1
                backend.device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', 'serial-1', 'UE')
                backend.generation = 1
                backend.collection_report, backend.acquisition_window = {}, None
                if state['failing']:
                    raise AcquisitionUnavailable(
                        'Modem session is not readable while connection step OPEN is retried',
                        code='modem_not_readable',
                        details={'state': 'BACKOFF', 'diagnosis': compact})
                backend.state, backend.diagnosis = 'ONLINE', None
                return {}, {'nas_value_lte_rsrp': -90}

            writes = []
            original_write = Path.write_text

            def counted(path, *args, **kwargs):
                if path.name == 'health.tmp':
                    writes.append(time.monotonic())
                return original_write(path, *args, **kwargs)

            thread, stop, outcome = self.run_worker(root, self.settings_with(root))
            try:
                with patch('modules.runtime.workers.RemoteBackend.metrics', metrics), \
                        patch.object(Path, 'write_text', counted):
                    thread.start()
                    self.wait(lambda: state['calls'] >= 5)
                    health_path = root/'captures'/'health.json'
                    self.wait(lambda: (read_json(health_path) or {}).get('connection_state') == 'ONLINE')
                    # Several samples with an unchanged fingerprint: a single write.
                    self.assertEqual(len(writes), 1)
                    self.assertIsNone(read_json(health_path)['diagnosis'])
                    state['failing'] = True
                    self.wait(lambda: (read_json(health_path) or {}).get('connection_state') == 'BACKOFF')
                    # The transition is written at once, well before HEALTH_REFRESH_SECONDS.
                    self.assertGreaterEqual(len(writes), 2)
                    self.assertEqual(read_json(health_path)['diagnosis'], compact)
                    stop.set()
                    thread.join(6)
            finally:
                stop.set()
                thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [0])

    def test_health_refresh_is_periodic_while_the_sample_loop_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = {'n': 0}
            compact = {'code': 'qmi.no_answer', 'severity': 'error', 'attempts': 3,
                       'failing_since_utc': '2026-11-23T00:00:00+00:00'}

            def metrics(backend):
                calls['n'] += 1
                raise AcquisitionUnavailable(
                    'Modem session is not readable while connection step OPEN is retried',
                    code='modem_not_readable',
                    details={'state': 'BACKOFF', 'diagnosis': compact})

            writes = []
            original_write = Path.write_text

            def counted(path, *args, **kwargs):
                if path.name == 'health.tmp':
                    writes.append(time.monotonic())
                return original_write(path, *args, **kwargs)

            thread, stop, outcome = self.run_worker(root, self.settings_with(
                root, interval=.05, collection_budget=.04))
            try:
                with patch('modules.runtime.workers.RemoteBackend.metrics', metrics), \
                        patch('modules.runtime.diagnosis.HEALTH_REFRESH_SECONDS', 0.5), \
                        patch('modules.runtime.diagnosis.stall_seconds', lambda interval: .15), \
                        patch.object(Path, 'write_text', counted):
                    thread.start()
                    self.wait(lambda: len(writes) >= 4, timeout=8)
                    self.wait(lambda: (read_json(root/'captures'/'health.json') or {}).get('stall'))
                    recorded, samples = list(writes), calls['n']
                    health = read_json(root/'captures'/'health.json')
                    stop.set()
                    thread.join(6)
            finally:
                stop.set()
                thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [0])
            self.assertGreaterEqual(len(recorded), 4)
            # No sample ever succeeded: the refresh cannot be tied to the sample loop,
            # and it runs far less often than the sample attempt rate.
            self.assertGreater(samples, 2*len(recorded))
            self.assertEqual(health['stall'], {'since_utc': health['stall']['since_utc'],
                                               'reason_code': 'modem_not_readable'})
            # The worker never sampled, so the file must not claim it did.
            self.assertIsNone(health['last_sample_at_utc'])

    def test_health_payload_is_compact_and_carries_no_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            serial = '490154203237518'
            imei = '351234567890123'
            imsi = '310150123456789'
            compact = {'code': 'qmi.no_answer', 'severity': 'warning', 'attempts': 3,
                       'failing_since_utc': '2026-11-23T00:00:00+00:00'}

            def metrics(backend):
                backend.device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', serial, 'UE')
                backend.generation = 1
                backend.state, backend.diagnosis = 'ONLINE', compact
                backend.collection_report, backend.acquisition_window = {}, None
                return {'dms_value_imei': imei, 'imsi': imsi}, {}

            thread, stop, outcome = self.run_worker(root, self.settings_with(root))
            try:
                with patch('modules.runtime.workers.RemoteBackend.metrics', metrics):
                    thread.start()
                    health_path = root/'captures'/'health.json'
                    self.wait(lambda: (read_json(health_path) or {}).get('modem'))
                    health = read_json(health_path)
                    stop.set()
                    thread.join(6)
            finally:
                stop.set()
                thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [0])
            self.assertEqual(health['schema_version'], 1)
            self.assertEqual(health['profile'], 'default')
            self.assertEqual(health['connection_state'], 'ONLINE')
            self.assertEqual(health['diagnosis'], compact)
            self.assertIsNone(health['stall'])
            self.assertIsNone(health['sim'])
            self.assertIsNone(health['registration'])
            self.assertIsInstance(health['last_sample_at_utc'], str)
            self.assertRegex(health['updated_at_utc'], r'^\d{4}-\d{2}-\d{2}T')
            hashed = 'sn-'+hashlib.sha256(serial.encode()).hexdigest()[:8]
            self.assertEqual(health['modem'], hashed)
            self.assertRegex(health['modem'], r'^sn-[0-9a-f]{8}$')
            rendered = json.dumps(health)
            for identifier in (serial, imei, imsi):
                self.assertNotIn(identifier, rendered)

    def test_a_blocked_health_write_does_not_stop_the_telemetry_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = {'n': 0}

            def metrics(backend):
                calls['n'] += 1
                backend.device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', 'serial-1', 'UE')
                backend.generation = 1
                backend.state, backend.diagnosis = 'ONLINE', None
                backend.collection_report, backend.acquisition_window = {}, None
                return {}, {'nas_value_lte_rsrp': -90}

            gate, attempts = threading.Event(), []
            original_write = Path.write_text

            def blocked(path, *args, **kwargs):
                if path.name == 'health.tmp':
                    attempts.append(path)
                    gate.wait(5)
                return original_write(path, *args, **kwargs)

            thread, stop, outcome = self.run_worker(root, self.settings_with(root))
            try:
                with patch('modules.runtime.workers.RemoteBackend.metrics', metrics), \
                        patch.object(Path, 'write_text', blocked):
                    thread.start()
                    seen = set()
                    deadline = time.monotonic()+6
                    # The health write is stuck on disk; an inline write would have
                    # stopped the main loop and no new samples would appear.
                    while time.monotonic() < deadline and len(seen) < 3:
                        heartbeat = read_json(root/'run'/'telemetry.json') or {}
                        log_id = (heartbeat.get('sample') or {}).get('log_id')
                        if log_id is not None:
                            seen.add(log_id)
                        self.assertTrue(thread.is_alive())
                        time.sleep(.01)
                    self.assertGreaterEqual(len(seen), 3)
                    self.assertTrue(attempts)
                    self.assertFalse((root/'captures'/'health.json').exists())
                    gate.set()
                    self.wait(lambda: (root/'captures'/'health.json').exists())
                    stop.set()
                    thread.join(6)
            finally:
                gate.set()
                stop.set()
                thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [0])
            self.assertGreaterEqual(calls['n'], 3)

    def test_a_generic_expiry_never_replaces_a_specific_stall_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = {'n': 0}
            compact = {'code': 'qmi.no_answer', 'severity': 'error', 'attempts': 3,
                       'failing_since_utc': '2026-11-23T00:00:00+00:00'}

            def metrics(backend):
                calls['n'] += 1
                if calls['n'] == 1:
                    raise AcquisitionUnavailable(
                        'Modem session is not readable while connection step OPEN is retried',
                        code='modem_not_readable',
                        details={'state': 'BACKOFF', 'diagnosis': compact})
                raise AcquisitionUnavailable('Acquisition deadline expired', code='acquisition_expired')

            thread, stop, outcome = self.run_worker(root, self.settings_with(root))
            try:
                with patch('modules.runtime.workers.RemoteBackend.metrics', metrics), \
                        patch('modules.runtime.diagnosis.stall_seconds', lambda interval: .15):
                    thread.start()
                    self.wait(lambda: (read_json(root/'run'/'telemetry.json') or {}).get('stall'))
                    stall = read_json(root/'run'/'telemetry.json')['stall']
                    self.wait(lambda: calls['n'] >= 4)
                    self.assertEqual(stall['reason_code'], 'modem_not_readable')
                    self.assertEqual(stall['connection_code'], 'qmi.no_answer')
                    stop.set()
                    thread.join(6)
            finally:
                stop.set()
                thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [0])


if __name__ == '__main__':
    unittest.main()
