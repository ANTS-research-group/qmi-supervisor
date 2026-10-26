from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from modules.runtime.settings import Settings
from modules.runtime.supervisor import Supervisor
from modules.telemetry.metrics import ListDynamicMetrics
from modules.telemetry.collector import fields


class SchedulerPrecisionTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.telemetry = Mock()
        self.supervisor = Supervisor(Settings(), Mock(), self.telemetry, threading.Event(), clock=lambda: self.now)

    def test_thousand_samples_do_not_add_processing_time_to_interval(self):
        scheduled = []
        def acquire(*args, **kwargs):
            scheduled.append(kwargs['scheduled_monotonic'])
            self.now += .2
        self.telemetry.sample.side_effect = acquire
        for index in range(1000):
            self.now = self.supervisor.next_sample
            self.supervisor.sample()
        self.assertEqual(scheduled, list(range(1000)))
        self.assertEqual(self.supervisor.next_sample, 1000)
        self.assertEqual(self.supervisor.skipped_intervals, 0)

    def test_slow_modem_skips_ticks_without_replay_bursts(self):
        def acquire(*args, **kwargs):
            self.now += 1.8
        self.telemetry.sample.side_effect = acquire
        self.supervisor.sample()
        self.assertEqual(self.supervisor.next_sample, 2)
        self.assertEqual(self.supervisor.skipped_intervals, 1)
        self.supervisor.sample()
        self.assertEqual(self.telemetry.sample.call_count, 1)
        self.now = 2
        self.supervisor.sample()
        self.assertEqual(self.telemetry.sample.call_args.kwargs['skipped_intervals'], 1)
        self.assertEqual(self.supervisor.next_sample, 4)

    def test_late_wakeup_observes_latest_tick_and_counts_expired_ticks(self):
        self.now = 3.25
        self.supervisor.sample()
        self.assertEqual(self.telemetry.sample.call_args.kwargs['scheduled_monotonic'], 3)
        self.assertEqual(self.telemetry.sample.call_args.kwargs['skipped_intervals'], 3)
        self.assertEqual(self.supervisor.next_sample, 4)


try:
    from modules.services.client import Session
    from modules.services.nas_client import NASClient
    from modules.services.wds_client import WDSClient
    from modules.device.backend import QmiBackend
    from gi.repository import GLib, Qmi
except (ImportError, ValueError):
    Session = None


@unittest.skipIf(Session is None, 'Requires libqmi GI')
class BatchPrecisionTests(unittest.TestCase):
    def test_new_device_generation_discards_previous_status_and_report(self):
        settings = replace(Settings(), modem=replace(Settings().modem, mode='monitor'))
        backend = QmiBackend(settings, threading.Event())
        backend.session = Mock()
        backend._last_status = 'connected'
        backend._last_status_at = time.monotonic()
        backend.collection_report = {'old-device': True}
        backend.nas.unsupported.add('old-method')
        device = SimpleNamespace(path='/dev/cdc-wdm2', usb_path='/sys/synthetic', serial='synthetic', enumeration='7')
        with patch('modules.device.backend.discover', return_value=device), \
                patch.object(backend, 'enumeration', return_value='7'):
            backend.open(device)
        self.assertIsNone(backend._last_status)
        self.assertEqual(backend._last_status_at, 0)
        self.assertEqual(backend.collection_report, {})
        self.assertEqual(backend.nas.unsupported, set())
        backend.session.open.assert_called_once_with('/dev/cdc-wdm2')

    def client(self, session, delay):
        class Client:
            def get_signal_info(self, value, timeout, cancel, callback, data):
                source = GLib.timeout_source_new(delay)
                source.set_callback(lambda *unused: (callback(self, object()), False)[1])
                source.attach(session.context)
            def get_signal_info_finish(self, response):
                return SimpleNamespace(get_result=lambda: True)
        return Client()

    def test_concurrent_requests_finish_in_one_observation_window(self):
        session = Session(20, threading.Event())
        session.clients[Qmi.Service.NAS] = self.client(session, 40)
        started = time.monotonic()
        outputs, report = session.collect([('one', Qmi.Service.NAS, 'get_signal_info', None),
                                           ('two', Qmi.Service.NAS, 'get_signal_info', None)], budget=.5, request_timeout=1)
        self.assertEqual(set(outputs), {'one', 'two'})
        self.assertTrue(report['complete'])
        self.assertLess(time.monotonic() - started, .2)
        self.assertLess(abs(report['requests']['one']['sent_monotonic']-report['requests']['two']['sent_monotonic']), .02)

    def test_expired_replies_never_modify_a_published_sample(self):
        session = Session(20, threading.Event())
        session.clients[Qmi.Service.NAS] = self.client(session, 50)
        outputs, report = session.collect([('slow', Qmi.Service.NAS, 'get_signal_info', None)], budget=.01, request_timeout=1)
        self.assertEqual(outputs, {})
        self.assertEqual(report['requests']['slow']['status'], 'timeout')
        def idle(*args, cancellable, callback):
            source = GLib.timeout_source_new(80)
            source.set_callback(lambda *unused: (callback(None, 'done'), False)[1])
            source.attach(session.context)
        session.wait(idle, lambda result: result)
        self.assertEqual(outputs, {})
        self.assertEqual(report['requests']['slow']['status'], 'timeout')

    def test_unsupported_response_is_reported_without_losing_the_callback(self):
        session = Session(20, threading.Event())
        client = self.client(session, 5)
        error = GLib.Error.new_literal(Qmi.protocol_error_quark(), 'Unsupported', Qmi.ProtocolError.NOTSUPPORTED)
        client.get_signal_info_finish = Mock(return_value=SimpleNamespace(get_result=Mock(side_effect=error)))
        session.clients[Qmi.Service.NAS] = client
        outputs, report = session.collect([('optional', Qmi.Service.NAS, 'get_signal_info', None)],
                                          budget=.2, request_timeout=1)
        self.assertEqual(outputs, {})
        self.assertEqual(report['requests']['optional']['status'], 'error')
        self.assertTrue(report['requests']['optional']['unsupported'])
        self.assertFalse(report['complete'])

    def test_request_not_sent_has_no_fabricated_send_timestamp(self):
        stop = threading.Event()
        stop.set()
        session = Session(20, stop)
        outputs, report = session.collect([('unsent', Qmi.Service.NAS, 'get_signal_info', None)],
                                          budget=.1, request_timeout=1)
        self.assertEqual(outputs, {})
        record = report['requests']['unsent']
        self.assertEqual(record['status'], 'not_requested')
        self.assertIsNone(record['sent_at_utc'])
        self.assertIsNone(record['sent_monotonic'])
        self.assertIn('queued_at_utc', record)

    def test_nr_units_and_invalid_sentinel(self):
        signal = SimpleNamespace(get_5g_signal_strength=lambda: (-89, 123),
                                 get_5g_signal_strength_extended=lambda: -32768)
        values = NASClient.signal(signal)
        self.assertEqual(values['nas_value_5g_signal_strength_snr'], 12.3)
        self.assertEqual(values['nas_value_5g_signal_strength_rsrp'], -89)
        self.assertIsNone(values['nas_value_5g_signal_strength_extended'])
        cell = SimpleNamespace(get_nr5g_cell_information=lambda: ([0x99, 0xF9, 0x01], [0,0,1], 1,2,-123,-987,143))
        values = NASClient.cell(cell)
        self.assertEqual(values['nas_value_nr5g_cell_information_rsrq'], -12.3)
        self.assertEqual(values['nas_value_nr5g_cell_information_rsrp'], -98.7)
        self.assertEqual(values['nas_value_nr5g_cell_information_snr'], 14.3)

    def test_rx_chain_scaling_and_untuned_chain(self):
        output = SimpleNamespace(get_rx_chain_0_info=lambda: (True,-1001,-123,0,-987,0xFFFFFFFF),
                                 get_rx_chain_1_info=lambda: (False,-1001,-123,0,-987,3),
                                 get_rx_chain_2_info=lambda: (True,-900,-100,0,-950,100),
                                 get_rx_chain_3_info=lambda: (True,-850,-110,0,-900,200),
                                 get_tx_info=lambda: (False,123))
        result = NASClient.txrx(output, Qmi.NasRadioInterface(12))
        self.assertEqual(result['nas_value_rx_chain_0_info_rx_power'], -100.1)
        self.assertEqual(result['nas_value_rx_chain_0_info_rsrp'], -98.7)
        self.assertIsNone(result['nas_value_rx_chain_0_info_phase'])
        self.assertIsNone(result['nas_value_rx_chain_1_info_rsrp'])
        self.assertIsNone(result['nas_value_tx_power'])
        self.assertFalse(set(result)-set(fields(ListDynamicMetrics)),
                         'Every constructed Tx/Rx field must have a CSV column')

    def test_channel_rates_preserve_csv_schema_and_zero_rates(self):
        output = SimpleNamespace(get_channel_rates=lambda: (0, 100, 200, 300))
        result = WDSClient.rates(output)
        self.assertEqual(result, {
            'wds_value_channel_rates_channel_tx_rate_bps': 0,
            'wds_value_channel_rates_channel_rx_rate_bps': 100,
            'wds_value_channel_rates_max_channel_tx_rate_bps': 200,
            'wds_value_channel_rates_max_channel_rx_rate_bps': 300})
        self.assertFalse(set(result)-set(fields(ListDynamicMetrics)),
                         'Every channel rate must have a CSV column')
        session = Mock()
        session.request.side_effect = [SimpleNamespace(get_connection_status=lambda: Qmi.WdsConnectionStatus.CONNECTED),
                                       SimpleNamespace(), output]
        legacy = WDSClient(session).metrics()
        self.assertEqual({key: legacy[key] for key in result}, result)
        self.assertFalse(set(legacy)-set(fields(ListDynamicMetrics)))

    def test_signal_strength_request_has_required_typed_input_in_every_profile(self):
        nas = NASClient(Mock())
        for profile in ('fast', 'standard', 'full'):
            request = next(item for item in nas.specifications(profile) if item[2] == 'get_signal_strength')
            self.assertIsInstance(request[3], Qmi.MessageNasGetSignalStrengthInput)
            self.assertTrue(request[3].get_request_mask() & Qmi.NasSignalStrengthRequest.IO)

    def test_wds_unavailable_sentinels_are_not_exported_as_real_measurements(self):
        output = SimpleNamespace(get_tx_packets_error=lambda: 0xFFFFFFFF, get_rx_packets_ok=lambda: 0,
                                 get_tx_bytes_ok=lambda: 0xFFFFFFFF)
        values = WDSClient.statistics(output)
        self.assertIsNone(values['wds_value_tx_packets_error'])
        self.assertEqual(values['wds_value_rx_packets_ok'], 0)
        self.assertEqual(values['wds_value_tx_bytes_ok'], 0xFFFFFFFF)
        rates = WDSClient.rates(SimpleNamespace(get_channel_rates=lambda: (0xFFFFFFFF, 0xFFFFFFFF, 0, 0)))
        self.assertIsNone(rates['wds_value_channel_rates_channel_tx_rate_bps'])
        self.assertEqual(rates['wds_value_channel_rates_max_channel_tx_rate_bps'], 0)

    def test_lte_serving_and_neighbors_use_tenths_from_existing_response(self):
        serving = SimpleNamespace(physical_cell_id=12, rsrq=-123, rsrp=-987, rssi=-754, cell_selection_rx_level=10)
        neighbor = SimpleNamespace(physical_cell_id=18, rsrq=-101, rsrp=-32768, rssi=-800, cell_selection_rx_level=5)
        frequency = SimpleNamespace(eutra_absolute_rf_channel_number=1650, cell_reselection_priority=3,
                                    cell_selection_rx_level_low_threshold=4, cell_selection_rx_level_high_threshold=6,
                                    cell=[neighbor])
        output = SimpleNamespace(
            get_intrafrequency_lte_info_v2=lambda: (True, [0x13, 0xF0, 0x62], 21, 123456, 1300, 12, 7, 2, 3, 4,
                                                    [serving, neighbor]),
            get_interfrequency_lte_info=lambda: (True, [frequency]))
        values = NASClient.cell(output)
        intra = values['nas_value_lte_intrafrequency']
        self.assertEqual((intra['plmn'], intra['tracking_area_code'], intra['global_cell_id'],
                          intra['earfcn'], intra['serving_cell_id']), ('31026', 21, 123456, 1300, 12))
        self.assertEqual(intra['cells'][0], dict(physical_cell_id=12, rsrq=-12.3, rsrp=-98.7, rssi=-75.4,
                                               cell_selection_rx_level_raw=10))
        self.assertIsNone(intra['cells'][1]['rsrp'])
        inter = values['nas_value_lte_interfrequency']['frequencies'][0]
        self.assertEqual(inter['earfcn'], 1650)
        self.assertEqual(inter['cells'][0]['rsrq'], -10.1)
        self.assertEqual(inter['cells'][0]['rssi'], -80)
        self.assertFalse(set(values)-set(fields(ListDynamicMetrics)))

    def test_connected_lte_leaves_idle_only_parameters_null(self):
        cell = SimpleNamespace(physical_cell_id=1, rsrq=-120, rsrp=-900, rssi=-700, cell_selection_rx_level=11)
        output = SimpleNamespace(get_intrafrequency_lte_info_v2=lambda:
            (False, [0x13, 0x00, 0x62], 21, 123456, 1300, 1, 7, 2, 3, 4, [cell]))
        intra = NASClient.cell(output)['nas_value_lte_intrafrequency']
        self.assertEqual(intra['plmn'], '310260')
        self.assertIsNone(intra['cell_reselection_priority'])
        self.assertIsNone(intra['s_intra_search_threshold_raw'])
        self.assertIsNone(intra['cells'][0]['cell_selection_rx_level_raw'])
        self.assertEqual(intra['cells'][0]['rsrp'], -90)
        self.assertIsNone(NASClient.plmn([0xFF, 0xFF, 0xFF]))
