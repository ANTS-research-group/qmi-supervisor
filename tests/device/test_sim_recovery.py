"""Hot-swapped SIM recovery: power cycle first, rate-limited modem reset."""
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import Qmi
    from modules.device.backend import QmiBackend
    from modules.runtime.settings import Settings
    from modules.services.dms_client import DMSClient, SimNotReady
except (ImportError, ValueError):
    QmiBackend = None


@unittest.skipIf(QmiBackend is None, 'Requires OS libqmi GI bindings')
class SimRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backend = QmiBackend(Settings(), threading.Event(), self.root/'state.json')
        self.backend.device = SimpleNamespace(identity='test-modem')
        self.backend.dms = Mock()
        patcher = patch.object(QmiBackend, 'SIM_READY_TIMEOUT', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = patch('modules.device.backend.time.time', return_value=10_000.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        patcher = patch('modules.device.backend.time.sleep')
        patcher.start()
        self.addCleanup(patcher.stop)

    def missing(self):
        return SimNotReady('SIM absent or not ready; slot 1: error (voltage-mismatch)', [1])

    def test_card_status_error_reports_all_slots_for_recovery(self):
        session = Mock()
        cards = [SimpleNamespace(card_state=Qmi.UimCardState.ERROR, error_code=Qmi.UimCardError.VOLTAGE_MISMATCH)]
        session.request.side_effect = [SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.ONLINE),
                                       SimpleNamespace(get_card_status=lambda: (0, 0, 0, 0, cards))]
        with self.assertRaises(SimNotReady) as raised:
            DMSClient(session).prepare()
        self.assertEqual(raised.exception.slots, [1])

    def test_power_cycle_recovers_a_hot_swapped_card(self):
        self.backend.dms.prepare.side_effect = [self.missing(), self.missing(), None]
        with patch.object(QmiBackend, 'SIM_READY_TIMEOUT', 60):
            self.backend.prepare_sim()
        self.backend.dms.power_cycle_sim.assert_called_once_with(1)
        self.backend.dms.reset.assert_not_called()
        self.assertFalse((self.root/'state-sim.json').exists())

    def test_failed_power_cycle_escalates_to_one_reset_per_interval(self):
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.backend.dms.reset.assert_not_called()
        # A new worker process (fresh backend) must honour the saved history.
        backend = QmiBackend(Settings(), threading.Event(), self.root/'state.json')
        backend.device, backend.dms = self.backend.device, self.backend.dms
        with self.assertRaisesRegex(RuntimeError, 'modem reset requested'):
            backend.prepare_sim()
        backend.dms.reset.assert_called_once()
        with self.assertRaises(SimNotReady):
            backend.prepare_sim()
        backend.dms.reset.assert_called_once()
        self.assertEqual(backend.dms.power_cycle_sim.call_count, 1)
        # Once the power-cycle interval elapses, the card is retried before any new reset.
        with patch('modules.device.backend.time.time', return_value=10_000.0 + 121):
            with self.assertRaises(SimNotReady):
                backend.prepare_sim()
        self.assertEqual(backend.dms.power_cycle_sim.call_count, 2)
        backend.dms.reset.assert_called_once()

    def test_ladder_counters_are_saved_and_survive_a_reload(self):
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady) as raised:
            self.backend.prepare_sim()
        self.assertTrue(raised.exception.diagnosis['params']['ladder_tried'])
        record = json.loads((self.root/'state-sim.json').read_text())
        self.assertEqual((record['power_cycles'], record['resets']), (1, 0))
        self.assertEqual(record['last_outcome'], 'power_cycle_no_card')
        self.assertEqual(record['since'], 10_000.0)
        # A new worker process loads the counters and honours the rate limits.
        backend = QmiBackend(Settings(), threading.Event(), self.root/'state.json')
        backend.load_sim_recovery()
        self.assertEqual(backend.sim_recovery['power_cycles'], 1)
        backend.device, backend.dms = self.backend.device, self.backend.dms
        with self.assertRaisesRegex(RuntimeError, 'modem reset requested') as reset:
            backend.prepare_sim()
        self.assertEqual(reset.exception.diagnosis['code'], 'sim.not_reported')
        self.assertEqual(reset.exception.diagnosis['params']['action'], 'modem_reset')
        self.assertTrue(reset.exception.diagnosis['params']['ladder_tried'])
        record = json.loads((self.root/'state-sim.json').read_text())
        self.assertEqual((record['power_cycles'], record['resets']), (1, 1))
        self.assertEqual(record['last_outcome'], 'reset_requested')
        self.assertEqual(backend.sim_recovery['resets'], 1)

    def test_an_old_saved_file_loads_with_zero_counters(self):
        (self.root/'state-sim.json').write_text(json.dumps(
            {'version': 1, 'identity': 'test-modem', 'power_cycled': 9000.0}))
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        record = json.loads((self.root/'state-sim.json').read_text())
        self.assertEqual((record['power_cycles'], record['resets']), (1, 0))
        self.assertEqual(record['since'], 10_000.0)

    def test_a_recent_power_cycle_and_reset_only_annotates_the_ladder(self):
        (self.root/'state-sim.json').write_text(json.dumps(
            {'version': 1, 'identity': 'test-modem', 'power_cycled': 9_990.0, 'reset': 9_900.0,
             'power_cycles': 2, 'resets': 1, 'since': 8_000.0}))
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady) as raised:
            self.backend.prepare_sim()
        self.assertTrue(raised.exception.diagnosis['params']['ladder_tried'])
        self.backend.dms.power_cycle_sim.assert_not_called()
        self.backend.dms.reset.assert_not_called()

    def test_success_clears_the_saved_ladder_and_the_published_view(self):
        self.backend.dms.prepare.side_effect = [self.missing(), self.missing(), None]
        with patch.object(QmiBackend, 'SIM_READY_TIMEOUT', 60):
            self.backend.prepare_sim()
        self.assertFalse((self.root/'state-sim.json').exists())
        self.assertEqual(self.backend.sim_recovery, {})

    def test_the_published_recovery_view_matches_the_saved_ladder(self):
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        view = self.backend.sim_recovery_view()
        self.assertEqual(view['power_cycles'], 1)
        self.assertEqual(view['resets'], 0)
        self.assertTrue(view['since_utc'].endswith('+00:00'))
        self.assertTrue(view['last_power_cycle_utc'].endswith('+00:00'))
        self.assertTrue(view['next_power_cycle_not_before_utc'].endswith('+00:00'))
        self.assertIsNone(view['last_reset_utc'])
        self.assertEqual(view['last_outcome'], 'power_cycle_no_card')

    def test_a_reset_that_reboots_the_modem_counts_as_requested(self):
        from gi.repository import GLib
        hangup = lambda: GLib.Error.new_literal(Qmi.core_error_quark(), 'endpoint hangup',
                                                Qmi.CoreError.FAILED)
        session = Mock()
        session.request.side_effect = [SimpleNamespace(), hangup()]
        DMSClient(session).reset()
        session = Mock()
        session.request.side_effect = [hangup()]
        with self.assertRaises(GLib.Error):
            DMSClient(session).reset()

    @staticmethod
    def slot_output(active_card=Qmi.UimPhysicalCardState.PRESENT,
                    inactive_card=Qmi.UimPhysicalCardState.ABSENT):
        status = [SimpleNamespace(physical_slot_status=Qmi.UimSlotState.INACTIVE,
                                  physical_card_status=inactive_card, logical_slot=1),
                  SimpleNamespace(physical_slot_status=Qmi.UimSlotState.ACTIVE,
                                  physical_card_status=active_card, logical_slot=1)]
        information = [SimpleNamespace(card_protocol=Qmi.UimCardProtocol.UNKNOWN,
                                       atr_value=[], is_euicc=False),
                       SimpleNamespace(card_protocol=Qmi.UimCardProtocol.UICC,
                                       atr_value=[0x3B], is_euicc=False)]
        return SimpleNamespace(get_physical_slot_status=lambda: status,
                               get_physical_slot_information=lambda: information)

    def recent_reset(self):
        """A saved ladder that already reset recently: failures stay SimNotReady."""
        (self.root/'state-sim.json').write_text(json.dumps(
            {'version': 1, 'identity': 'test-modem', 'reset': 9_999.0, 'resets': 1}))

    def count_slot_requests(self):
        return sum(call.args[1] == 'get_slot_status'
                   for call in self.backend.session.request.call_args_list)

    def test_slot_status_is_read_once_per_minute_while_the_card_fails(self):
        self.recent_reset()
        self.backend.session.request = Mock(return_value=self.slot_output())
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady) as raised:
            self.backend.prepare_sim()
        self.assertEqual(self.count_slot_requests(), 1)
        self.assertIn('logical slot 1 is physical slot 2 (active)', str(raised.exception))
        self.assertIn('physical slot 1 inactive, card absent', str(raised.exception))
        self.assertEqual(raised.exception.diagnosis['params']['physical_slot'], 2)
        self.assertEqual(len(raised.exception.diagnosis['params']['slots']), 2)
        # A second failure inside the minute reuses the cached mapping.
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.assertEqual(self.count_slot_requests(), 1)
        self.backend.next_slot_status = 0
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.assertEqual(self.count_slot_requests(), 2)

    def test_an_unsupported_slot_status_is_not_asked_again_until_reopen(self):
        from gi.repository import GLib
        unsupported = GLib.Error.new_literal(Qmi.protocol_error_quark(), 'not supported',
                                             Qmi.ProtocolError.NOTSUPPORTED)
        self.recent_reset()
        self.backend.session.request = Mock(side_effect=unsupported)
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.assertTrue(self.backend.sim_slots_unsupported)
        self.backend.next_slot_status = 0
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.assertEqual(self.count_slot_requests(), 1)
        self.backend.session.open = Mock()
        self.backend.present = Mock(return_value=True)
        device = SimpleNamespace(path='/dev/cdc-wdm2', usb_path='/sys/synthetic',
                                 serial='synthetic', enumeration='7')
        with patch('modules.device.backend.discover', return_value=device), \
                patch.object(self.backend, 'enumeration', return_value='7'):
            self.backend.open(device)
        self.assertFalse(self.backend.sim_slots_unsupported)
        self.assertIsNone(self.backend.sim_slots)

    def test_slot_status_is_bounded_by_a_three_second_deadline_and_restored(self):
        seen = {}

        def request(service, name, value=None):
            if name == 'get_slot_status':
                seen['deadline'] = self.backend.session.collection_deadline
                raise TimeoutError('no answer')
            return SimpleNamespace()
        self.recent_reset()
        self.backend.session.request = Mock(side_effect=request)
        self.backend.session.collection_deadline = None
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.assertAlmostEqual(seen['deadline'] - time.monotonic(), 3, delta=1)
        self.assertIsNone(self.backend.session.collection_deadline)

    def test_a_card_in_an_inactive_slot_is_named_only_when_unambiguous(self):
        self.recent_reset()
        self.backend.session.request = Mock(return_value=self.slot_output(
            active_card=Qmi.UimPhysicalCardState.ABSENT,
            inactive_card=Qmi.UimPhysicalCardState.PRESENT))
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady) as raised:
            self.backend.prepare_sim()
        self.assertEqual(raised.exception.diagnosis['code'], 'sim.in_inactive_slot')
        self.assertEqual(raised.exception.diagnosis['params']['occupied'], 1)
        self.assertEqual(raised.exception.diagnosis['params']['active'], 2)
        # The Fibocom reports a dead inserted card as absent in both slots:
        # that is never turned into "the card is in the wrong slot".
        self.backend.next_slot_status = 0
        self.backend.session.request = Mock(return_value=self.slot_output(
            active_card=Qmi.UimPhysicalCardState.ABSENT,
            inactive_card=Qmi.UimPhysicalCardState.ABSENT))
        with self.assertRaises(SimNotReady) as raised:
            self.backend.prepare_sim()
        self.assertNotEqual(raised.exception.diagnosis['code'], 'sim.in_inactive_slot')

    def test_history_of_another_modem_is_ignored(self):
        self.backend.dms.prepare.side_effect = self.missing()
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.backend.device = SimpleNamespace(identity='other-modem')
        with self.assertRaises(SimNotReady):
            self.backend.prepare_sim()
        self.assertEqual(self.backend.dms.power_cycle_sim.call_count, 2)
        self.backend.dms.reset.assert_not_called()


if __name__ == '__main__':
    unittest.main()
