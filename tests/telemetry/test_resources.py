from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from modules.telemetry.resources import ProcessSampler, read_boot_id


class ProcessResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        boot = self.root / 'sys/kernel/random/boot_id'
        boot.parent.mkdir(parents=True)
        boot.write_text('test-boot\n')
        self.now = 100.0
        self.sampler = ProcessSampler(proc_root=self.root, clock=lambda: self.now,
                                      clock_ticks=100, page_size=4096)

    def record(self, pid=12, *, user=100, system=50, start=7, rss=10,
               name='a tricky ) process', state='R'):
        path = self.root / str(pid) / 'stat'
        path.parent.mkdir(exist_ok=True)
        fields = ['0'] * 22
        fields[0], fields[11], fields[12] = state, str(user), str(system)
        fields[13], fields[14] = '999999', '999999'  # Child CPU must be ignored.
        fields[17], fields[19], fields[20], fields[21] = '3', str(start), '999999', str(rss)
        path.write_text(f'{pid} ({name}) ' + ' '.join(fields))
        return path

    def test_actual_elapsed_interval_and_one_core_cpu_with_current_rss(self):
        self.record()
        first = self.sampler.sample({'supervisor': 12})
        self.assertEqual(first['supervisor']['state'], 'warming_up')
        self.assertIsNone(first['supervisor']['cpu_percent'])
        self.assertEqual(first['supervisor']['rss_bytes'], 40960)
        self.now += 6.25
        self.record(user=225, system=50, rss=20)
        later = self.sampler.sample({'supervisor': 12})
        self.assertEqual(later['interval_seconds'], 6.25)
        self.assertEqual(later['supervisor']['cpu_percent'], 20)
        self.assertEqual(later['supervisor']['rss_bytes'], 81920)
        self.assertEqual(later['web']['state'], 'disabled')

    def test_cpu_can_exceed_one_hundred_and_pid_reuse_resets_it(self):
        self.record()
        self.sampler.sample({'supervisor': 12})
        self.now += 5
        self.record(user=1100)
        self.assertEqual(self.sampler.sample({'supervisor': 12})['supervisor']['cpu_percent'], 200)
        self.now += 5
        self.record(user=1, system=1, start=20)
        result = self.sampler.sample({'supervisor': 12})['supervisor']
        self.assertEqual(result['starttime_ticks'], 20)
        self.assertIsNone(result['cpu_percent'])

    def test_missing_and_invalid_proc_reset_the_baseline_without_false_zero(self):
        path = self.record()
        self.sampler.sample({'supervisor': 12})
        path.unlink()
        self.now += 5
        missing = self.sampler.sample({'supervisor': 12})['supervisor']
        self.assertEqual(missing['state'], 'unavailable')
        self.assertIsNone(missing['rss_bytes'])
        self.assertIsNone(missing['cpu_percent'])
        self.record(user=200)
        self.now += 5
        self.assertIsNone(self.sampler.sample({'supervisor': 12})['supervisor']['cpu_percent'])
        path.write_text('not a process record')
        self.assertEqual(self.sampler.sample({'supervisor': 12})['supervisor']['state'], 'unavailable')

    def test_owned_process_exit_and_pid_reuse_are_not_another_web_process(self):
        self.record()
        child = SimpleNamespace(pid=12, poll=lambda: None)
        self.sampler.sample({'web': child})
        self.now += 5
        self.record(start=99)
        reused = self.sampler.sample({'web': child})['web']
        self.assertEqual(reused['state'], 'unavailable')
        self.assertIsNone(reused['rss_bytes'])
        child.poll = lambda: 0
        self.assertEqual(self.sampler.sample({'web': child})['web']['state'], 'exited')

    def test_zero_cpu_is_valid_after_baseline_and_unknown_boot_is_none(self):
        self.record(name='embedded\nnewline)')
        self.sampler.sample({'supervisor': 12})
        self.now += 5
        result = self.sampler.sample({'supervisor': 12})['supervisor']
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['cpu_percent'], 0)
        self.assertEqual(read_boot_id(self.root), 'test-boot')
        self.assertIsNone(read_boot_id(self.root / 'missing'))
