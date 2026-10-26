from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from modules.runtime.platform import OptionalWeb, StatusPublisher, _history_error
from modules.runtime.settings import Settings, load_settings
from modules.deployment.provisioning import render_config
from modules.device.device import Device
from modules.telemetry.collector import Telemetry


class OptionalPlatformTests(unittest.TestCase):
    def test_historical_errors_are_bounded_in_json_bytes_including_unicode(self):
        for message in ('error' * 100, '\U0001f4f6' * 200, '\u6e2c\u5b9a' * 200, '\\"\n' * 200):
            short, truncated = _history_error(message)
            self.assertTrue(truncated)
            self.assertTrue(message.startswith(short))
            self.assertLessEqual(len(json.dumps(short))-2, 256)
        self.assertEqual(_history_error('short'), ('short', False))

    def supervisor(self, *, state='ONLINE', generation=1, stats=None):
        device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', 'test', 'test modem')
        return SimpleNamespace(device=device, backend=SimpleNamespace(network=None, generation=generation),
            state=SimpleNamespace(name=state), settings=Settings(), last_error=None, failed_state=None,
            deadline=0, clock=lambda: 0, skipped_intervals=0,
            telemetry=SimpleNamespace(run_id='run-test', stats=lambda: dict(stats or {})))

    def test_web_disabled_starts_no_process(self):
        with patch('modules.runtime.platform.subprocess.Popen') as spawn:
            web = OptionalWeb(Settings(), None)
            web.close()
        spawn.assert_not_called()

    def test_published_route_metric_is_applied_not_requested_and_preserves_zero(self):
        for applied in (99, 0, None):
            with self.subTest(applied=applied), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'status.json'
                publisher = StatusPublisher(path)
                supervisor = self.supervisor()
                supervisor.settings = replace(Settings(), network=replace(
                    Settings().network, route_metric=100, route_policy='primary'))
                supervisor.backend.network = SimpleNamespace(route_metric=applied,
                    route=None if applied is None else ['default', 'dev', 'wwan0', 'metric', str(applied)])
                publisher.heartbeat(supervisor)
                self.assertTrue(publisher.close())
                network = json.loads(path.read_text())['network']
                self.assertEqual(network['route_metric'], applied)
                self.assertEqual(network['requested_route_metric'], 100)
                self.assertEqual(network['route_policy'], 'primary')

    def test_web_process_resolves_caller_paths_before_using_project_directory(self):
        settings = replace(Settings(), web=replace(Settings().web, enabled=True))
        with tempfile.TemporaryDirectory() as directory, \
                patch('os.getcwd', return_value=directory), \
                patch('modules.runtime.platform.subprocess.Popen') as spawn:
            web = OptionalWeb(settings, 'etc/config.ini', 'runtime/status.json')
            web.close()
        spawn.assert_called_once_with([
            sys.executable, '-m', 'modules.web',
            '--config', str(Path(directory)/'etc/config.ini'),
            '--status', str(Path(directory)/'runtime/status.json'),
        ], cwd=Path(__file__).resolve().parents[2])
        spawn.return_value.terminate.assert_called_once_with()

    def test_connectivity_status_publication_retains_probe_time_without_creating_new_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            publisher = StatusPublisher(path, clock=lambda: 100)
            supervisor = self.supervisor()
            supervisor.backend.network = SimpleNamespace(connectivity_status={
                'state': 'down', 'route_mode': 'fallback', 'checked_monotonic': 85,
                'last_success_monotonic': 70, 'sequence': 4, 'preferred': False})
            publisher.heartbeat(supervisor)
            self.assertTrue(publisher.close())
            status = json.loads(path.read_text())['network']['connectivity']
            self.assertEqual(status['checked_monotonic'], 85)
            self.assertEqual(status['last_success_monotonic'], 70)
            self.assertEqual(status['sequence'], 4)
            self.assertEqual(status['route_mode'], 'fallback')

    def test_publisher_is_bounded_and_writes_latest_without_modem_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            publisher = StatusPublisher(path, history_size=3)
            for index in range(100):
                publisher.offer({'log_id':index,'end_datetime':datetime.now(timezone.utc).isoformat(),
                                 'dynamic':{'nas_value_5g_signal_strength_rsrp':-90},
                                 'timing':{'schedule_lag_ms':0}})
            self.assertTrue(publisher.close())
            snapshot=json.loads(path.read_text())
            self.assertEqual(snapshot['sample']['log_id'],99)
            self.assertEqual(len(snapshot['history']),3)
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_new_observation_is_written_without_waiting_for_the_one_second_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            now = [100.0]
            publisher = StatusPublisher(path, clock=lambda: now[0])
            supervisor = self.supervisor()
            deadline = time.monotonic()+2
            publisher.heartbeat(supervisor)
            while (not path.exists() or json.loads(path.read_text())['published_monotonic'] != 100) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(path.exists(), 'the liveness write reaches the file')
            publisher.offer({'log_id': 1, 'end_datetime': 'sample-time', 'device': supervisor.device.path,
                'device_identity': supervisor.device.identity, 'device_generation': 1,
                'dynamic': {}, 'timing': {'run_id': 'run-test'}})
            while ((json.loads(path.read_text()).get('sample') or {}).get('log_id')) != 1 and time.monotonic() < deadline:
                time.sleep(.01)
            snapshot = json.loads(path.read_text())
            self.assertEqual(snapshot['sample']['log_id'], 1)
            self.assertEqual(snapshot['published_monotonic'], 100,
                             'a new observation is published at once, not on the next grid tick')
            self.assertTrue(publisher.close())

    def test_decimal_settings_roundtrip_for_http_and_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.ini'
            settings=replace(Settings(),telemetry=replace(Settings().telemetry,interval=1.25,collection_budget=.75),
                             recovery=replace(Settings().recovery,retry_initial=2.5))
            path.write_text(render_config(settings))
            self.assertEqual(load_settings(path),settings)

    def test_connection_failure_is_published_even_without_telemetry_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            publisher = StatusPublisher(path)
            supervisor = SimpleNamespace(
                device=None, backend=SimpleNamespace(network=None), state=SimpleNamespace(name='BACKOFF'),
                settings=Settings(), telemetry=SimpleNamespace(stats=lambda: {'acquired': 0}),
                skipped_intervals=0, last_error='SIM absent; slot 1: absent', failed_state='OPEN',
                deadline=30, clock=lambda: 10)
            publisher.heartbeat(supervisor)
            self.assertTrue(publisher.close())
            status = json.loads(path.read_text())
            self.assertEqual(status['error'], supervisor.last_error)
            self.assertEqual(status['failed_step'], 'OPEN')
            self.assertEqual(status['retry_in_seconds'], 20)
            self.assertIsNone(status['sample'])

    def test_registration_wait_publishes_current_sample_without_claiming_a_connected_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            publisher = StatusPublisher(path)
            supervisor = self.supervisor(state='WAIT_REGISTRATION')
            supervisor.settings = replace(Settings(), modem=replace(Settings().modem, apn='test.apn', plmn='99910'))
            publisher.heartbeat(supervisor)
            sample = {'log_id': 1, 'device': supervisor.device.path,
                'device_identity': supervisor.device.identity, 'device_generation': 1,
                'end_datetime': 'sample-time', 'static': {'dms_value_imei': 'test-imei'},
                'dynamic': {'nas_value_lte_rsrp': -98,
                            'nas_value_serving_system_registration_state': 'not-registered',
                            'wds_value_connection_status': None},
                'timing': {'run_id': 'run-test'},
                'collection': {'observation_state': 'WAIT_REGISTRATION',
                               'connection': {'phase': 'WAIT_REGISTRATION'}}}
            publisher.offer(sample)
            publisher.heartbeat(supervisor)
            self.assertTrue(publisher.close())
            snapshot = json.loads(path.read_text())
            self.assertEqual(snapshot['state'], 'WAIT_REGISTRATION')
            self.assertEqual(snapshot['device']['interface'], 'wwan0')
            self.assertEqual(snapshot['sample'], sample)
            self.assertEqual(snapshot['history'][-1]['rsrp'], -98)
            self.assertIsNone(snapshot['sample']['dynamic']['wds_value_connection_status'])
            self.assertIsNone(snapshot['network']['address'])
            self.assertEqual(snapshot['connection']['requested_apn'], 'test.apn')
            self.assertEqual(snapshot['connection']['requested_plmn'], '99910')
            self.assertEqual(snapshot['connection']['phase'], 'WAIT_REGISTRATION')

    def test_registration_wait_without_sample_still_publishes_selected_device_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            publisher = StatusPublisher(path)
            supervisor = self.supervisor(state='WAIT_REGISTRATION')
            publisher.heartbeat(supervisor)
            self.assertTrue(publisher.close())
            snapshot = json.loads(path.read_text())
            self.assertEqual(snapshot['state'], 'WAIT_REGISTRATION')
            self.assertEqual(snapshot['device']['product'], 'test modem')
            self.assertEqual(snapshot['device']['path'], '/dev/cdc-wdm0')
            self.assertIsNone(snapshot['sample'])
            self.assertIsNone(snapshot['radio'])

    def test_registration_wait_retires_prior_online_samples_and_old_device_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = StatusPublisher(Path(directory)/'status.json')
            supervisor = self.supervisor()
            publisher.heartbeat(supervisor)
            online = {'log_id': 1, 'device': supervisor.device.path,
                'device_identity': supervisor.device.identity, 'device_generation': 1,
                'end_datetime': 'sample-time', 'dynamic': {'wds_value_connection_status': 'connected'},
                'timing': {'run_id': 'run-test'}, 'collection': {'observation_state': 'ONLINE'}}
            publisher.offer(online)
            supervisor.state.name = 'WAIT_REGISTRATION'
            publisher.heartbeat(supervisor)
            self.assertIsNone(publisher.sample)
            waiting = dict(online, log_id=2,
                dynamic={'nas_value_lte_rsrp': -90},
                collection={'observation_state': 'WAIT_REGISTRATION',
                            'connection': {'phase': 'WAIT_REGISTRATION'}})
            publisher.offer(waiting)
            publisher.heartbeat(supervisor)
            self.assertIs(publisher.sample, waiting)
            supervisor.backend.generation = 2
            publisher.heartbeat(supervisor)
            self.assertIsNone(publisher.sample)
            self.assertFalse(publisher.history)
            publisher.offer(waiting)
            self.assertIsNone(publisher.sample)
            current = dict(waiting, device_generation=2, log_id=3)
            publisher.offer(current)
            publisher.heartbeat(supervisor)
            self.assertIs(publisher.sample, current)
            supervisor.state.name = 'BACKOFF'
            publisher.heartbeat(supervisor)
            self.assertIsNone(publisher.sample)
            self.assertTrue(publisher.close())

    def test_signal_history_keeps_quality_and_radio_source_for_each_metric(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json'
            publisher=StatusPublisher(path)
            publisher.offer({'log_id':1,'end_datetime':datetime.now(timezone.utc).isoformat(),
                'dynamic':{'nas_value_5g_signal_strength_rsrp':-86,
                           'nas_value_5g_signal_strength_extended':-12,
                           'nas_value_lte_snr':0},'timing':{'schedule_lag_ms':0}})
            self.assertTrue(publisher.close())
            point=json.loads(path.read_text())['history'][0]
            self.assertEqual((point['rsrp'],point['rsrp_rat']),(-86,'NR'))
            self.assertEqual((point['rsrq'],point['rsrq_rat']),(-12,'NR'))
            self.assertEqual((point['snr'],point['snr_rat']),(0,'LTE'))

    def test_interface_traffic_history_is_bounded_and_uses_observed_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            publisher = StatusPublisher(path, history_size=3)
            for index in range(5):
                publisher.offer({'log_id': index, 'end_datetime': 'sample-time',
                    'dynamic': {'throughput_download_speed_kbps': index*16,
                                'throughput_upload_speed_kbps': 0,
                                'wds_value_channel_rates_channel_rx_rate_bps': 100000000},
                    'collection': {'interface_traffic': {'enabled': True, 'interface': 'wwan0',
                        'status': 'ok', 'observed_at_utc': 'counter-time',
                        'observed_monotonic': 100+index, 'interval_seconds': 1.25}},
                    'timing': {}})
            self.assertTrue(publisher.close())
            history = json.loads(path.read_text())['history']
            self.assertEqual(len(history), 3)
            self.assertEqual([point['throughput_download_speed_kbps'] for point in history], [32, 48, 64])
            self.assertEqual(history[-1]['throughput_upload_speed_kbps'], 0)
            self.assertEqual(history[-1]['traffic_rate_unit'], 'Kibit/s')
            self.assertEqual(history[-1]['traffic_interface'], 'wwan0')
            self.assertEqual(history[-1]['traffic_observed_at_utc'], 'counter-time')
            self.assertEqual(history[-1]['traffic_observed_monotonic'], 104)
            self.assertEqual(history[-1]['traffic_interval_seconds'], 1.25)
            self.assertNotIn('wds_value_channel_rates_channel_rx_rate_bps', history[-1])

    def test_unavailable_or_invalid_traffic_is_a_gap_and_legacy_rates_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = StatusPublisher(Path(directory)/'status.json')
            for index, status in enumerate(('disabled', 'initializing', 'counter_reset', 'error')):
                publisher.offer({'log_id': index, 'end_datetime': 'sample-time',
                    'dynamic': {'throughput_download_speed_kbps': 999},
                    'collection': {'interface_traffic': {'enabled': status != 'disabled', 'status': status}},
                    'timing': {}})
                self.assertIsNone(publisher.history[-1]['throughput_download_speed_kbps'])
            for index, value in enumerate((None, True, -1, float('nan'), float('inf'), 10**1000)):
                publisher.offer({'log_id': index+4, 'end_datetime': 'sample-time',
                    'dynamic': {'throughput_download_speed_kbps': value}, 'timing': {}})
                self.assertIsNone(publisher.history[-1]['throughput_download_speed_kbps'])
            publisher.offer({'log_id': 10, 'end_datetime': 'sample-time',
                'dynamic': {'throughput_download_speed_kbps': 12.5, 'throughput_upload_speed_kbps': 0},
                'timing': {}})
            self.assertEqual(publisher.history[-1]['throughput_download_speed_kbps'], 12.5)
            self.assertEqual(publisher.history[-1]['throughput_upload_speed_kbps'], 0)
            self.assertTrue(publisher.close())

    def test_web_configuration_requires_strong_auth_for_non_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.ini'
            settings=replace(Settings(),web=replace(Settings().web,enabled=True,host='0.0.0.0'))
            path.write_text(render_config(settings))
            with self.assertRaises(ValueError):
                load_settings(path)

    def test_writer_history_has_its_own_time_and_is_bounded_at_one_hz(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100.0]
            publisher = StatusPublisher(Path(directory)/'status.json', history_size=2,
                clock=lambda: now[0], utcnow=lambda: datetime.fromtimestamp(now[0], timezone.utc))
            supervisor = self.supervisor(stats={'last_write_sample_id': 5, 'last_write_success': False,
                                                'last_persistence_error': 'error' * 100})
            publisher.offer({'log_id': 9, 'end_datetime': '2026-10-20T12:00:00+00:00', 'dynamic': {},
                'timing': {'acquired_start_utc': '2026-10-20T11:59:59+00:00',
                           'acquired_start_monotonic': 99.5,
                           'acquisition_duration_ms': 25, 'schedule_lag_ms': 3,
                           'effective_interval_ms': 1000, 'skipped_intervals': 0, 'run_id': 'run-test'}})
            for moment in (100, 100.5, 101, 102):
                now[0] = moment
                publisher.heartbeat(supervisor)
            self.assertTrue(publisher.close())
            snapshot = json.loads((Path(directory)/'status.json').read_text())
            self.assertEqual(len(snapshot['pipeline_history']), 2)
            point = snapshot['pipeline_history'][-1]
            self.assertEqual(point['observed_monotonic'], 102)
            self.assertEqual(point['last_write_sample_id'], 5)
            self.assertNotIn('sample_id', point)
            self.assertEqual(len(point['last_persistence_error']), 256)
            self.assertTrue(point['last_persistence_error_truncated'])
            self.assertEqual(snapshot['telemetry']['last_persistence_error'], 'error' * 100)
            self.assertEqual(point['time'], datetime.fromtimestamp(102, timezone.utc).isoformat())
            self.assertEqual(snapshot['history'][-1]['acquisition_duration_ms'], 25)
            self.assertEqual(snapshot['history'][-1]['monotonic'], 99.5)
            self.assertEqual(snapshot['history'][-1]['time'], '2026-10-20T11:59:59+00:00')

    def test_resources_use_the_existing_worker_and_never_refresh_heartbeat(self):
        now = [100.0]
        calls, observed = [], threading.Event()
        class Sampler:
            boot_id = 'test-boot'
            def sample(self, targets):
                calls.append((now[0], threading.get_ident(), targets))
                observed.set()
                return {'sampled_monotonic': now[0], 'supervisor': {'cpu_percent': None}}
        with tempfile.TemporaryDirectory() as directory:
            publisher = StatusPublisher(Path(directory)/'status.json', clock=lambda: now[0],
                process_sampler=Sampler(), resource_targets=lambda: {'supervisor': 123},
                utcnow=lambda: datetime.fromtimestamp(now[0], timezone.utc))
            self.assertTrue(observed.wait(1))
            publisher.heartbeat(self.supervisor())
            for index in range(20):
                publisher.offer({'log_id': index, 'end_datetime': 'sample-time', 'dynamic': {}, 'timing': {}})
            observed.clear()
            now[0] = 106
            with publisher._condition:
                publisher._condition.notify()
            self.assertTrue(observed.wait(1))
            self.assertTrue(publisher.close())
            snapshot = json.loads((Path(directory)/'status.json').read_text())
            self.assertEqual([call[0] for call in calls], [100, 106])
            self.assertTrue(all(call[1] == publisher._worker.ident for call in calls))
            self.assertNotEqual(calls[0][1], threading.get_ident())
            self.assertEqual(snapshot['heartbeat_monotonic'], 100)
            self.assertEqual(snapshot['published_monotonic'], 106)
            self.assertEqual(snapshot['boot_id'], 'test-boot')

    def test_new_device_generation_and_disconnection_retire_old_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = StatusPublisher(Path(directory)/'status.json')
            supervisor = self.supervisor()
            publisher.heartbeat(supervisor)
            sample = {'log_id': 1, 'device': supervisor.device.path,
                      'device_identity': supervisor.device.identity, 'device_generation': 1,
                      'end_datetime': 'sample-time', 'dynamic': {}, 'timing': {'run_id': 'run-test'}}
            publisher.offer(sample)
            self.assertIs(publisher.sample, sample)
            supervisor.backend.generation = 2
            publisher.heartbeat(supervisor)
            self.assertIsNone(publisher.sample)
            self.assertFalse(publisher.history)
            publisher.offer(dict(sample, device_generation=2))
            publisher.offer(sample)  # A late snapshot from generation 1 is obsolete.
            self.assertEqual(publisher.sample['device_generation'], 2)
            supervisor.state.name = 'BACKOFF'
            publisher.heartbeat(supervisor)
            self.assertIsNone(publisher.sample)
            self.assertTrue(publisher.close())
            self.assertIsNone(json.loads((Path(directory)/'status.json').read_text())['radio'])


class WriterObservationTests(unittest.TestCase):
    def test_last_write_attempt_is_the_writer_result_not_a_newer_dropped_sample(self):
        entered, release = threading.Event(), threading.Event()
        class ControlledWriter(Telemetry):
            def persist(self, payload):
                if payload['log_id'] == 1:
                    entered.set()
                    release.wait(2)
                return payload['log_id'] == 1
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings().telemetry, csv=True, json=False, directory=directory, queue_size=1)
            telemetry = ControlledWriter(settings, threading.Event())
            backend = SimpleNamespace(generation=4, metrics=lambda: ({}, {}))
            device = Device('/dev/test', 'wwan0', '/sys/test', 'test', 'test modem')
            try:
                first = telemetry.sample(backend, device)
                self.assertEqual(first['device_generation'], 4)
                self.assertTrue(entered.wait(1))
                telemetry.sample(backend, device)
                telemetry.sample(backend, device)  # Full queue: this is not a write attempt.
                self.assertEqual(telemetry.stats()['persistence_dropped'], 1)
                self.assertIsNone(telemetry.stats()['last_write_sample_id'])
            finally:
                release.set()
                self.assertFalse(telemetry.close())  # The second write intentionally failed.
            stats = telemetry.stats()
            self.assertEqual(stats['last_write_sample_id'], 2)
            self.assertFalse(stats['last_write_success'])
            self.assertGreaterEqual(stats['last_write_duration_ms'], 0)
            self.assertGreaterEqual(stats['last_write_latency_ms'], stats['last_write_duration_ms'])
            self.assertEqual(stats['persisted'], 1)
            self.assertEqual(stats['persistence_failed'], 1)
