"""The external resource observer includes every fleet child without fake zeros."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.telemetry.platform_profile import aggregate, sample_processes, summarize, targets


class FleetProfileTests(unittest.TestCase):
    def test_all_children_are_enumerated_without_overwriting_same_role_peers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            children = root/'100/task/100/children'
            children.parent.mkdir(parents=True)
            children.write_text('11 12 13 14 15 16 17')
            for pid, role in ((11, 'modem'), (12, 'modem'), (13, 'telemetry'), (14, 'telemetry'), (15, 'web')):
                path = root/str(pid)/'cmdline'
                path.parent.mkdir()
                path.write_bytes(f'python\0-m\0modules.runtime.workers\0--role\0{role}\0'.encode())
            (root/'16').mkdir()
            (root/'16/cmdline').write_bytes(b'unrelated\0')
            observed = targets(100, root)
            self.assertEqual(observed, {'manager': 100, 'modem:11': 11, 'modem:12': 12,
                                       'telemetry:13': 13, 'telemetry:14': 14, 'web:15': 15})

    def test_role_totals_include_all_peers_and_preserve_measured_zero(self):
        observed = aggregate({'modem:11': dict(state='ok', cpu_percent=0.0, rss_bytes=10),
                              'modem:12': dict(state='ok', cpu_percent=2.5, rss_bytes=20),
                              'web:13': dict(state='ok', cpu_percent=9, rss_bytes=50)}, 'modem')
        self.assertEqual(observed, dict(state='ok', process_count=2, cpu_percent=2.5, rss_bytes=30))

    def test_disabled_roles_have_unavailable_values_instead_of_zero_usage(self):
        self.assertEqual(aggregate({}, 'modem'), dict(state='disabled', process_count=0,
                                                    cpu_percent=None, rss_bytes=None))

    def test_failed_or_warming_process_is_not_replaced_with_zero_in_an_aggregate(self):
        observed = aggregate({'modem:11': dict(state='ok', cpu_percent=2, rss_bytes=10),
                              'modem:12': dict(state='warming_up', cpu_percent=None, rss_bytes=20)}, 'modem')
        self.assertIsNone(observed['cpu_percent'])
        self.assertEqual(observed['rss_bytes'], 30)
        self.assertEqual(observed['state'], 'partial')

    def test_pid_specific_accounting_is_reused_and_removed_after_child_exit(self):
        existing = Mock()
        existing.sample.return_value = {'supervisor': dict(state='ok', cpu_percent=0, rss_bytes=20)}
        stale = Mock()
        samplers = {'modem:11': existing, 'modem:12': stale}
        with patch('tools.telemetry.platform_profile.ProcessSampler') as create:
            observed = sample_processes(samplers, {'modem:11': 11})
        create.assert_not_called()
        stale.sample.assert_not_called()
        self.assertEqual(set(samplers), {'modem:11'})
        self.assertEqual(observed['modem']['cpu_percent'], 0)
        self.assertEqual(observed['modem']['process_count'], 1)
        existing.sample.assert_called_once_with({'supervisor': 11})

    def test_cpu_summary_weights_irregular_intervals_and_includes_both_modems(self):
        samples = []
        for interval, first, second in ((1, 10, 20), (3, 30, 40)):
            records = {f'modem:{pid}': dict(state='ok', cpu_percent=cpu, rss_bytes=100,
                                          cpu_interval_seconds=interval) for pid, cpu in ((11, first), (12, second))}
            samples.append(dict(modem=aggregate(records, 'modem'), processes=records))
        result = summarize(samples, 'modem')
        self.assertEqual(result['cpu_percent_mean'], 60)
        self.assertEqual(result['process_count_max'], 2)
        self.assertEqual(result['rss_bytes_max'], 200)

    def test_incomplete_cpu_observation_keeps_the_summary_unavailable(self):
        records = {'modem:11': dict(state='warming_up', cpu_percent=None, rss_bytes=100,
                                    cpu_interval_seconds=None)}
        result = summarize([dict(modem=aggregate(records, 'modem'), processes=records)], 'modem')
        self.assertIsNone(result['cpu_percent_mean'])
        self.assertEqual(result['rss_bytes_max'], 100)
