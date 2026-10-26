"""Heartbeat writes at most once per second unless a new observation forces it."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from modules.runtime.ipc import Heartbeat, LatestFile, revision
from modules.runtime.settings import Settings


class Recorder:
    """Stand-in for LatestFile that keeps every offered payload in order."""

    def __init__(self):
        self.values = []

    def offer(self, value):
        self.values.append(value)

    def close(self):
        pass


class HeartbeatTests(unittest.TestCase):
    def start(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = Settings()
        beat = Heartbeat(Path(directory.name)/'modem.json', 'worker-1', settings)
        beat.writer.close()
        recorded = Recorder()
        beat.writer = cast(LatestFile, recorded)
        self.addCleanup(beat.close)
        return beat, recorded, settings

    def test_one_beat_per_second_unless_a_new_observation_forces_it(self):
        beat, writer, settings = self.start()
        with patch('modules.runtime.ipc.time.monotonic', side_effect=[100.0, 100.4, 100.9, 102.0]):
            beat.beat(phase='OPEN', log_id=1)
            beat.beat(phase='ONLINE', log_id=2)
            beat.beat(force=True, phase='ONLINE', log_id=2)
            beat.beat(phase='BACKOFF', log_id=3)
        self.assertEqual([value['log_id'] for value in writer.values], [1, 2, 3])
        self.assertEqual([value['phase'] for value in writer.values], ['OPEN', 'ONLINE', 'BACKOFF'])
        first = writer.values[0]
        self.assertEqual((first['worker_id'], first['heartbeat_monotonic']), ('worker-1', 100.0))
        self.assertEqual(first['config_revision'], revision(settings))
        self.assertIn('heartbeat_at_utc', first)
        self.assertEqual(writer.values[1]['heartbeat_monotonic'], 100.9,
                         'a forced beat keeps its own timestamp')
