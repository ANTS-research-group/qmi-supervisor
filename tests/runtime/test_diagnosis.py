from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import unittest

from modules.device.device import Device
from modules.runtime import diagnosis
from modules.runtime.diagnosis import FailureTracker, annotate, bounded_text, cause_of, classify, stage
from modules.runtime.logfilter import RepeatLimiter

try:
    from gi.repository import GLib, Qmi
except (ImportError, ValueError):
    GLib = Qmi = None


class Clock:
    def __init__(self, start=0):
        self.now = start

    def __call__(self):
        return self.now


class PrivacyLabelTests(unittest.TestCase):
    def test_imei_shaped_runs_are_hashed_inside_identifiers(self):
        digits = '356938035643809'
        hashed = 'sn-' + hashlib.sha256(digits.encode()).hexdigest()[:8]
        self.assertEqual(diagnosis.public_identifier('auto-quectel-' + digits),
                         'auto-quectel-' + hashed)
        self.assertEqual(diagnosis.public_identifier(digits), hashed)
        self.assertEqual(diagnosis.public_identifier('auto-quectel-9f173352'),
                         'auto-quectel-9f173352')
        self.assertEqual(diagnosis.public_identifier(None), '')

    def test_log_label_picks_the_serial_hashes_imeis_and_sanitizes(self):
        self.assertEqual(diagnosis.log_label('18d94aeb'), '18d94aeb')
        self.assertEqual(diagnosis.log_label('', '9f173352', 'auto-quectel-9f173352'), '9f173352')
        self.assertEqual(diagnosis.log_label('', '', 'default'), 'default')
        hashed = diagnosis.log_label('356938035643809')
        self.assertRegex(hashed, r'^sn-[0-9a-f]{8}$')
        self.assertNotIn('356938035643809', hashed)
        self.assertEqual(diagnosis.log_label(' modem serial!! '), 'modemserial')
        self.assertEqual(len(diagnosis.log_label('s' * 60)), 32)

    def test_published_device_hashes_only_an_imei_shaped_serial(self):
        imei = Device('/dev/cdc-wdm0', 'wwan0', '/sys/1-1', '356938035643809', 'FM160')
        self.assertRegex(diagnosis.public_device(imei)['serial'], r'^sn-[0-9a-f]{8}$')
        normal = Device('/dev/cdc-wdm0', 'wwan0', '/sys/1-1', '18d94aeb', 'FM160')
        self.assertEqual(diagnosis.public_device(normal)['serial'], '18d94aeb')
        self.assertIsNone(diagnosis.public_device(None))


class FrontendCatalogueTests(unittest.TestCase):
    def test_every_backend_code_reaches_the_frontend_table(self):
        source = (Path(__file__).resolve().parents[2]/'frontend'/'lib'/'diagnosis.ts').read_text()
        codes = set(diagnosis.LOG_TITLES) | set(diagnosis.POLICIES)
        missing = sorted(code for code in codes if "'%s'" % code not in source)
        self.assertEqual(missing, [], 'frontend/lib/diagnosis.ts lacks these codes')


BASE = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def utcnow(clock):
    return lambda: BASE + timedelta(seconds=clock.now)


def failure(code='qmi.no_answer', step='OPEN', operation='open.session', params=None, detail='x'):
    return {'code': code, 'step': step, 'operation': operation,
            'params': params or {}, 'detail': detail}


class CoreError(RuntimeError):
    def __init__(self, message, code=2, domain='qmi_core_error_quark'):
        super().__init__(message)
        self.domain, self.code = domain, code


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        diagnosis.STREAK_ACTIVE.clear()

    def test_core_timeout_and_python_timeout_are_the_same_failure(self):
        self.assertEqual(classify(CoreError('Transaction timed out', 2), 'OPEN')['code'], 'qmi.no_answer')
        self.assertEqual(classify(TimeoutError('No answer'), 'OPEN')['code'], 'qmi.no_answer')

    def test_endpoint_hangup_and_protocol_error(self):
        hangup = classify(CoreError('endpoint hangup', 0), 'OPEN')
        self.assertEqual(hangup['code'], 'qmi.endpoint_hangup')
        self.assertEqual(hangup['cause'], 'core_failed')
        protocol = classify(CoreError('bad request', 7, 'qmi_protocol_error_quark'), 'OPEN')
        self.assertEqual((protocol['code'], protocol['cause']), ('qmi.error', 'qmi_protocol_error_quark:7'))

    def test_shutdown_is_not_a_failure(self):
        self.assertIsNone(classify(InterruptedError('stopping'), 'OPEN'))

    def test_annotation_wins_over_the_mechanism(self):
        error = annotate(CoreError('Transaction timed out', 2), 'device.locked', slot=1)
        result = classify(error, 'OPEN')
        self.assertEqual((result['code'], result['params']), ('device.locked', {'slot': 1}))

    def test_cause_chain_finds_a_timeout_under_a_wrapper(self):
        try:
            try:
                raise TimeoutError('No answer')
            except TimeoutError as inner:
                raise RuntimeError('APN activation failed') from inner
        except RuntimeError as error:
            self.assertEqual(classify(error, 'CONNECT')['code'], 'qmi.no_answer')

    def test_unprintable_error_still_classifies(self):
        class Broken(Exception):
            def __str__(self):
                raise ValueError('no text')
        result = classify(Broken(), 'OPEN')
        self.assertEqual(result['code'], 'unknown')
        self.assertEqual(result['detail'], '')

    def test_detail_is_bounded_in_json_bytes(self):
        text = 'ñ' * 500
        bounded = bounded_text(text)
        self.assertLessEqual(len(bounded.encode()), 256 * 6)
        self.assertTrue(text.startswith(bounded))

    def test_stage_tags_the_exception_once_and_reraises_it(self):
        error = ValueError('boom')
        with self.assertRaises(ValueError) as raised:
            with stage('open.lock'):
                with stage('open.session'):
                    raise error
        self.assertIs(raised.exception, error)
        self.assertEqual(error.operation, 'open.session')

    def test_cause_of_os_and_unknown(self):
        self.assertEqual(cause_of(FileNotFoundError(2, 'missing')), 'os:2')
        self.assertEqual(cause_of(ValueError('x')), 'ValueError')


class FailureTrackerTests(unittest.TestCase):
    def setUp(self):
        diagnosis.STREAK_ACTIVE.clear()
        self.clock = Clock()
        self.tracker = FailureTracker(self.clock, utcnow(self.clock))

    def test_three_identical_failures_within_seconds_stay_warnings(self):
        severities = []
        for moment in (0, 5, 10):
            self.clock.now = moment
            severities.append(self.tracker.record(failure())['severity'])
        self.assertEqual(severities, ['warning', 'warning', 'warning'])
        self.assertEqual(self.tracker.snapshot()['count'], 3)

    def test_three_failures_over_two_minutes_escalate_once(self):
        events = []
        for moment in (0, 60, 130):
            self.clock.now = moment
            events.append(self.tracker.record(failure()))
        self.assertEqual([event['severity'] for event in events], ['warning', 'warning', 'error'])
        self.assertEqual([event['escalated'] for event in events], [False, False, True])
        self.assertEqual(self.tracker.snapshot()['severity'], 'error')

    def test_a_single_failure_older_than_ten_minutes_is_an_error(self):
        self.clock.now = 0
        self.tracker.record(failure())
        self.clock.now = 601
        self.assertEqual(self.tracker.record(failure())['severity'], 'error')

    def test_alternating_codes_keep_the_episode(self):
        self.clock.now = 0
        self.tracker.record(failure('qmi.no_answer'))
        self.clock.now = 10
        event = self.tracker.record(failure('unknown'))
        self.assertEqual((event['attempts'], self.tracker.count), (2, 1))
        self.assertEqual(self.tracker.snapshot()['failing_since_utc'], (BASE).isoformat())
        self.assertEqual(self.tracker.previous['resolution'], 'changed')

    def test_immediate_code_is_an_error_at_once(self):
        event = self.tracker.record(failure('host.modemmanager_active'))
        self.assertEqual((event['severity'], event['escalated']), ('error', True))

    def test_step_passed_closes_the_streak_but_keeps_the_episode(self):
        self.tracker.record(failure())
        self.assertTrue(self.tracker.step_passed('WAIT_REGISTRATION'))
        self.assertFalse(self.tracker.active)
        self.assertEqual(self.tracker.attempts, 1)
        self.assertEqual(self.tracker.snapshot(), {'active': False, 'previous': self.tracker.previous})
        self.assertFalse(diagnosis.STREAK_ACTIVE.is_set())

    def test_step_passed_ignores_earlier_or_equal_states(self):
        self.tracker.record(failure())
        self.assertFalse(self.tracker.step_passed('OPEN'))
        self.assertTrue(self.tracker.active)

    def test_success_returns_a_summary_and_keeps_the_previous_streak(self):
        self.clock.now = 0
        self.tracker.record(failure())
        self.clock.now = 45
        summary = self.tracker.success()
        self.assertEqual((summary['attempts'], summary['duration_s'], summary['code']), (1, 45, 'qmi.no_answer'))
        self.assertFalse(self.tracker.snapshot()['active'])
        self.assertEqual(self.tracker.previous['resolution'], 'recovered')
        self.assertFalse(diagnosis.STREAK_ACTIVE.is_set())

    def test_kind_sequence_follows_the_journal_policy(self):
        kinds = {}
        self.clock.now = 0
        for count in range(1, 1201):
            self.clock.now = count
            kinds[count] = self.tracker.record(failure())['kind']
            if count != self.tracker.count:
                break
        self.assertEqual([kinds[n] for n in (1, 2, 3, 4, 9)], ['logged'] * 3 + ['folded'] * 2)
        self.assertEqual([kinds[n] for n in (10, 99, 100, 999, 1000)], ['summary', 'folded', 'summary',
                                                                         'folded', 'summary'])
        self.assertEqual(sum(1 for kind in kinds.values() if kind == 'summary'), 3)

    def test_record_roundtrip_restores_the_streak(self):
        self.clock.now = 0
        self.tracker.record(failure('qmi.no_answer'))
        self.clock.now = 90
        self.tracker.record(failure('qmi.no_answer'))
        record = self.tracker.to_record()
        restored_clock = Clock(self.clock.now)
        restored = FailureTracker(restored_clock, utcnow(restored_clock)).from_record(record)
        snapshot = restored.snapshot()
        self.assertEqual(snapshot['count'], 2)
        self.assertEqual(snapshot['attempts'], 2)
        self.assertEqual(snapshot['failing_seconds'], 90)
        self.assertEqual(snapshot['code'], 'qmi.no_answer')

    def test_record_roundtrip_keeps_the_escalation_flag(self):
        self.tracker.record(failure('host.modemmanager_active'))
        record = self.tracker.to_record()
        self.assertTrue(record['error_logged'])
        restored = FailureTracker(self.clock, utcnow(self.clock)).from_record(record)
        event = restored.record(failure('host.modemmanager_active'))
        self.assertFalse(event['escalated'])
        self.assertEqual(event['attempts'], 2)

    def test_inactive_tracker_has_no_snapshot(self):
        self.assertIsNone(self.tracker.snapshot())

    def test_bounded_diagnosis_stays_inside_the_contract_budget(self):
        self.clock.now = 0
        for count in range(1, 1001):
            self.clock.now = count
            self.tracker.record(failure(detail='x' * 2000,
                                        params={'network': 'n' * 2000, 'items': ['y' * 900] * 40}))
        published = diagnosis.bounded_diagnosis(self.tracker.snapshot())
        size = len(json.dumps(published, ensure_ascii=True))
        self.assertLessEqual(size, diagnosis.DIAGNOSIS_MAX_BYTES)
        self.assertLessEqual(size, diagnosis.SNAPSHOT_EXTRA_MAX_BYTES)
        self.assertEqual((published['code'], published['count']), ('qmi.no_answer', 1000))
        self.assertIn('attempts', published)


def record(msg, level=logging.WARNING, name='modules.device.backend', **extra):
    item = logging.LogRecord(name, level, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(item, key, value)
    return item


class RepeatLimiterTests(unittest.TestCase):
    def setUp(self):
        diagnosis.STREAK_ACTIVE.clear()

    def test_folds_the_fourth_identical_template(self):
        limiter = RepeatLimiter()
        diagnosis.STREAK_ACTIVE.set()
        self.assertEqual([limiter.filter(record('Repeated line')) for _ in range(4)],
                         [True, True, True, False])
        self.assertEqual(limiter.drain(), {'Repeated line': 1})

    def test_never_folds_diagnosis_lines_or_errors(self):
        limiter = RepeatLimiter()
        diagnosis.STREAK_ACTIVE.set()
        for _ in range(4):
            self.assertTrue(limiter.filter(record('Mine', qmi_diagnosis=True)))
            self.assertTrue(limiter.filter(record('Fatal', level=logging.ERROR)))

    def test_passes_everything_without_a_streak(self):
        limiter = RepeatLimiter()
        for _ in range(5):
            self.assertTrue(limiter.filter(record('Quiet')))

    def test_drain_reports_every_suppressed_template(self):
        limiter = RepeatLimiter()
        diagnosis.STREAK_ACTIVE.set()
        for _ in range(6):
            limiter.filter(record('A'))
            limiter.filter(record('B', name='modules.runtime.supervisor'))
        self.assertEqual(limiter.drain(), {'A': 3, 'B': 3})


class StartupTransientTests(unittest.TestCase):
    """M12: only boot-time allocation failures are informational."""

    def failure(self, **values):
        base = {'code': 'qmi.no_answer', 'cause': 'timeout', 'operation': 'open.allocate',
                'step': 'OPEN', 'params': {}, 'detail': 'Transaction timed out'}
        base.update(values)
        return base

    def test_only_the_listed_operations_and_causes_inside_the_grace(self):
        self.assertTrue(diagnosis.is_transient(self.failure(), [1000], 1000, 0))
        self.assertTrue(diagnosis.is_transient(
            self.failure(cause='qmi_protocol_error_quark:52'), [950], 1000, 0))
        self.assertTrue(diagnosis.is_transient(
            self.failure(cause='qmi_core_error_quark:1'), [1000], 1090, 0))
        for failure in (self.failure(step='WAIT_REGISTRATION'),
                        self.failure(operation='open.recover.saved_stop'),
                        self.failure(operation='prepare.sim'),
                        self.failure(cause='qmi_error')):
            with self.subTest(failure=failure):
                self.assertFalse(diagnosis.is_transient(failure, [1000], 1000, 0))
        self.assertFalse(diagnosis.is_transient(self.failure(), [1000], 1091, 0))
        self.assertFalse(diagnosis.is_transient(self.failure(), [1000], 999, 0))
        self.assertFalse(diagnosis.is_transient(self.failure(), [1000], 1000, 2))

    def test_unknown_anchor_types_and_mocks_are_ignored(self):
        self.assertFalse(diagnosis.is_transient(self.failure(), [object(), True, None, '1000'],
                                                1000, 0))
        self.assertFalse(diagnosis.is_transient(self.failure(), [], 1000, 0))
        self.assertFalse(diagnosis.is_transient(None, [1000], 1000, 0))


@unittest.skipIf(GLib is None, 'Requires OS libqmi GI bindings')
class GlibClassificationTests(unittest.TestCase):
    def test_glib_core_timeout_classifies_as_no_answer(self):
        error = GLib.Error.new_literal(Qmi.core_error_quark(), 'Transaction timed out', Qmi.CoreError.TIMEOUT)
        self.assertEqual(classify(error, 'OPEN')['code'], 'qmi.no_answer')

    def test_startup_transient_text_keeps_its_operation(self):
        error = GLib.Error.new_literal(Qmi.core_error_quark(), 'CID allocation failed in the CTL client: '
                                       'Transaction timed out', Qmi.CoreError.TIMEOUT)
        with self.assertRaises(GLib.Error):
            with stage('open.allocate'):
                raise error
        result = classify(error, 'OPEN')
        self.assertEqual((result['code'], result['operation']), ('qmi.no_answer', 'open.allocate'))


if __name__ == '__main__':
    unittest.main()
