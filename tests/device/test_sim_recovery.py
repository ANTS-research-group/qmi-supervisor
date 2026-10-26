"""Hot-swapped SIM recovery: power cycle first, rate-limited modem reset."""
import tempfile
import threading
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
