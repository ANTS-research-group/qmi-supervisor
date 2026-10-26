from dataclasses import replace
from datetime import datetime, timedelta, timezone
import csv
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modules.telemetry.metrics import ListDynamicMetrics
from modules.device.device import Device
from modules.runtime.settings import TelemetrySettings
from modules.telemetry.collector import Telemetry, TIMING_COLUMNS, fields


class TelemetryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='qmi-telemetry-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.settings = TelemetrySettings(directory=self.directory.name)
        self.device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', 'serial-1', 'modem')
        self.backend = SimpleNamespace(metrics=Mock(return_value=({'dms_value_imei': '123'}, {'wds_value_tx_bytes_ok': 0})),
                                       collection_report={'requests': [{'name': 'signal', 'duration_ms': 2}]})
        self.stop = threading.Event()

    def telemetry(self, **settings):
        instance = Telemetry(replace(self.settings, **settings), self.stop)
        self.addCleanup(instance.close)
        return instance

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail('Worker did not reach expected state within the deadline')

    def test_slow_storage_does_not_block_acquisition_and_queue_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        telemetry = self.telemetry(queue_size=2)
        warning_patch = patch.object(telemetry.log, 'warning')
        warning = warning_patch.start()
        self.addCleanup(warning_patch.stop)
        original = telemetry.persist
        def blocked(payload):
            entered.set()
            release.wait(2)
            return original(payload)
        telemetry.persist = blocked
        self.addCleanup(release.set)
        telemetry.sample(self.backend, self.device)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        for _ in range(9):
            telemetry.sample(self.backend, self.device)
        elapsed = time.monotonic()-started
        stats = telemetry.stats()
        self.assertLess(elapsed, 0.2)
        self.assertEqual(stats['acquired'], 10)
        self.assertEqual(stats['persistence_dropped'], 7)
        self.assertEqual((stats['pending'], stats['queued'], stats['inflight']), (3, 2, True))
        self.assertGreater(stats['queue_age_ms'], 0)
        warning.assert_not_called()
        release.set()
        self.assertTrue(telemetry.close(2))
        warning.assert_called_once_with('Telemetry %s=%s; bounded worker queue is full', 'persistence_dropped', 7)
        self.assertEqual(telemetry.stats()['persisted'], 3)
        self.assertGreater(telemetry.stats()['write_p95_ms'], 0)
        self.assertGreater(telemetry.stats()['last_write_latency_ms'], 0)
        self.assertEqual(telemetry.stats()['pending'], 0)
        self.assertEqual(self.backend.metrics.call_count, 10)

    def test_close_reports_timeout_then_drains_after_global_stop(self):
        entered, release = threading.Event(), threading.Event()
        telemetry = self.telemetry()
        original = telemetry.persist
        def blocked(payload):
            entered.set()
            release.wait(2)
            return original(payload)
        telemetry.persist = blocked
        self.addCleanup(release.set)
        telemetry.sample(self.backend, self.device)
        self.assertTrue(entered.wait(1))
        telemetry.sample(self.backend, self.device)
        self.stop.set()
        started = time.monotonic()
        self.assertFalse(telemetry.close(0.02))
        self.assertLess(time.monotonic()-started, 0.2)
        release.set()
        self.assertTrue(telemetry.close(2))
        self.assertEqual(telemetry.stats()['persisted'], 2)
        self.assertFalse(telemetry.stats()['writer_alive'])

    def test_writer_uses_acquired_snapshot_and_never_requeries_backend(self):
        telemetry = self.telemetry()
        payload = telemetry.sample(self.backend, self.device)
        self.backend.collection_report['requests'][0]['duration_ms'] = 999
        self.backend.metrics.side_effect = AssertionError('Writer must not consult modem')
        self.assertTrue(telemetry.close())
        saved = json.loads((Path(self.directory.name)/'modem_123/stats.json').read_text())
        self.assertEqual(saved['collection']['requests'][0]['duration_ms'], 2)
        self.assertEqual(saved, payload)
        self.backend.metrics.assert_called_once()

    def test_dynamic_csv_timing_zero_and_missing_values_and_static_changes(self):
        telemetry = self.telemetry()
        lte = {'earfcn': 1300, 'cells': [{'physical_cell_id': 12, 'rsrp': -98.7},
                                       {'physical_cell_id': 18, 'rsrp': None}]}
        self.backend.metrics.return_value[1]['nas_value_lte_intrafrequency'] = lte
        start = time.monotonic()
        first = telemetry.sample(self.backend, self.device, scheduled_monotonic=start-0.1,
                                 scheduled_at_utc='2026-10-19T12:00:00+00:00', skipped_intervals=3)
        telemetry.sample(self.backend, self.device)
        self.backend.metrics.return_value = ({'dms_value_imei': '123', 'dms_value_esn': 'changed'}, {})
        telemetry.sample(self.backend, self.device)
        self.assertTrue(telemetry.close())
        folder = Path(self.directory.name)/'modem_123'
        with next(folder.glob('dynamic*.csv')).open() as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            self.assertEqual(reader.fieldnames[:len(TIMING_COLUMNS)], TIMING_COLUMNS)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['sample_id'], '1')
        self.assertEqual(rows[0]['scheduled_at_utc'], '2026-10-19T12:00:00+00:00')
        self.assertEqual(rows[0]['skipped_intervals'], '3')
        self.assertEqual(rows[0]['wds_value_tx_bytes_ok'], '0')
        self.assertEqual(json.loads(rows[0]['nas_value_lte_intrafrequency']), lte)
        self.assertEqual(rows[0]['nas_value_io'], '')
        self.assertGreaterEqual(first['timing']['schedule_lag_ms'], 100)
        with next(folder.glob('static*.csv')).open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 2)

    def test_timing_measures_monotonic_duration_independent_of_utc_jump(self):
        telemetry = self.telemetry(csv=False, json=False)
        start = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        with patch('modules.telemetry.collector.time.monotonic', side_effect=[10.0, 10.125, 10.125]), \
                patch('modules.telemetry.collector.datetime') as utc:
            utc.now.side_effect = [start, start-timedelta(seconds=20)]
            payload = telemetry.sample(self.backend, self.device, scheduled_monotonic=9.9)
        self.assertAlmostEqual(payload['timing']['acquisition_duration_ms'], 125)
        self.assertAlmostEqual(payload['timing']['schedule_lag_ms'], 100)
        self.assertEqual(payload['timing']['acquired_start_monotonic'], 10)
        self.assertEqual(payload['timing']['scheduled_at_utc'], (start-timedelta(seconds=.1)).isoformat())

    def test_utc_adjustment_between_samples_does_not_corrupt_period_or_schedule(self):
        telemetry = self.telemetry(csv=False, json=False)
        start = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        with patch('modules.telemetry.collector.time.monotonic', side_effect=[10, 10.1, 10.1, 11, 11.1, 11.1]), \
                patch('modules.telemetry.collector.datetime') as utc:
            utc.now.side_effect = [start, start+timedelta(seconds=.1),
                                   start+timedelta(seconds=31), start+timedelta(seconds=31.1)]
            telemetry.sample(self.backend, self.device, scheduled_monotonic=10)
            payload = telemetry.sample(self.backend, self.device, scheduled_monotonic=11)
        self.assertAlmostEqual(payload['timing']['effective_interval_ms'], 1000)
        self.assertAlmostEqual(payload['timing']['wall_clock_step_ms'], 30000)
        self.assertEqual(payload['timing']['schedule_lag_ms'], 0)
        self.assertEqual(payload['timing']['scheduled_at_utc'], (start+timedelta(seconds=31)).isoformat())

    def test_existing_header_is_read_once_and_writer_handle_is_reused(self):
        telemetry = self.telemetry(json=False)
        day = datetime.now(timezone.utc).strftime('%Y%m%d')
        folder = Path(self.directory.name)/'modem_123'
        folder.mkdir()
        dynamic = folder/f'dynamic_stats_v2_{day}.csv'
        with dynamic.open('w') as stream:
            csv.writer(stream).writerow(TIMING_COLUMNS+fields(ListDynamicMetrics))
        original_open, calls = Path.open, []
        def tracked(path, *args, **kwargs):
            if path == dynamic:
                calls.append(args[0] if args else 'r')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', tracked):
            for _ in range(20):
                telemetry.sample(self.backend, self.device)
            self.assertTrue(telemetry.close())
        self.assertEqual(calls, ['r', 'a'])

    def test_periodic_flush_makes_csv_visible_before_shutdown(self):
        telemetry = self.telemetry(json=False, flush_interval=0.02)
        telemetry.sample(self.backend, self.device)
        folder = Path(self.directory.name)/'modem_123'
        def visible():
            files = list(folder.glob('dynamic*.csv'))
            return bool(files and files[0].stat().st_size)
        self.wait_for(visible)
        with next(folder.glob('dynamic*.csv')).open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_csv_failure_is_reported_without_preventing_json(self):
        telemetry = self.telemetry()
        telemetry.csv_row = Mock(side_effect=OSError('disk write failed'))
        telemetry.sample(self.backend, self.device)
        self.assertFalse(telemetry.close())
        self.assertTrue((Path(self.directory.name)/'modem_123/stats.json').exists())
        self.assertEqual(telemetry.stats()['persistence_failed'], 1)
        self.assertIn('disk write failed', telemetry.stats()['last_persistence_error'])

    def test_incompatible_header_is_checked_once_and_never_appended(self):
        telemetry = self.telemetry(json=False)
        day = datetime.now(timezone.utc).strftime('%Y%m%d')
        folder = Path(self.directory.name)/'modem_123'
        folder.mkdir()
        dynamic = folder/f'dynamic_stats_v2_{day}.csv'
        dynamic.write_text('old,header\n')
        original_open, reads = Path.open, []
        def tracked(path, *args, **kwargs):
            if path == dynamic:
                reads.append(args[0] if args else 'r')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', tracked):
            for _ in range(3):
                telemetry.sample(self.backend, self.device)
            self.assertFalse(telemetry.close())
        self.assertEqual(reads, ['r'])
        self.assertEqual(dynamic.read_text(), 'old,header\n')
        self.assertEqual(telemetry.stats()['persistence_failed'], 3)

    def test_disabled_telemetry_has_no_workers_or_acquisition(self):
        telemetry = self.telemetry(enabled=False, gps=True, udp_host='unused.invalid')
        self.assertIsNone(telemetry.sample(self.backend, self.device))
        self.backend.metrics.assert_not_called()
        self.assertIsNone(telemetry._writer)
        self.assertIsNone(telemetry._udp_worker)
        self.assertIsNone(telemetry._gps_worker)
        self.assertEqual(telemetry.stats()['acquired'], 0)
        self.assertTrue(telemetry.close())

    def test_publisher_receives_full_payload_without_storage_worker(self):
        publisher = Mock()
        telemetry = Telemetry(replace(self.settings, csv=False, json=False), self.stop, publish=publisher)
        self.addCleanup(telemetry.close)
        payload = telemetry.sample(self.backend, self.device)
        publisher.assert_called_once_with(payload)
        self.assertEqual(payload['device_identity'], 'serial-1')
        self.assertIsNone(telemetry._writer)
        self.assertFalse(list(Path(self.directory.name).iterdir()))

    def test_udp_queue_is_bounded_and_collector_wrapper_is_preserved(self):
        entered, release, sent = threading.Event(), threading.Event(), []
        def blocked(host, port, payload):
            entered.set()
            release.wait(2)
            sent.append((host, port, payload))
        with patch.object(Telemetry, 'udp', staticmethod(blocked)):
            telemetry = self.telemetry(csv=False, json=False, udp_host='udp.example', collector_host='collector.example')
            self.addCleanup(release.set)
            telemetry.sample(self.backend, self.device)
            self.assertTrue(entered.wait(1))
            telemetry.sample(self.backend, self.device)
            telemetry.sample(self.backend, self.device)
            self.assertEqual(telemetry.stats()['udp_dropped'], 1)
            self.assertEqual(telemetry.stats()['udp_pending'], 2)
            release.set()
            self.assertTrue(telemetry.close(2))
        self.assertEqual(telemetry.stats()['udp_sent'], 2)
        wrapped = next(payload for host, _, payload in sent if host == 'collector.example')
        self.assertEqual(set(wrapped), {'static', 'dynamic'})

    def test_gps_preserves_receiver_time_and_filters_old_fix(self):
        with patch.object(Telemetry, 'read_gps', return_value=None):
            telemetry = self.telemetry(csv=False, json=False, gps=True, gps_max_age=10)
        receiver_time = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        telemetry._record_gps({'class': 'TPV', 'time': receiver_time, 'mode': 3, 'lat': 0, 'lon': 2})
        payload = telemetry.sample(self.backend, self.device)
        self.assertEqual(payload['dynamic']['gpsd_tpv_datetime'], receiver_time)
        self.assertEqual(payload['dynamic']['gpsd_tpv_lat'], 0)
        self.assertTrue(payload['timing']['gps']['tpv']['fresh'])
        self.assertEqual(payload['collection']['gps']['status'], 'fixed')
        self.assertEqual(payload['collection']['gps']['source'], 'gpsd')
        old = (datetime.now(timezone.utc)-timedelta(seconds=30)).isoformat()
        telemetry._record_gps({'class': 'TPV', 'time': old, 'mode': 3, 'lat': 8})
        payload = telemetry.sample(self.backend, self.device)
        self.assertIsNone(payload['dynamic']['gpsd_tpv_lat'])
        self.assertIsNone(payload['dynamic']['gpsd_tpv_datetime'])
        self.assertFalse(payload['timing']['gps']['tpv']['fresh'])
        self.assertEqual(payload['timing']['gps']['tpv']['source_time'], old)
        self.assertEqual(payload['collection']['gps']['status'], 'stale')

    def test_gps_new_message_without_fix_does_not_reuse_old_position(self):
        with patch.object(Telemetry, 'read_gps', return_value=None):
            telemetry = self.telemetry(csv=False, json=False, gps=True)
        now = datetime.now(timezone.utc).isoformat()
        telemetry._record_gps({'class': 'TPV', 'time': now, 'mode': 3, 'lat': 15})
        telemetry._record_gps({'class': 'TPV', 'time': now, 'mode': 1})
        self.assertIsNone(telemetry.sample(self.backend, self.device)['dynamic']['gpsd_tpv_lat'])
        telemetry._record_gps({'class': 'TPV', 'mode': 3, 'lat': 15})
        self.assertIsNone(telemetry.sample(self.backend, self.device)['dynamic']['gpsd_tpv_lat'])

    def test_modem_gps_does_not_start_gpsd_or_overwrite_backend_report(self):
        settings = SimpleNamespace(**(dict(vars(self.settings)) | dict(
            csv=False, json=False, gps=True, gps_source='modem')))
        with patch.object(Telemetry, 'read_gps') as read:
            telemetry = Telemetry(settings, self.stop)
        self.addCleanup(telemetry.close)
        self.backend.metrics.return_value[1]['gnss_latitude'] = 0
        self.backend.collection_report = {'gps': {'enabled': True, 'source': 'qmi_loc', 'status': 'fixed'}}
        with patch.object(telemetry, '_gps_snapshot', side_effect=AssertionError('Modem mode must not read GPSd')):
            payload = telemetry.sample(self.backend, self.device)
        self.assertIsNone(telemetry._gps_worker)
        read.assert_not_called()
        self.assertEqual(payload['collection']['gps'], self.backend.collection_report['gps'])
        self.assertEqual(payload['dynamic']['gnss_latitude'], 0)
        self.assertNotIn('gps', payload['timing'])

    def test_disabled_gps_is_explicit_and_never_starts_a_receiver(self):
        telemetry = self.telemetry(csv=False, json=False, gps=False)
        self.backend.collection_report = {'gps': {'enabled': True, 'source': 'qmi_loc', 'status': 'fixed'}}
        payload = telemetry.sample(self.backend, self.device)
        self.assertEqual(payload['collection']['gps'],
                         {'enabled': False, 'source': 'gpsd', 'status': 'disabled', 'error': None})
        self.assertIsNone(telemetry._gps_worker)

    def test_malformed_gps_class_time_and_coordinates_do_not_poison_snapshot(self):
        with patch.object(Telemetry, 'read_gps', return_value=None):
            telemetry = self.telemetry(csv=False, json=False, gps=True)
        for value in (None, [], {'class': None}, {'class': 1}, {'class': []}, {'class': True}):
            telemetry._record_gps(value)
        self.assertEqual(telemetry.gps, {})
        for source_time in (float('nan'), float('inf'), -float('inf'), 10**1000):
            telemetry._record_gps({'class': 'TPV', 'time': source_time, 'mode': 3,
                                   'lat': 0, 'lon': 2})
            payload = telemetry.sample(self.backend, self.device)
            self.assertIsNone(payload['dynamic']['gpsd_tpv_lat'])
            self.assertFalse(payload['collection']['gps']['tpv']['fresh'])
            json.dumps(payload, allow_nan=False)
        now = datetime.now(timezone.utc).isoformat()
        for mode in (None, '3', [], True):
            telemetry._record_gps({'class': 'TPV', 'time': now, 'mode': mode, 'lat': 0, 'lon': 2})
            self.assertIsNone(telemetry.sample(self.backend, self.device)['dynamic']['gpsd_tpv_lat'])
        telemetry._record_gps({'class': 'TPV', 'time': now, 'mode': 3,
            'lat': float('nan'), 'lon': float('inf'), 'alt': 10**1000, 'speed': 'fast'})
        payload = telemetry.sample(self.backend, self.device)
        self.assertEqual(payload['collection']['gps']['status'], 'no_fix')
        self.assertIsNone(payload['dynamic']['gpsd_tpv_lat'])
        json.dumps(payload, allow_nan=False)

    def test_gpsd_connection_error_is_published_and_cannot_relabel_cached_fix_as_live(self):
        with patch.object(Telemetry, 'read_gps', return_value=None):
            telemetry = self.telemetry(csv=False, json=False, gps=True)
        telemetry._record_gps({'class': 'TPV', 'time': datetime.now(timezone.utc).isoformat(),
                               'mode': 3, 'lat': 0, 'lon': 2})
        self.assertEqual(telemetry.sample(self.backend, self.device)['collection']['gps']['status'], 'fixed')
        with patch('modules.telemetry.collector.socket.create_connection', side_effect=OSError('connection refused')), \
                patch.object(telemetry._closing, 'wait', side_effect=lambda seconds: self.stop.set()):
            Telemetry.read_gps(telemetry)
        payload = telemetry.sample(self.backend, self.device)
        self.assertEqual(payload['collection']['gps']['status'], 'disconnected')
        self.assertIn('connection refused', payload['collection']['gps']['error'])
        self.assertIsNone(payload['dynamic']['gpsd_tpv_lat'])
        self.assertIsNone(payload['dynamic']['gpsd_tpv_datetime'])

    def test_gpsd_fix_cannot_be_rejuvenated_by_a_backward_wall_clock_step(self):
        with patch.object(Telemetry, 'read_gps', return_value=None):
            telemetry = self.telemetry(csv=False, json=False, gps=True, gps_max_age=10)
        receipt = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        receiver = receipt-timedelta(seconds=9)
        class WallClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return receipt
        with patch('modules.telemetry.collector.datetime', WallClock), \
                patch('modules.telemetry.collector.time.monotonic', return_value=100):
            telemetry._record_gps({'class': 'TPV', 'time': receiver.isoformat(), 'mode': 3, 'lat': 0, 'lon': 2})
        values, report = telemetry._gps_snapshot(102, receipt-timedelta(seconds=100))
        self.assertEqual(report['tpv']['age_ms'], 11000)
        self.assertEqual(report['tpv']['age_at_receipt_ms'], 9000)
        self.assertFalse(report['tpv']['fresh'])
        self.assertNotIn('gpsd_tpv_lat', values)

    def test_modem_gnss_is_recorded_in_csv_without_new_columns_or_mutating_json_payload(self):
        settings = SimpleNamespace(**(dict(vars(self.settings)) | dict(
            csv=True, json=True, gps=True, gps_source='modem')))
        telemetry = Telemetry(settings, self.stop)
        self.addCleanup(telemetry.close)
        stamp = '2026-10-20T12:00:00.250+00:00'
        source_values = dict(gnss_latitude=0, gnss_longitude=2.5, gnss_altitude_msl_m=25,
            gnss_altitude_ellipsoid_m=75, gnss_horizontal_speed_mps=0,
            gnss_horizontal_uncertainty_m=3, gnss_timestamp_utc=stamp)
        self.backend.metrics.return_value[1].update(source_values)
        self.backend.collection_report = {'gps': {'enabled': True, 'source': 'qmi_loc', 'status': 'fixed'}}
        payload = telemetry.sample(self.backend, self.device)
        self.assertTrue(telemetry.close())
        directory = Path(self.directory.name)/'modem_123'
        with next(directory.glob('dynamic_stats_v2_*.csv')).open() as stream:
            reader = csv.DictReader(stream)
            row = next(reader)
            self.assertEqual(reader.fieldnames, TIMING_COLUMNS+fields(ListDynamicMetrics))
        self.assertEqual((row['gpsd_tpv_lat'], row['gpsd_tpv_lon'], row['gpsd_tpv_speed']), ('0', '2.5', '0'))
        self.assertEqual(row['gpsd_tpv_althae'], '75')
        self.assertEqual(row['gpsd_tpv_datetime'], stamp)
        self.assertEqual(float(row['gpsd_tpv_timestamp']), datetime.fromisoformat(stamp).timestamp())
        self.assertEqual((row['gpsd_tpv_alt'], row['gpsd_tpv_epx'], row['gpsd_tpv_epy']), ('', '', ''))
        metadata = json.loads(row['query_timing_json'])['gps']
        self.assertEqual(metadata['source'], 'qmi_loc')
        self.assertEqual(metadata['values']['gnss_altitude_msl_m'], 25)
        saved = json.loads((directory/'stats.json').read_text())
        self.assertEqual(saved, payload)
        self.assertIsNone(payload['dynamic']['gpsd_tpv_lat'])
        self.assertEqual({key: payload['dynamic'][key] for key in source_values}, source_values)

    def test_throughput_resets_on_modem_and_interface_change(self):
        telemetry = self.telemetry(csv=False, json=False)
        with patch.object(Path, 'read_text', side_effect=['1024', '2048', '2048', '4096', '3072', '6144', '4096', '8192']), \
                patch('modules.telemetry.collector.time.monotonic', side_effect=[10, 10, 11, 11, 12, 12, 13, 13]):
            self.assertNotIn('throughput_upload_speed_kbps', telemetry.throughput('wwan0', 'first'))
            self.assertEqual(telemetry.throughput('wwan0', 'first')['throughput_upload_speed_kbps'], 8)
            self.assertNotIn('throughput_upload_speed_kbps', telemetry.throughput('wwan0', 'second'))
            self.assertNotIn('throughput_upload_speed_kbps', telemetry.throughput('wwan1', 'second'))

    def test_disabled_interface_traffic_is_explicit_without_reading_counters(self):
        telemetry = self.telemetry(csv=False, json=False, throughput=False)
        self.backend.metrics.return_value[1].update(throughput_download_speed_kbps=999,
            wds_value_channel_rates_channel_rx_rate_bps=100000000)
        with patch.object(Path, 'read_text', side_effect=AssertionError('Disabled traffic must not read sysfs')):
            payload = telemetry.sample(self.backend, self.device)
        report = payload['collection']['interface_traffic']
        self.assertFalse(report['enabled'])
        self.assertEqual((report['status'], report['interface'], report['source']), ('disabled', 'wwan0', 'sysfs'))
        self.assertIsNone(report['observed_at_utc'])
        self.assertIsNone(payload['dynamic']['throughput_download_speed_kbps'])
        self.assertEqual(payload['dynamic']['wds_value_channel_rates_channel_rx_rate_bps'], 100000000)

    def test_interface_traffic_uses_real_interval_and_keeps_first_rates_null(self):
        telemetry = self.telemetry(csv=False, json=False, throughput=True)
        now = [10.0]
        with patch.object(Path, 'read_text', side_effect=['1024', '2048', '3584', '7168']), \
                patch('modules.telemetry.collector.time.monotonic', side_effect=lambda: now[0]):
            first = telemetry.sample(self.backend, self.device)
            now[0] = 12.5
            second = telemetry.sample(self.backend, self.device)
        self.assertEqual(first['collection']['interface_traffic']['status'], 'initializing')
        self.assertIsNone(first['dynamic']['throughput_upload_speed_kbps'])
        self.assertIsNone(first['dynamic']['throughput_download_speed_kbps'])
        report = second['collection']['interface_traffic']
        self.assertEqual((report['status'], report['interval_seconds'], report['tx_bytes'], report['rx_bytes']),
                         ('ok', 2.5, 3584, 7168))
        self.assertEqual(second['dynamic']['throughput_upload_speed_kbps'], 8)
        self.assertEqual(second['dynamic']['throughput_download_speed_kbps'], 16)
        self.assertEqual(report['rate_unit'], 'Kibit/s')
        self.assertEqual(report['observed_monotonic'], 12.5)
        self.assertTrue(report['observed_at_utc'])
        self.assertEqual(report['previous_observed_at_utc'], first['collection']['interface_traffic']['observed_at_utc'])
        self.assertEqual(second['collection']['units']['throughput_upload_speed_kbps'], 'Kibit/s')

    def test_counter_reset_and_device_generation_change_do_not_create_false_rates(self):
        telemetry = self.telemetry(csv=False, json=False, throughput=True)
        self.backend.generation = 1
        now = [10.0]
        with patch.object(Path, 'read_text', side_effect=['2048', '4096', '1024', '2048', '2048', '4096', '3072', '6144']), \
                patch('modules.telemetry.collector.time.monotonic', side_effect=lambda: now[0]):
            telemetry.sample(self.backend, self.device)
            now[0] = 11
            reset = telemetry.sample(self.backend, self.device)
            now[0] = 12
            normal = telemetry.sample(self.backend, self.device)
            self.backend.generation = 2
            now[0] = 13
            replaced = telemetry.sample(self.backend, self.device)
        self.assertEqual(reset['collection']['interface_traffic']['status'], 'counter_reset')
        self.assertIsNone(reset['dynamic']['throughput_download_speed_kbps'])
        self.assertEqual(normal['dynamic']['throughput_download_speed_kbps'], 16)
        self.assertEqual(replaced['collection']['interface_traffic']['status'], 'initializing')
        self.assertIsNone(replaced['dynamic']['throughput_download_speed_kbps'])

    def test_interface_read_failure_is_visible_and_invalidates_previous_baseline(self):
        telemetry = self.telemetry(csv=False, json=False, throughput=True)
        now = [10.0]
        with patch.object(Path, 'read_text', side_effect=['1024', '2048', OSError('interface disappeared'), '2048', '4096']), \
                patch('modules.telemetry.collector.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(telemetry.log, 'warning') as warning:
            telemetry.sample(self.backend, self.device)
            now[0] = 11
            failed = telemetry.sample(self.backend, self.device)
            now[0] = 12
            recovered = telemetry.sample(self.backend, self.device)
        self.assertEqual(failed['collection']['interface_traffic']['status'], 'error')
        self.assertIn('interface disappeared', failed['collection']['interface_traffic']['error'])
        self.assertIsNone(failed['dynamic']['throughput_download_speed_kbps'])
        self.assertEqual(recovered['collection']['interface_traffic']['status'], 'initializing')
        self.assertIsNone(recovered['dynamic']['throughput_download_speed_kbps'])
        warning.assert_not_called()

    def test_unchanged_counters_produce_measured_zero_only_after_positive_interval(self):
        telemetry = self.telemetry(csv=False, json=False, throughput=True)
        now = [10.0]
        with patch.object(Path, 'read_text', return_value='1024'), \
                patch('modules.telemetry.collector.time.monotonic', side_effect=lambda: now[0]):
            telemetry.sample(self.backend, self.device)
            no_interval = telemetry.sample(self.backend, self.device)
            now[0] = 11
            measured = telemetry.sample(self.backend, self.device)
        self.assertIsNone(no_interval['dynamic']['throughput_download_speed_kbps'])
        self.assertEqual(no_interval['collection']['interface_traffic']['status'], 'initializing')
        self.assertEqual(measured['dynamic']['throughput_download_speed_kbps'], 0)
        self.assertEqual(measured['collection']['interface_traffic']['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
