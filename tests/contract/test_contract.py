"""The objects the code really publishes must match the versioned contract."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading
import unittest

from modules.deployment.provisioning import render_config, write_atomic
from modules.runtime import diagnosis
from modules.runtime.diagnosis import FailureTracker
from modules.runtime.events import EventDetector, EventLog
from modules.runtime.fleet_status import modem_snapshot
from modules.runtime.processes import ProcessManager
from modules.runtime.settings import Settings
from modules.telemetry.collector import health_document
from modules.update import history
from tests.contract.schema_check import assert_valid, validate
from tools.contract.generate_codes import (
    CODES_NAME, DIAGNOSIS_NAME, generate, render_codes, render_diagnosis_schema,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / 'doc' / 'schemas'
MOMENT = datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc)
SINCE = MOMENT.isoformat()


def load_schema(name):
    return json.loads((SCHEMAS / name).read_text())


def snapshot(state, diagnosis_value=None):
    return {'state': state, 'diagnosis': diagnosis_value}


def diagnosis_value(code='qmi.no_answer', severity='warning', step='CONNECT',
                    operation='connect.start', attempts=1, since_utc=SINCE,
                    failing_since_utc=SINCE, active=True):
    return {'code': code, 'severity': severity, 'step': step, 'operation': operation,
            'attempts': attempts, 'since_utc': since_utc,
            'failing_since_utc': failing_since_utc, 'active': active}


def children(**restarts):
    return {role: {'restarts': value} for role, value in restarts.items()}


class DiagnosisSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = load_schema('diagnosis.schema.json')

    def test_an_active_failure_snapshot_is_valid(self):
        tracker = FailureTracker(clock=lambda: 10, utcnow=lambda: MOMENT)
        tracker.record({'code': 'qmi.no_answer', 'step': 'CONNECT',
                        'operation': 'connect.start', 'params': {'operation': 'connect.start'},
                        'detail': 'synthetic failure'})
        value = tracker.snapshot()
        self.assertIs(value.get('active'), True)
        assert_valid(self, value, self.schema, 'FailureTracker.snapshot()')

    def test_a_configuration_pending_diagnosis_is_valid(self):
        value = {'code': 'config.pending', 'severity': 'warning', 'active': True,
                 'step': 'CONFIG', 'operation': None, 'params': {},
                 'detail': 'synthetic error', 'observed_at_utc': SINCE}
        assert_valid(self, value, self.schema, 'config.pending diagnosis')

    def test_an_inactive_snapshot_is_valid(self):
        value = {'active': False,
                 'previous': {'code': 'qmi.no_answer', 'resolution': 'recovered',
                              'ended_utc': SINCE}}
        assert_valid(self, value, self.schema, 'inactive diagnosis')


class HealthSchemaTests(unittest.TestCase):
    def test_the_health_document_helper_output_is_valid(self):
        schema = load_schema('health.schema.json')
        minimal = health_document(modem='modem[18d94aeb]', profile='default',
                                  connection_state='WAIT_DEVICE',
                                  supervisor_version='1.2.0-1',
                                  updated_at_utc=SINCE)
        assert_valid(self, minimal, schema, 'minimal health.json')
        rich = health_document(
            modem='sn-0123abcd', profile='default', connection_state='BACKOFF',
            supervisor_version='1.2.0-1',
            last_sample_at_utc=SINCE,
            diagnosis={'code': 'qmi.no_answer', 'severity': 'error', 'attempts': 3,
                       'failing_since_utc': SINCE, 'active': True},
            stall={'since_utc': SINCE, 'reason_code': 'modem_not_readable',
                   'reason': 'private detail', 'skipped': 5},
            updated_at_utc=SINCE)
        self.assertEqual(set(rich['stall']), {'since_utc', 'reason_code'})
        self.assertEqual(rich['supervisor_version'], '1.2.0-1')
        assert_valid(self, rich, schema, 'health.json with a stall')

    def test_the_health_document_publishes_the_specific_sim_code(self):
        schema = load_schema('health.schema.json')
        document = health_document(modem='m', profile='default', connection_state='BACKOFF',
                                   supervisor_version='1.2.0-1', sim={'code': 'sim.no_atr'},
                                   updated_at_utc=SINCE)
        assert_valid(self, document, schema, 'health.json with a SIM code')
        self.assertEqual(document['sim'], {'code': 'sim.no_atr'})

    def test_the_health_document_publishes_the_registration_block(self):
        schema = load_schema('health.schema.json')
        document = health_document(
            modem='m', profile='default', connection_state='BACKOFF',
            supervisor_version='1.2.0-1',
            registration={'code': 'registration.home_not_found', 'home': '99910',
                          'camped': ['21403', '21407']},
            updated_at_utc=SINCE)
        assert_valid(self, document, schema, 'health.json with a registration block')
        self.assertEqual(document['registration']['code'], 'registration.home_not_found')

    def test_supervisor_version_is_required_in_the_health_contract(self):
        schema = load_schema('health.schema.json')
        document = health_document(modem='m', profile='default', connection_state='IDLE',
                                   supervisor_version='1.2.0-1', updated_at_utc=SINCE)
        document.pop('supervisor_version')
        self.assertIn('supervisor_version', schema['required'])
        self.assertNotEqual(validate(document, schema), [],
                            'a health.json without supervisor_version must be rejected')


class EventSchemaTests(unittest.TestCase):
    def test_event_lines_from_the_detector_and_log_are_valid(self):
        schema = load_schema('event.schema.json')
        moment = MOMENT
        detector = EventDetector(utcnow=lambda: moment)
        kids = children(modem=0, telemetry=0)
        seen = set()
        with tempfile.TemporaryDirectory() as directory:
            log = EventLog(Path(directory), profile='default', modem='18d94aeb')
            try:
                def run(state, current=None, telemetry=None, restarts=None):
                    nonlocal kids
                    if restarts is not None:
                        kids = restarts
                    events = detector.update(snapshot(state, current), telemetry or {}, kids)
                    for event_type, data in events:
                        seen.add(event_type)
                        log.emit(event_type, data)

                detector.update(snapshot('WAIT_DEVICE'), {}, kids)  # baseline
                run('ONLINE')
                run('ONLINE', diagnosis_value(attempts=1))
                moment = MOMENT + timedelta(seconds=120)
                run('ONLINE', diagnosis_value(severity='error', attempts=3))
                run('ONLINE', diagnosis_value(code='sim.absent', severity='error'))
                run('ONLINE')
                run('BACKOFF')
                run('BACKOFF', restarts=children(modem=0, telemetry=1))
                stall = {'since_utc': SINCE, 'reason_code': 'modem_not_readable', 'skipped': 3}
                run('BACKOFF', telemetry={'stall': stall})
                moment = MOMENT + timedelta(seconds=165)
                run('BACKOFF')
            finally:
                log.close(timeout=3)
            lines = [json.loads(line) for line in
                     (Path(directory) / 'events.jsonl').read_text().splitlines() if line]
        self.assertTrue(lines)
        self.assertEqual(seen, {
            'connection.online', 'connection.offline', 'diagnosis.opened',
            'diagnosis.changed', 'diagnosis.escalated', 'diagnosis.resolved',
            'telemetry.stalled', 'telemetry.resumed', 'worker.restarted'})
        for line in lines:
            assert_valid(self, line, schema, f'event {line.get("type")}')


class ModemStatusSchemaTests(unittest.TestCase):
    class Process:
        sequence = 0

        def __init__(self, command, **kwargs):
            type(self).sequence += 1
            self.pid, self.returncode = type(self).sequence, None

        def poll(self):
            return self.returncode

    def manager(self, root):
        config = root/'settings.ini'
        write_atomic(config, render_config(Settings()))
        runtime = root/'run'
        runtime.mkdir()
        return ProcessManager(config, Settings(), threading.Event(), runtime=runtime,
                              popen=self.Process)

    def test_the_status_the_manager_really_writes_is_valid(self):
        schema = load_schema('modem-status.schema.json')
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(Path(directory))
            modem = manager.children['modem']
            modem.process = self.Process('synthetic')
            modem.last_heartbeat = manager.clock()
            # The subset workers.snapshot() publishes for a running session.
            modem.status = {'snapshot': {
                'state': 'ONLINE', 'id': 'default',
                'device': {'path': '/dev/cdc-wdm0', 'interface': 'wwan0', 'serial': '18d94aeb'},
                'log_label': '18d94aeb',
                'network': {'address': '192.0.2.2/30', 'gateway': '192.0.2.1', 'interface': 'wwan0'},
                'diagnosis': None, 'error': None,
                'features': {}, 'sample': None}}
            try:
                manager.publish()
                document = manager.publisher.state
            finally:
                if manager.publisher:
                    manager.publisher.close()
            assert_valid(self, document, schema, 'ProcessManager.publish() document')
            self.assertEqual(document['id'], 'default')
            self.assertEqual(document['log_label'], '18d94aeb')
            entry = {'id': 'default', 'detected': True, 'device': document['device'],
                     'features': document.get('features', {})}
            record = modem_snapshot(document, entry, manager_heartbeat=manager.clock())
            assert_valid(self, record, schema, 'modem_snapshot() record')
            self.assertEqual(record['id'], 'default')
            self.assertIsInstance(record['supervisor_version'], str)
            self.assertEqual(record['supervisor_version'], document['supervisor_version'])
            without = dict(record)
            without.pop('supervisor_version')
            self.assertIn('supervisor_version', schema['required'])
            self.assertNotEqual(validate(without, schema), [],
                                'a modem record without supervisor_version must be rejected')


class GeneratedCatalogueTests(unittest.TestCase):
    def test_committed_files_match_a_fresh_generator_run(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copy(SCHEMAS / DIAGNOSIS_NAME, Path(directory) / DIAGNOSIS_NAME)
            generate(directory)
            self.assertEqual((Path(directory) / CODES_NAME).read_text(),
                             (SCHEMAS / CODES_NAME).read_text())
            self.assertEqual((Path(directory) / DIAGNOSIS_NAME).read_text(),
                             (SCHEMAS / DIAGNOSIS_NAME).read_text())

    def test_codes_json_lists_every_code_once_with_category_policy_and_since(self):
        committed = json.loads((SCHEMAS / CODES_NAME).read_text())
        self.assertEqual(committed, json.loads(render_codes()))
        self.assertEqual(committed['version'], 1)
        self.assertEqual(set(committed['codes']), set(diagnosis.CODES))
        for code, entry in committed['codes'].items():
            with self.subTest(code=code):
                self.assertEqual(set(entry), {'category', 'policy', 'since'})
                self.assertEqual(entry['since'], 'v1.3.0')
                self.assertIn(entry['category'], {
                    'qmi', 'device', 'host', 'config', 'worker', 'session', 'recovery',
                    'sim', 'registration', 'telemetry', 'startup', 'connect'})
                self.assertIn(entry['policy'], {'immediate', 'after_ladder', 'default', 'info'})

    def test_diagnosis_schema_enum_and_policies_follow_the_single_source_of_truth(self):
        schema = load_schema(DIAGNOSIS_NAME)
        self.assertEqual(schema['properties']['code']['enum'], sorted(diagnosis.CODES))
        self.assertEqual(schema, json.loads(render_diagnosis_schema(schema)))
        expected = {code: entry['policy'] for code, entry in diagnosis.CODES.items()
                    if entry['policy'] != 'default'}
        self.assertEqual(diagnosis.POLICIES, expected)

    def test_every_backend_code_and_frontend_entry_agree(self):
        source = (ROOT / 'frontend' / 'lib' / 'diagnosis.ts').read_text()
        block = source.split('export const CODE_TEXT', 1)[1].split('\n};', 1)[0]
        frontend_codes = set(re.findall(r"^\s*'([^']+)':\s*\{", block, re.M))
        self.assertEqual(frontend_codes, set(diagnosis.CODES))
        for code in diagnosis.CODES:
            with self.subTest(code=code):
                self.assertIn(f"'{code}'", source)


class UpdateHistorySchemaTests(unittest.TestCase):
    def record(self, **overrides):
        fields = {
            'id': '9b2c5d0e4f5a4c1b8a7e6d5c4b3a2910', 'request_id': None,
            'started_at_utc': SINCE, 'at_utc': SINCE, 'duration_s': 398,
            'from': '1.3.0-1', 'to': '1.4.0-1', 'reason': 'manual', 'result': 'success',
            'step_failed': None, 'code': None, 'detail': 'synthetic transaction',
            'rollback_available': True, 'installed_after': '1.4.0-1',
            'config_restored': False,
            'signed_by': '54F3194C1190482A124798C299A63F58A88CB47E',
        }
        fields.update(overrides)
        return history.make_record(**fields)

    def test_lines_from_every_result_are_valid(self):
        schema = load_schema('update-history.schema.json')
        for label, line in (
                ('success', self.record()),
                ('rolled back', self.record(
                    reason='auto', result='rolled_back', step_failed='health',
                    code='health_failed', detail='profile default was ONLINE before the update',
                    installed_after='1.3.0-1')),
                ('interrupted', self.record(
                    result='interrupted', step_failed='install', code='interrupted',
                    detail='stopped during install; package state iF 1.4.0-1'[:256],
                    installed_after=None)),
                ('repair', self.record(
                    reason='repair', to='1.3.0-1', result='success', installed_after='1.3.0-1')),
        ):
            with self.subTest(result=label):
                assert_valid(self, line, schema, f'update history line ({label})')

    def test_the_schema_enums_equal_the_history_tuples(self):
        schema = load_schema('update-history.schema.json')
        properties = schema['properties']
        self.assertEqual(set(properties['code']['enum']), set(history.CODES) | {None})
        self.assertEqual(set(properties['step_failed']['enum']), set(history.STEPS) | {None})
        self.assertEqual(set(properties['reason']['enum']), set(history.REASONS))
        self.assertEqual(set(properties['result']['enum']), set(history.RESULTS))


if __name__ == '__main__':
    unittest.main()
