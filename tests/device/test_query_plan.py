from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading
import unittest

from modules.device.query_plan import GROUPS, PRESETS, QueryPlan, periods
from modules.runtime.settings import Settings, validate_settings

try:
    from modules.device.backend import QmiBackend
except (ImportError, ValueError):
    QmiBackend = None


def telemetry(**values):
    return replace(Settings().telemetry, **values)


def run(plan, settings, seconds, statuses=None):
    """Due groups per 1-second sample, completing every request as `ok`."""
    batches = []
    for now in range(seconds):
        groups = plan.due(settings, now + 0.003 * (now % 3))
        records = {GROUPS[group]: {'status': (statuses or {}).get(group, 'ok'), 'received_at_utc': str(now)}
                   for group in groups}
        plan.completed(groups, records, now)
        batches.append(groups)
    return batches


class QueryPlanTests(unittest.TestCase):
    def test_fixed_profiles_request_their_historical_groups_in_every_sample(self):
        for profile, expected in PRESETS.items():
            batches = run(QueryPlan(), telemetry(profile=profile), 5)
            self.assertTrue(all(batch == expected for batch in batches), profile)

    def test_custom_periods_refresh_each_group_at_its_own_rate(self):
        batches = run(QueryPlan(), telemetry(profile='custom'), 60)
        counts = {group: sum(group in batch for batch in batches) for group in GROUPS}
        self.assertEqual(counts, {'signal': 60, 'cell': 60, 'txrx': 60, 'wds_stats': 60, 'wds_status': 60,
                                  'serving': 12, 'bands': 6, 'wds_rates': 6, 'home': 1, 'strength': 0})

    def test_slow_groups_are_staggered_instead_of_joining_one_sample(self):
        batches = run(QueryPlan(), telemetry(profile='custom'), 30)
        slow = {'serving', 'bands', 'wds_rates', 'home'}
        self.assertLessEqual(max(len(batch & slow) for batch in batches[1:]), 2)
        self.assertNotEqual([i for i, batch in enumerate(batches) if 'bands' in batch],
                            [i for i, batch in enumerate(batches) if 'wds_rates' in batch])

    def test_a_request_that_was_not_sent_is_retried_in_the_next_sample(self):
        plan = QueryPlan()
        settings = telemetry(profile='custom')
        first = plan.due(settings, 0)
        self.assertIn('serving', first)
        plan.completed(first, {GROUPS['serving']: {'status': 'not_requested'}}, 0)
        self.assertIn('serving', plan.due(settings, 1))

    def test_periods_shorter_than_the_interval_mean_every_sample(self):
        values = periods(telemetry(profile='custom', interval=2.0, collection_budget=1.0, query_bands=0.5))
        self.assertEqual(values['bands'], 2.0)
        self.assertEqual(values['strength'], 0)

    def test_changing_a_period_reschedules_only_that_group(self):
        plan = QueryPlan()
        run(plan, telemetry(profile='custom'), 3)
        groups = plan.due(telemetry(profile='custom', query_serving=7.0), 3)
        self.assertIn('serving', groups)
        self.assertNotIn('bands', groups)
        self.assertEqual(plan.period('serving'), 7.0)

    def test_report_describes_periods_and_last_observations(self):
        plan = QueryPlan()
        run(plan, telemetry(profile='custom'), 2)
        report = plan.report({'signal'})
        self.assertEqual(report['signal'], {'interval_s': 1.0, 'queried': True, 'last_observed_at_utc': '1'})
        self.assertFalse(report['bands']['queried'])
        self.assertNotIn('strength', report)

    def test_validation_bounds_periods_and_keeps_bearer_status_while_connecting(self):
        for value in (-1.0, float('nan'), 86401.0):
            with self.assertRaises(ValueError):
                validate_settings(replace(Settings(), telemetry=telemetry(query_bands=value)))
        with self.assertRaises(ValueError):
            validate_settings(replace(Settings(), telemetry=telemetry(profile='custom', query_wds_status=0.0)))
        monitor = replace(Settings(), modem=replace(Settings().modem, mode='monitor'),
                          telemetry=telemetry(profile='custom', query_wds_status=0.0))
        self.assertIs(validate_settings(monitor), monitor)


@unittest.skipIf(QmiBackend is None, 'Requires libqmi GI')
class BackendScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.batches = []
        settings = replace(Settings(), telemetry=telemetry(profile='custom'))
        self.backend = QmiBackend(settings, threading.Event())
        self.backend.session = Mock()
        self.backend.session.collect.side_effect = self.collect
        self.backend.dms.specifications = Mock(return_value=[])
        self.backend.dms.sim_specifications = Mock(return_value=[])
        self.backend.observation_state = 'ONLINE'
        clock = patch('modules.device.backend.time.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def collect(self, requests, *, budget, request_timeout):
        if requests:
            self.batches.append({item[0] for item in requests})
        records = {key: {'status': 'ok', 'received_at_utc': str(self.now)} for key, *_ in requests}
        return {}, {'requests': records, 'complete': True}

    def test_only_due_groups_are_requested_and_schedule_is_reported(self):
        for second in range(10):
            self.now = second
            _, dynamic = self.backend.metrics()
        every = {GROUPS[group] for group in ('signal', 'cell', 'txrx', 'wds_stats', 'wds_status')}
        self.assertTrue(all(every <= batch for batch in self.batches))
        self.assertEqual(sum(GROUPS['bands'] in batch for batch in self.batches), 1)
        self.assertNotIn(GROUPS['strength'], set().union(*self.batches))
        schedule = self.backend.collection_report['schedule']
        self.assertEqual(schedule['serving']['interval_s'], 5.0)
        self.assertNotIn(GROUPS['home'], self.backend.collection_report['requests'])

    def test_registration_still_observes_serving_system_every_sample(self):
        self.backend.observation_state = 'WAIT_REGISTRATION'
        for second in range(3):
            self.now = second
            self.backend.metrics()
        self.assertTrue(all({'nas.get_serving_system', 'nas.get_system_info'} <= batch for batch in self.batches))
        self.assertFalse(any(key.startswith('wds.') for batch in self.batches for key in batch))

    def test_health_uses_the_bearer_status_period_without_extra_queries(self):
        self.backend.settings = replace(self.backend.settings,
                                        telemetry=telemetry(profile='custom', query_wds_status=5.0))
        self.backend.metrics()
        self.backend._last_status, self.backend._last_status_at = 'connected', 0
        self.backend.present = Mock(return_value=True)
        self.backend.network = SimpleNamespace(healthy=lambda: True)
        self.backend.wds.connected = Mock(side_effect=AssertionError('No QMI outside the sample'))
        self.now = 8
        self.assertTrue(self.backend.healthy())


if __name__ == '__main__':
    unittest.main()
