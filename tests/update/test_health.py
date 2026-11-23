"""Health signals, the service/stability wait and the modem wait."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from modules.update import health
from modules.update.errors import Interrupted
from tests.update.fakes import FakeClock, FakeUpdateHost

NOW = datetime(2026, 11, 21, 3, 7, 41, tzinfo=timezone.utc)


class HealthSignalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.clock = FakeClock(NOW)

    def write(self, name, document):
        path = self.runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))

    def test_a_heartbeat_is_fresh_only_within_fifteen_seconds_and_after_a_marker(self):
        self.write('web.json', {'phase': 'RUNNING', 'worker_id': 'w',
                                'heartbeat_monotonic': 1000.0, 'secret': 'not read'})
        self.assertEqual(health.heartbeat(self.runtime / 'web.json', 1005.0),
                         {'phase': 'RUNNING', 'worker_id': 'w', 'monotonic': 1000.0})
        self.assertIsNone(health.heartbeat(self.runtime / 'web.json', 1016.0))
        self.assertIsNone(health.heartbeat(self.runtime / 'web.json', 1000.0, after=1000.0))
        self.assertIsNotNone(health.heartbeat(self.runtime / 'web.json', 1000.0, after=999.0))
        for value in (True, '1000', None):
            self.write('web.json', {'phase': 'RUNNING', 'heartbeat_monotonic': value})
            self.assertIsNone(health.heartbeat(self.runtime / 'web.json', 1000.0))

    def test_online_profiles_ignore_stale_non_online_and_invalid_entries(self):
        for profile, phase, beat in (('default', 'ONLINE', 1000.0), ('private', 'STOPPED', 1000.0),
                                     ('stale', 'ONLINE', 900.0), ('Bad', 'ONLINE', 1000.0),
                                     ('bad.name', 'ONLINE', 1000.0)):
            self.write(f'modems/{profile}/modem.json',
                       {'phase': phase, 'worker_id': profile, 'heartbeat_monotonic': beat})
        self.assertEqual(health.online_profiles(self.runtime, 1000.0), {'default': 'default'})

    def test_web_and_status_publication(self):
        self.write('web.json', {'phase': 'RUNNING', 'heartbeat_monotonic': 1000.0})
        self.write('status.json', {'published_monotonic': 1000.0})
        self.assertTrue(health.web_running(self.runtime, 1000.0))
        self.assertFalse(health.web_running(self.runtime, 1000.0, after=1000.0))
        self.assertFalse(health.status_published(self.runtime, 1000.0))
        self.assertTrue(health.status_published(self.runtime, 999.0))

    def test_the_running_identity_requires_schema_version_one(self):
        self.assertIsNone(health.running_identity(self.runtime))
        self.write('version.json', {'schema_version': 2, 'supervisor_version': '1.0.0-1'})
        self.assertIsNone(health.running_identity(self.runtime))
        self.write('version.json', {'schema_version': 1, 'supervisor_version': '1.0.0-1'})
        self.assertEqual(health.running_identity(self.runtime)['supervisor_version'], '1.0.0-1')


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = FakeClock(NOW)
        self.host = FakeUpdateHost(self.root, self.clock, installed='1.0.0-1')
        self.host.last_install_version = '2.0.0-1'
        self.host.refresh()

    def evaluate(self, after=999.0, web_required=True, target='2.0.0-1', now=1000.0):
        return health.evaluate(self.root, self.host, now, target=target, after=after,
                               web_required=web_required)

    def test_healthy_now(self):
        self.assertEqual(self.evaluate(), (None, None))

    def test_stage_one_the_service_is_not_active(self):
        self.host.service = 'failed'
        self.assertEqual(self.evaluate(), ('restart', 'service inactive/dead'))

    def test_stage_two_the_running_version_pid_or_start_time_is_wrong(self):
        self.assertEqual(self.evaluate(target='9.9.9-1'), ('restart', 'running version 2.0.0-1'))
        self.host.reported_version = None
        self.host.refresh()
        self.assertEqual(self.evaluate(), ('restart', 'running version unknown'))
        self.host.reported_version = 'target'
        (self.root / 'version.json').write_text(json.dumps({
            'schema_version': 1, 'supervisor_version': '2.0.0-1', 'pid': 999,
            'started_monotonic': 1000.5}))
        self.assertEqual(self.evaluate()[0], 'restart')
        (self.root / 'version.json').write_text(json.dumps({
            'schema_version': 1, 'supervisor_version': '2.0.0-1', 'pid': self.host.main_pid,
            'started_monotonic': 1000.0}))
        self.assertEqual(self.evaluate(after=1000.0)[0], 'restart')

    def test_stage_three_the_web_worker_or_status_is_missing(self):
        self.host.web = False
        self.host.refresh()
        self.assertEqual(self.evaluate(), ('health', 'web worker not running'))
        self.host.web = True
        self.host.refresh()
        (self.root / 'status.json').write_text(json.dumps({'published_monotonic': 500.0}))
        self.assertEqual(self.evaluate(), ('health', 'web worker not running'))
        self.host.service = 'inactive'
        self.assertEqual(self.evaluate()[0], 'restart', 'service failures win over web failures')


class WaitServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = FakeClock(NOW)
        self.host = FakeUpdateHost(self.root, self.clock, installed='1.0.0-1')
        self.host.last_install_version = '2.0.0-1'
        self.host.refresh()

    def wait(self, **kwargs):
        options = {'target': '2.0.0-1', 'after': 1000.0, 'web_required': True,
                   'clock': self.clock.monotonic, 'sleep': self.clock.sleep}
        options.update(kwargs)
        return health.wait_service(self.root, self.host, **options)

    def test_it_passes_after_thirty_stable_seconds(self):
        self.assertIsNone(self.wait())
        self.assertGreaterEqual(self.clock.monotonic() - 1000.0, health.STABLE_SECONDS)

    def test_a_main_pid_or_restart_count_change_restarts_the_window(self):
        for attribute in ('pid_change_at', 'restart_at'):
            setattr(self.host, attribute, 1020.0)
            self.assertIsNone(self.wait(), attribute)
            self.assertGreaterEqual(self.clock.monotonic() - 1000.0,
                                    health.STABLE_SECONDS + 15)
            setattr(self.host, attribute, None)
            self.clock.now_monotonic = 1000.0
            self.host.started_monotonic = 1000.5
            self.host.refresh()

    def test_conditions_first_met_at_170_seconds_still_pass(self):
        self.host.healthy_at = 1170.0
        self.assertIsNone(self.wait())
        self.assertGreaterEqual(self.clock.monotonic() - 1000.0, 200.0)

    def test_never_healthy_by_180_seconds_fails(self):
        self.host.healthy_at = float('inf')
        stage, code, detail = self.wait()
        self.assertEqual((stage, code), ('health', 'health_failed'))
        self.assertTrue(detail)

    def test_the_step_tracker_moves_from_restart_to_health_when_the_service_is_up(self):
        self.host.web = False
        self.host.healthy_at = float('inf')
        stages = []
        result = self.wait(on_stage=stages.append)
        self.assertEqual(result[1], 'health_failed')
        self.assertEqual(stages, ['health'])

    def test_the_step_tracker_moves_to_health_as_soon_as_the_service_is_healthy(self):
        stages = []
        self.assertIsNone(self.wait(on_stage=stages.append))
        self.assertEqual(stages, ['health'])

    def test_a_stop_flag_raises_interrupted(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Interrupted):
            self.wait(stop=stop)
        self.assertEqual(self.clock.monotonic(), 1000.0, 'no sleep after a stop')


class WaitModemsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = FakeClock(NOW)
        self.host = FakeUpdateHost(self.root, self.clock, online=('default',))
        self.before = health.online_profiles(self.root, self.clock.monotonic())

    def test_a_profile_is_back_only_with_a_new_worker_id(self):
        self.assertEqual(self.before, {'default': self.host.worker_id})
        self.host._restarted('2.0.0-1')
        self.assertEqual(health.wait_modems(self.root, self.before, deadline=1600.0,
                                           clock=self.clock.monotonic, sleep=self.clock.sleep), [])

    def test_a_profile_that_never_returns_is_reported_and_a_stop_interrupts(self):
        self.host.after_install = {'online': ()}
        self.host._restarted('2.0.0-1')
        missing = health.wait_modems(self.root, self.before, deadline=1100.0,
                                     clock=self.clock.monotonic, sleep=self.clock.sleep)
        self.assertEqual(missing, ['default'])
        self.assertGreaterEqual(self.clock.monotonic(), 1100.0)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Interrupted):
            health.wait_modems(self.root, self.before, deadline=9999.0,
                               clock=self.clock.monotonic, sleep=self.clock.sleep, stop=stop)


if __name__ == '__main__':
    unittest.main()
