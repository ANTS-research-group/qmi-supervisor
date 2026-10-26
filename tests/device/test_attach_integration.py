"""Initial APN radio sequencing, isolated from any physical modem."""
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import GLib, Qmi
    from modules.device.backend import QmiBackend
    from modules.runtime.settings import Settings
    from modules.runtime.state import RuntimeState
    from modules.device.attach_apn import AttachApn
except (ImportError, ValueError):
    QmiBackend = None


@unittest.skipIf(QmiBackend is None, 'Requires OS libqmi GI bindings')
class AttachIntegrationTests(unittest.TestCase):
    def test_attach_completion_rejects_a_changed_open_transport_and_retains_its_journal(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root/'devnum').write_text('7')
        backend = QmiBackend(Settings(), threading.Event(), root/'state.json')
        backend.device = SimpleNamespace(identity='test-modem', usb_path=str(root))
        backend._opened_enumeration = '7'
        attach = AttachApn(Mock(), backend.device, root/'attach.json', enumeration=backend.session_enumeration)
        RuntimeState(attach.state.path).save(dict(kind='attach_apn', identity='test-modem', enumeration='7',
            token='transaction', requested_apn='internet', profile_id=1))
        attach._completion_token = 'transaction'
        before = attach.state.path.read_bytes()
        (root/'devnum').write_text('8')
        with self.assertRaisesRegex(RuntimeError, 'USB enumeration changed'):
            attach.mark_completed()
        self.assertEqual(attach.state.path.read_bytes(), before)

    def test_prepare_does_not_change_host_or_modem_after_open_transport_changes(self):
        root = self.backend.state.path.parent
        (root/'devnum').write_text('8')
        self.backend.device = SimpleNamespace(interface='wwan0', usb_path=str(root), identity='test-modem')
        self.backend._opened_enumeration = '7'
        self.backend.network, self.backend.dms = Mock(), Mock()
        with self.assertRaisesRegex(RuntimeError, 'USB enumeration changed'):
            self.backend.prepare()
        self.backend.network.attach.assert_not_called()
        self.backend.dms.prepare.assert_not_called()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.backend = QmiBackend(Settings(), threading.Event(), Path(temporary.name)/'state.json')
        self.backend.session = Mock(cleaning=False)
        self.backend.attach_apn = Mock()
        self.backend.nas = Mock()
        self.events = []
        self.mode = Qmi.DmsOperatingMode.ONLINE
        def request(service, method, value=None):
            self.assertEqual(service, Qmi.Service.DMS)
            if method == 'set_operating_mode':
                self.mode = value.get_mode()
                self.events.append(self.mode)
                if self.mode == Qmi.DmsOperatingMode.ONLINE:
                    self.assertTrue(self.backend.session.cleaning, 'ONLINE restoration must survive SIGTERM')
            return SimpleNamespace(get_mode=lambda: self.mode)
        self.backend.session.request.side_effect = request

    def transaction(self, *, failure=None, interrupt=False):
        def ensure(apn, *, mode, before_change):
            self.events.append('read-and-checkpoint')
            before_change()
            self.events.append('modify-and-readback')
            if interrupt:
                self.backend.stop.set()
            if failure:
                raise failure
            return {'status': 'updated', 'needs_reregister': True, 'profile_id': 1, 'profile_apn': apn}
        self.backend.attach_apn.ensure.side_effect = ensure

    def test_low_modify_online_then_wait_for_registration_before_committing(self):
        self.transaction()
        self.backend._last_status = 'connected'
        self.backend._last_status_at = 42
        self.backend.prepare_attach_apn()
        self.assertEqual(self.events, ['read-and-checkpoint', Qmi.DmsOperatingMode.LOW_POWER,
                                      'modify-and-readback', Qmi.DmsOperatingMode.ONLINE])
        self.assertFalse(self.backend.session.cleaning)
        self.assertIsNone(self.backend._last_status)
        self.assertEqual(self.backend._last_status_at, 0)
        self.backend.attach_apn.mark_completed.assert_not_called()
        self.backend.nas.registered.return_value = False
        self.assertFalse(self.backend.registered())
        self.backend.attach_apn.mark_completed.assert_not_called()
        self.backend.nas.registered.return_value = True
        self.assertTrue(self.backend.registered())
        self.backend.attach_apn.mark_completed.assert_called_once()
        self.assertFalse(self.backend.attach_report['needs_reregister'])

    def test_matching_profile_without_pending_change_does_not_cycle_radio(self):
        self.backend.attach_apn.ensure.return_value = {'status': 'matched', 'needs_reregister': False}
        self.backend.prepare_attach_apn()
        self.backend.session.request.assert_not_called()

    def test_changed_identity_does_not_complete_old_registration(self):
        self.backend.attach_report = {'needs_reregister': True}
        self.backend.nas.registered.return_value = True
        self.backend.attach_apn.mark_completed.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'checkpoint could not be completed'):
            self.backend.registered()
        self.assertTrue(self.backend.attach_report['needs_reregister'])

    def test_write_failure_still_restores_online_without_committing(self):
        self.transaction(failure=RuntimeError('Profile write failed'))
        with self.assertRaisesRegex(RuntimeError, 'Profile write failed'):
            self.backend.prepare_attach_apn()
        self.assertEqual(self.events[-1], Qmi.DmsOperatingMode.ONLINE)
        self.backend.attach_apn.mark_completed.assert_not_called()
        self.assertFalse(self.backend.session.cleaning)

    def test_low_power_timeout_still_attempts_online(self):
        self.transaction()
        original = self.backend.session.request.side_effect
        def request(service, method, value=None):
            output = original(service, method, value)
            if method == 'set_operating_mode' and value.get_mode() == Qmi.DmsOperatingMode.LOW_POWER:
                raise TimeoutError('LOW_POWER response lost')
            return output
        self.backend.session.request.side_effect = request
        with self.assertRaisesRegex(TimeoutError, 'response lost'):
            self.backend.prepare_attach_apn()
        self.assertEqual(self.events[-1], Qmi.DmsOperatingMode.ONLINE)
        self.assertNotIn('modify-and-readback', self.events)
        self.backend.attach_apn.mark_completed.assert_not_called()

    def test_stop_after_modify_does_not_prevent_online_restoration(self):
        self.transaction(interrupt=True)
        self.backend.prepare_attach_apn()
        self.assertTrue(self.backend.stop.is_set())
        self.assertEqual(self.events[-1], Qmi.DmsOperatingMode.ONLINE)
        self.assertFalse(self.backend.session.cleaning)
        self.backend.attach_apn.mark_completed.assert_not_called()

    def test_online_restore_failure_leaves_transaction_uncommitted(self):
        self.transaction()
        original = self.backend.session.request.side_effect
        def request(service, method, value=None):
            if method == 'set_operating_mode' and value.get_mode() == Qmi.DmsOperatingMode.ONLINE:
                raise TimeoutError('ONLINE response lost')
            return original(service, method, value)
        self.backend.session.request.side_effect = request
        with self.assertRaisesRegex(TimeoutError, 'ONLINE response lost'):
            self.backend.prepare_attach_apn()
        self.assertFalse(self.backend.session.cleaning)
        self.backend.attach_apn.mark_completed.assert_not_called()

    def test_operating_mode_ack_requires_observation_of_transition(self):
        calls = 0
        def request(service, method, value=None):
            nonlocal calls
            if method == 'get_operating_mode':
                calls += 1
            return SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.ONLINE if calls == 1
                                   else Qmi.DmsOperatingMode.LOW_POWER)
        self.backend.session.request.side_effect = request
        with patch('modules.device.backend.time.sleep'):
            self.backend.set_radio_mode(Qmi.DmsOperatingMode.LOW_POWER)
        self.assertEqual(calls, 2)

    def test_monitor_preparation_does_not_create_attach_manager(self):
        self.backend.settings = replace(self.backend.settings, modem=replace(self.backend.settings.modem, mode='monitor'))
        self.backend.device = SimpleNamespace(interface='wwan0')
        self.backend.network = Mock()
        self.backend.dms = Mock()
        output = SimpleNamespace(get_link_layer_protocol=lambda: Qmi.WdaLinkLayerProtocol(1))
        self.backend.session.request.side_effect = None
        self.backend.session.request.return_value = output
        with patch('modules.device.backend.AttachApn') as attach, patch('modules.device.backend.Path.exists', return_value=False):
            self.backend.prepare()
        attach.assert_not_called()

    def test_connect_preparation_passes_fresh_enumeration_and_ieee_framing(self):
        root = self.backend.state.path.parent
        (root/'devnum').write_text('7')
        self.backend.device = SimpleNamespace(interface='wwan0', usb_path=str(root), identity='test-modem')
        self.backend.network = Mock()
        self.backend.dms = Mock()
        self.backend.session.request.side_effect = None
        self.backend.session.request.return_value = SimpleNamespace(get_link_layer_protocol=lambda: Qmi.WdaLinkLayerProtocol(1))
        with patch('modules.device.backend.AttachApn') as attach, patch('modules.device.backend.Path.exists', return_value=False):
            attach.return_value.ensure.return_value = {'status': 'matched', 'needs_reregister': False}
            self.backend.prepare()
        self.assertEqual(attach.call_args.kwargs['state_path'], root/'state-attach.json')
        read_enumeration = attach.call_args.kwargs['enumeration']
        self.assertEqual(read_enumeration(), '7')
        (root/'devnum').write_text('8')
        self.assertEqual(read_enumeration(), '8')
        self.backend.network.prepare.assert_called_once_with(False)
        data_format = self.backend.session.request.call_args.args[2]
        self.assertEqual(data_format.get_link_layer_protocol(), Qmi.WdaLinkLayerProtocol(1))
