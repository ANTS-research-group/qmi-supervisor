"""Exercise the event detector rules and the rotating append-only log."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import modules.runtime.events as events_module
from modules.runtime.diagnosis import log_label
from modules.runtime.events import EventDetector, EventLog

MOMENT = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
SINCE = '2026-11-20T10:00:00+00:00'


def snapshot(state, diagnosis=None):
    return {'state': state, 'diagnosis': diagnosis}


def diagnosis(code='registration.timeout', severity='warning', step='WAIT_REGISTRATION',
              operation='registration', attempts=1, since_utc=SINCE,
              failing_since_utc=SINCE, active=True):
    return {'code': code, 'severity': severity, 'step': step, 'operation': operation,
            'attempts': attempts, 'since_utc': since_utc,
            'failing_since_utc': failing_since_utc, 'active': active}


def children(**restarts):
    return {role: SimpleNamespace(restarts=value) for role, value in restarts.items()}


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


class DetectorTests(unittest.TestCase):
    def test_first_observation_is_a_silent_baseline(self):
        detector = EventDetector(utcnow=lambda: MOMENT)
        kids = children(modem=0, telemetry=0, web=0)
        self.assertEqual(detector.update(snapshot('WAIT_REGISTRATION'), {}, kids), [])
        self.assertEqual(detector.update(snapshot('ONLINE'), {}, kids),
                         [('connection.online', {'state': 'ONLINE'})])

    def test_diagnosis_opens_escalates_and_resolves(self):
        moment = MOMENT
        detector = EventDetector(utcnow=lambda: moment)
        kids = children(modem=0, telemetry=0)

        self.assertEqual(detector.update(snapshot('WAIT_REGISTRATION'), {}, kids), [])
        events = detector.update(snapshot('WAIT_REGISTRATION', diagnosis(attempts=1)), {}, kids)
        self.assertEqual(events, [('diagnosis.opened', {
            'code': 'registration.timeout', 'severity': 'warning',
            'step': 'WAIT_REGISTRATION', 'operation': 'registration', 'since_utc': SINCE})])

        # More attempts at the same severity are not a new event.
        self.assertEqual(detector.update(
            snapshot('WAIT_REGISTRATION', diagnosis(attempts=2)), {}, kids), [])

        moment = MOMENT + timedelta(seconds=120)
        events = detector.update(
            snapshot('WAIT_REGISTRATION', diagnosis(severity='error', attempts=3)), {}, kids)
        self.assertEqual(events, [('diagnosis.escalated', {
            'code': 'registration.timeout', 'severity': 'error', 'attempts': 3,
            'failing_since_utc': SINCE})])

        moment = MOMENT + timedelta(seconds=150)
        events = detector.update(snapshot('ONLINE'), {}, kids)
        self.assertEqual(events, [
            ('diagnosis.resolved', {
                'code': 'registration.timeout', 'resolution': 'recovered',
                'attempts': 3, 'duration_s': 150}),
            ('connection.online', {'state': 'ONLINE'})])

    def test_a_changed_code_is_reported_as_changed(self):
        detector = EventDetector(utcnow=lambda: MOMENT)
        kids = children(modem=0)
        detector.update(snapshot('WAIT_REGISTRATION'), {}, kids)
        detector.update(snapshot('WAIT_REGISTRATION', diagnosis()), {}, kids)
        events = detector.update(snapshot('WAIT_REGISTRATION', diagnosis(
            code='sim.absent', severity='error', operation='sim')), {}, kids)
        self.assertEqual(events, [('diagnosis.changed', {
            'code': 'sim.absent', 'previous_code': 'registration.timeout',
            'severity': 'error'})])

    def test_resolution_after_a_later_step_is_step_passed(self):
        moment = MOMENT
        detector = EventDetector(utcnow=lambda: moment)
        kids = children(modem=0)
        detector.update(snapshot('WAIT_REGISTRATION'), {}, kids)
        detector.update(snapshot('WAIT_REGISTRATION', diagnosis()), {}, kids)
        moment = MOMENT + timedelta(seconds=30)
        events = detector.update(snapshot('CONNECT'), {}, kids)
        self.assertEqual(events, [('diagnosis.resolved', {
            'code': 'registration.timeout', 'resolution': 'step_passed',
            'attempts': 1, 'duration_s': 30})])

    def test_connection_online_and_offline_edges(self):
        detector = EventDetector(utcnow=lambda: MOMENT)
        kids = children(modem=0)
        detector.update(snapshot('WAIT_DEVICE'), {}, kids)
        self.assertEqual(detector.update(snapshot('ONLINE'), {}, kids),
                         [('connection.online', {'state': 'ONLINE'})])
        self.assertEqual(detector.update(snapshot('MONITOR'), {}, kids), [])
        self.assertEqual(detector.update(snapshot('BACKOFF'), {}, kids),
                         [('connection.offline', {'state': 'BACKOFF', 'from': 'MONITOR'})])
        self.assertEqual(detector.update(snapshot('MONITOR'), {}, kids),
                         [('connection.online', {'state': 'MONITOR'})])

    def test_telemetry_stall_and_resume(self):
        moment = MOMENT
        detector = EventDetector(utcnow=lambda: moment)
        kids = children(modem=0, telemetry=0)
        detector.update(snapshot('WAIT_DEVICE'), {}, kids)
        stall = {'since_utc': SINCE, 'last_sample_at_utc': None,
                 'reason_code': 'modem_not_readable', 'reason': 'private detail',
                 'connection_code': None, 'skipped': 5}
        self.assertEqual(detector.update(snapshot('WAIT_DEVICE'), {'stall': stall}, kids),
                         [('telemetry.stalled', {
                             'since_utc': SINCE, 'reason_code': 'modem_not_readable'})])
        moment = MOMENT + timedelta(seconds=45)
        self.assertEqual(detector.update(snapshot('WAIT_DEVICE'), {}, kids),
                         [('telemetry.resumed', {'stalled_seconds': 45, 'skipped': 5})])

    def test_worker_restart_is_reported_after_the_baseline(self):
        detector = EventDetector(utcnow=lambda: MOMENT)
        detector.update(snapshot('WAIT_DEVICE'), {}, children(modem=0, telemetry=0, web=0))
        self.assertEqual(
            detector.update(snapshot('WAIT_DEVICE'), {}, children(modem=0, telemetry=1, web=0)),
            [('worker.restarted', {'role': 'telemetry', 'restarts': 1})])
        self.assertEqual(
            detector.update(snapshot('WAIT_DEVICE'), {}, children(modem=0, telemetry=1, web=0)),
            [])

    def test_children_may_be_plain_status_dicts(self):
        detector = EventDetector(utcnow=lambda: MOMENT)
        detector.update(snapshot('WAIT_DEVICE'), {}, {'modem': {'restarts': 2}})
        self.assertEqual(detector.update(snapshot('WAIT_DEVICE'), {}, {'modem': {'restarts': 3}}),
                         [('worker.restarted', {'role': 'modem', 'restarts': 3})])


class EventLogTests(unittest.TestCase):
    def test_seq_is_monotonic_and_instance_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory), profile='auto-quectel-9f173352', modem='9f173352')
            for _ in range(5):
                log.emit('connection.online', {'state': 'ONLINE'})
            log.close(timeout=3)
            path = Path(directory)/'events.jsonl'
            lines = read_lines(path)
            self.assertEqual([line['seq'] for line in lines], [1, 2, 3, 4, 5])
            self.assertEqual({line['instance'] for line in lines}, {log.instance})
            self.assertEqual(log.seq, 5)
            self.assertEqual({line['schema_version'] for line in lines}, {1})
            self.assertEqual({line['profile'] for line in lines}, {'auto-quectel-9f173352'})
            self.assertEqual({line['modem'] for line in lines}, {'9f173352'})
            self.assertTrue(all(line['at_utc'] for line in lines))
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_rotation_keeps_exactly_the_current_and_one_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory), max_bytes=800)
            for attempt in range(40):
                log.emit('diagnosis.opened', {'code': 'registration.timeout',
                                              'severity': 'warning', 'attempts': attempt})
            log.close(timeout=3)
            names = sorted(path.name for path in Path(directory).iterdir())
            self.assertEqual(names, ['events.jsonl', 'events.jsonl.1'])
            for name in names:
                path = Path(directory)/name
                self.assertTrue(read_lines(path), name)
                self.assertLessEqual(path.stat().st_size, 800, name)

    def test_a_full_queue_drops_the_oldest_and_counts_it(self):
        gate, started = threading.Event(), threading.Event()
        real_write = events_module._write_all

        def blocked(descriptor, data):
            started.set()
            gate.wait(5)
            return real_write(descriptor, data)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(events_module, '_write_all', new=blocked):
            log = EventLog(Path(directory))
            log.emit('first', {})
            self.assertTrue(started.wait(2))
            for index in range(300):
                log.emit('queued', {'i': index})
            self.assertEqual(log.dropped, 44)
            gate.set()
            log.close(timeout=5)
            lines = read_lines(Path(directory)/'events.jsonl')

        self.assertEqual(log.dropped, 44)
        self.assertEqual(len(lines), 257)
        indices = [line['data'].get('i') for line in lines if line['type'] == 'queued']
        self.assertNotIn(0, indices)
        self.assertIn(44, indices)
        self.assertIn(299, indices)

    def test_a_writer_stuck_on_io_never_delays_the_caller(self):
        gate = threading.Event()
        real_write = events_module._write_all

        def slow(descriptor, data):
            gate.wait(5)
            return real_write(descriptor, data)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(events_module, '_write_all', new=slow):
            log = EventLog(Path(directory))
            started = time.monotonic()
            for index in range(200):
                log.emit('queued', {'i': index})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            gate.set()
            log.close(timeout=5)
            self.assertEqual(len(read_lines(Path(directory)/'events.jsonl')), 200)

    def test_sensitive_identifiers_never_reach_a_line(self):
        imsi, iccid, imei = '310150123456789', '89014103211118510720', '356938035643809'
        modem = log_label(serial=imei, profile='auto-quectel')
        moment = MOMENT
        detector = EventDetector(utcnow=lambda: moment)
        kids = children(modem=0, telemetry=0)
        base = {'state': 'WAIT_REGISTRATION',
                'device': {'serial': imei, 'imsi': imsi, 'iccid': iccid}}

        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory), profile='auto-quectel', modem=modem)
            detector.update(base, {}, kids)
            sensitive = {'code': 'registration.timeout', 'severity': 'warning',
                         'step': 'WAIT_REGISTRATION', 'operation': 'registration',
                         'attempts': 2, 'since_utc': SINCE, 'failing_since_utc': SINCE,
                         'detail': imsi + '/' + iccid + '/' + imei,
                         'params': {'imsi': imsi, 'iccid': iccid, 'imei': imei}}
            telemetry = {'sample': {'imsi': imsi, 'iccid': iccid, 'imei': imei},
                         'error': iccid,
                         'stall': {'since_utc': SINCE, 'reason_code': 'modem_not_readable',
                                   'reason': imsi, 'skipped': 3}}
            for event_type, data in detector.update(dict(base, diagnosis=sensitive),
                                                    telemetry, kids):
                log.emit(event_type, data)
            moment = MOMENT + timedelta(seconds=60)
            for event_type, data in detector.update(dict(base, state='ONLINE'), {}, kids):
                log.emit(event_type, data)
            log.close(timeout=3)
            text = (Path(directory)/'events.jsonl').read_text()

        self.assertTrue(text.strip())
        for secret in (imsi, iccid, imei):
            self.assertNotIn(secret, text)
        self.assertIn(modem, text)


if __name__ == '__main__':
    unittest.main()
