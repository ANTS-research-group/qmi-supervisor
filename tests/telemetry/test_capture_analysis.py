from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import csv
import tempfile
import unittest

spec=spec_from_file_location('capture_analysis', Path(__file__).resolve().parents[2]/'tools/telemetry/analyze_capture.py')
analysis=module_from_spec(spec)
spec.loader.exec_module(analysis)


class CaptureAnalysisTests(unittest.TestCase):
    def test_nonfinite_and_malformed_timings_are_counted_and_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dynamic_stats_v2_20261019.csv'
            with path.open('w', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['acquisition_duration_ms'])
                writer.writerows([[value] for value in (0, 'nan', 'inf', '-inf', 'invalid')])
            report = analysis.analyze(directory, window=5)
            self.assertEqual(report['rows'], 5)
            self.assertEqual(report['invalid_timing_fields'], 4)
            self.assertEqual(report['timing']['acquisition_duration_ms']['count'], 1)
            self.assertEqual(report['timing']['acquisition_duration_ms']['last']['p95_ms'], 0)

    def test_streaming_windows_reveal_slowdown_and_declared_losses(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'dynamic_stats_v2_20261019.csv'
            with path.open('w',newline='') as stream:
                writer=csv.writer(stream)
                writer.writerow(['acquisition_duration_ms','schedule_lag_ms','effective_interval_ms',
                                 'skipped_intervals','persistence_dropped_total'])
                for i in range(20):
                    writer.writerow([10 if i<10 else 200,0,1000,1 if i==19 else 0,2])
            report=analysis.analyze(directory,window=5)
            self.assertEqual(report['timing']['acquisition_duration_ms']['first']['p95_ms'],10)
            self.assertEqual(report['timing']['acquisition_duration_ms']['last']['p95_ms'],200)
            self.assertEqual(report['rows'],20)
            self.assertEqual(report['recorded_skipped_intervals'],1)
            self.assertEqual(report['max_reported_persistence_drops'],2)
