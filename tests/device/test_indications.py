from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading
import unittest

from modules.runtime.settings import Settings

try:
    from gi.repository import GLib, Qmi
    from modules.device.backend import QmiBackend
    from modules.device.query_plan import GROUPS
    from modules.services.indications import Indications
except (ImportError, ValueError):
    Indications = None


class FakeClient:
    def __init__(self):
        self.handlers = {}
        self.disconnected = []

    def connect(self, name, callback):
        self.handlers[name] = callback
        return name

    def disconnect(self, handler):
        self.disconnected.append(handler)

    def emit(self, name, output):
        self.handlers[name](self, output)


def statistics(tx_bytes):
    return SimpleNamespace(get_tx_bytes_ok=lambda: tx_bytes, get_rx_bytes_ok=lambda: 7)


def status(connected=True):
    value = Qmi.WdsConnectionStatus.CONNECTED if connected else Qmi.WdsConnectionStatus.DISCONNECTED
    return SimpleNamespace(get_connection_status=lambda: (value, False))


@unittest.skipIf(Indications is None, 'Requires libqmi GI')
class IndicationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.wds, self.nas = FakeClient(), FakeClient()
        self.session = SimpleNamespace(clients={Qmi.Service.WDS: self.wds, Qmi.Service.NAS: self.nas},
                                       request=Mock())
        self.indications = Indications(self.session, clock=lambda: self.now)

    def test_registration_connects_handlers_before_requesting_reports_once_per_client(self):
        self.indications.ensure(1.0)
        self.indications.ensure(1.0)
        methods = [call.args[1] for call in self.session.request.call_args_list]
        self.assertEqual(methods, ['set_event_report', 'register_indications'])
        self.assertEqual(set(self.wds.handlers), {'event-report', 'packet-service-status'})
        self.assertEqual(set(self.nas.handlers), {'serving-system'})
        self.assertEqual(self.indications.status, dict.fromkeys(Indications.GROUPS, 'active'))

    def test_periodic_statistics_are_served_only_while_indications_keep_arriving(self):
        self.indications.ensure(1.0)
        self.wds.emit('event-report', statistics(1234))
        record = self.indications.fresh('wds_stats', 1.0)
        self.assertEqual(record['values']['wds_value_tx_bytes_ok'], 1234)
        self.now += 2.5
        self.assertIsNone(self.indications.fresh('wds_stats', 1.0))
        self.indications.observe('wds_stats', {'wds_value_tx_bytes_ok': 1})
        self.assertIsNone(self.indications.fresh('wds_stats', 1.0), 'A poll is not a pushed statistic')

    def test_on_change_status_is_confirmed_by_polling_at_least_every_minute(self):
        self.indications.ensure(1.0)
        self.wds.emit('packet-service-status', status())
        self.assertEqual(self.indications.fresh('wds_status', 1.0)['values'],
                         {'wds_value_connection_status': 'connected'})
        self.now += Indications.REFRESH - 1
        self.assertIsNotNone(self.indications.fresh('wds_status', 1.0))
        self.now += 2
        self.assertIsNone(self.indications.fresh('wds_status', 1.0))
        self.indications.observe('wds_status', {'wds_value_connection_status': 'connected'})
        self.assertEqual(self.indications.fresh('wds_status', 1.0)['source'], 'poll')

    def test_unsupported_registration_keeps_polling_those_groups(self):
        self.session.request.side_effect = [GLib.Error.new_literal(
            Qmi.protocol_error_quark(), 'Unsupported', Qmi.ProtocolError.NOTSUPPORTED), None]
        self.indications.ensure(1.0)
        self.wds.emit('event-report', statistics(1))
        self.assertEqual(self.indications.status['wds_stats'], 'unsupported')
        self.assertIsNone(self.indications.fresh('wds_stats', 1.0))
        self.assertEqual(self.indications.status['serving'], 'active')

    def test_a_replaced_client_is_registered_again_without_old_observations(self):
        self.indications.ensure(1.0)
        self.wds.emit('packet-service-status', status())
        replacement = FakeClient()
        self.session.clients[Qmi.Service.WDS] = replacement
        self.indications.ensure(1.0)
        self.assertIsNone(self.indications.fresh('wds_status', 1.0))
        self.assertIn('packet-service-status', replacement.handlers)
        self.assertEqual(self.session.request.call_count, 3)

    def test_close_stops_periodic_statistics_and_disconnects_handlers(self):
        self.indications.ensure(1.0)
        self.session.request.reset_mock()
        self.indications.close()
        value = self.session.request.call_args.args[2]
        self.assertEqual(self.session.request.call_args.args[1], 'set_event_report')
        self.assertEqual(value.get_transfer_statistics()[0], 0)
        self.assertEqual(set(self.wds.disconnected), {'event-report', 'packet-service-status'})
        self.assertEqual(self.indications.status, {})

    def test_unparseable_indication_is_reported_without_replacing_the_last_value(self):
        self.indications.ensure(1.0)
        self.wds.emit('packet-service-status', status())
        self.wds.emit('packet-service-status', SimpleNamespace())
        self.assertIn('Unparseable', self.indications.errors['wds_status'])
        self.assertIsNotNone(self.indications.fresh('wds_status', 1.0))


@unittest.skipIf(Indications is None, 'Requires libqmi GI')
class BackendIndicationTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.batches = []
        self.backend = QmiBackend(self.settings('on'), threading.Event())
        self.wds, self.nas = FakeClient(), FakeClient()
        self.backend.session = Mock(clients={Qmi.Service.WDS: self.wds, Qmi.Service.NAS: self.nas},
                                    collection_deadline=None)
        self.backend.session.collect.side_effect = self.collect
        self.backend.indications.session = self.backend.session
        self.backend.indications.clock = lambda: self.now
        self.backend.dms.specifications = Mock(return_value=[])
        self.backend.dms.sim_specifications = Mock(return_value=[])
        self.backend.observation_state = 'ONLINE'
        clock = patch('modules.device.backend.time.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    @staticmethod
    def settings(mode):
        return replace(Settings(), telemetry=replace(Settings().telemetry, profile='custom', indications=mode))

    def collect(self, requests, *, budget, request_timeout):
        if not requests:
            return {}, {'requests': {}, 'complete': True}
        self.batches.append({item[0] for item in requests})
        outputs = {'wds.statistics': statistics(55), 'wds.status': status()}
        records = {key: {'status': 'ok', 'received_at_utc': str(self.now)} for key, *_ in requests}
        return {key: outputs[key] for key, *_ in requests if key in outputs}, {'requests': records, 'complete': True}

    def test_pushed_groups_are_not_requested_and_keep_their_receipt_time(self):
        self.backend.metrics()
        self.wds.emit('event-report', statistics(1234))
        self.wds.emit('packet-service-status', status())
        self.now = 1
        _, dynamic = self.backend.metrics()
        self.assertNotIn(GROUPS['wds_stats'], self.batches[-1])
        self.assertNotIn(GROUPS['wds_status'], self.batches[-1])
        self.assertIn(GROUPS['signal'], self.batches[-1])
        self.assertEqual(dynamic['wds_value_tx_bytes_ok'], 1234)
        self.assertEqual(self.backend._last_status, 'connected')
        groups = self.backend.collection_report['indications']['groups']
        self.assertTrue(groups['wds_stats']['served'])
        self.assertEqual(groups['wds_stats']['age_ms'], 1000)

    def test_stale_statistics_fall_back_to_polling_in_that_sample(self):
        self.backend.metrics()
        self.wds.emit('event-report', statistics(1234))
        self.now = 3
        _, dynamic = self.backend.metrics()
        self.assertIn(GROUPS['wds_stats'], self.batches[-1])
        self.assertEqual(dynamic['wds_value_tx_bytes_ok'], 55)

    def test_registration_never_uses_indications(self):
        self.backend.observation_state = 'WAIT_REGISTRATION'
        self.backend.metrics()
        self.assertNotIn('indications', self.backend.collection_report)
        self.backend.session.request.assert_not_called()

    def test_shadow_polls_every_group_and_records_differences(self):
        self.backend.settings = self.settings('shadow')
        self.backend.metrics()
        self.wds.emit('event-report', statistics(1234))
        self.now = 1
        _, dynamic = self.backend.metrics()
        self.assertIn(GROUPS['wds_stats'], self.batches[-1])
        self.assertEqual(dynamic['wds_value_tx_bytes_ok'], 55)
        shadow = self.backend.collection_report['indications']['groups']['wds_stats']['shadow']
        self.assertIn('wds_value_tx_bytes_ok', shadow['differences'])
        self.assertNotIn('wds_value_rx_bytes_ok', shadow['differences'])


if __name__ == '__main__':
    unittest.main()
