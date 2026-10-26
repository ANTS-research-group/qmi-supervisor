"""APN acceptance/ownership tests; no modem, network or persistent profiles."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

try:
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import GLib, Qmi
    from modules.services.wds_client import WDSClient
    from modules.device.backend import QmiBackend
    from modules.runtime.settings import Settings
except (ImportError, ValueError):
    WDSClient = None


@unittest.skipIf(WDSClient is None, 'Requires OS libqmi GI bindings')
class ApnSessionTests(unittest.TestCase):
    def session(self, reported='internet', *, stop_error=None, settings_error=None):
        session = Mock()
        def request(service, method, value=None):
            if method == 'start_network':
                self.assertEqual(value.get_apn(), 'internet')
                self.assertFalse(value.get_enable_autoconnect())
                return SimpleNamespace(get_packet_data_handle=lambda: 42)
            if method == 'get_current_settings':
                if settings_error:
                    raise settings_error
                return SimpleNamespace(get_apn_name=lambda: reported, get_ipv4_address=lambda: 0xC0A80302)
            if method == 'stop_network' and stop_error:
                raise stop_error
            return SimpleNamespace()
        session.request.side_effect = request
        return session

    def test_new_owned_handle_does_not_prove_requested_apn(self):
        session = self.session('tenten')
        wds = WDSClient(session)
        checkpoint = Mock()
        with self.assertRaisesRegex(RuntimeError, "Modem reports APN 'tenten', requested 'internet'"):
            wds.start('internet', on_started=checkpoint)
        checkpoint.assert_called_once()
        self.assertEqual(wds.handle, 42, 'Owned handle must remain available for cleanup')
        self.assertEqual(wds.apn_verification, 'mismatch')
        self.assertEqual(wds.apn_reported_source, 'current_settings')
        wds.stop()
        self.assertEqual(session.request.call_args.args[2].get_packet_data_handle(), 42)
        self.assertIsNone(wds.handle)

    def test_matching_effective_apn_is_verified_on_same_client(self):
        wds = WDSClient(self.session('INTERNET'))
        wds.start('internet')
        self.assertEqual(wds.apn_reported, 'INTERNET')
        self.assertEqual(wds.apn_verification, 'matched')
        self.assertFalse(wds.reused)
        self.assertEqual([call.args[1] for call in wds.session.request.call_args_list],
                         ['set_ip_family', 'start_network', 'get_current_settings'])

    def test_missing_owned_apn_is_explicitly_unverified_and_warned_once(self):
        wds = WDSClient(self.session(None))
        with self.assertLogs('modules.services.wds_client', level='WARNING') as logs:
            wds.start('internet')
            wds.verify_apn(wds.settings())
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(wds.apn_verification, 'unavailable')
        self.assertIsNone(wds.apn_reported)
        self.assertIsNone(wds.apn_reported_source)
        self.assertEqual(wds.handle, 42)

    def test_read_failure_preserves_owned_handle_for_cleanup(self):
        wds = WDSClient(self.session(settings_error=TimeoutError('read timed out')))
        with self.assertRaises(TimeoutError):
            wds.start('internet')
        self.assertEqual(wds.handle, 42)
        wds.stop()
        self.assertIsNone(wds.handle)

    def test_uncertain_stop_retains_handle_and_prevents_another_start(self):
        wds = WDSClient(self.session(stop_error=TimeoutError('stop timed out')))
        wds.start('internet')
        with self.assertRaises(TimeoutError):
            wds.stop()
        self.assertEqual(wds.handle, 42)
        with self.assertRaisesRegex(RuntimeError, 'not been confirmed stopped'):
            wds.start('another.apn')
        starts = [call for call in wds.session.request.call_args_list if call.args[1] == 'start_network']
        self.assertEqual(len(starts), 1)

    def test_unambiguously_gone_owned_handle_can_be_retired(self):
        for code in (Qmi.ProtocolError.NOEFFECT, Qmi.ProtocolError.INVALIDHANDLE, Qmi.ProtocolError.OUTOFCALL):
            with self.subTest(code=code):
                error = GLib.Error.new_literal(Qmi.protocol_error_quark(), 'Gone', code)
                wds = WDSClient(self.session(stop_error=error))
                wds.start('internet')
                wds.stop()
                self.assertIsNone(wds.handle)

    def test_mismatch_prevents_host_configuration_and_online(self):
        backend = QmiBackend(Settings(), threading.Event())
        backend.network = Mock()
        backend.wds = WDSClient(self.session('tenten'))
        backend.wds.apn_requested = 'internet'
        with self.assertRaisesRegex(RuntimeError, 'Modem reports APN'):
            backend.configure_ip()
        backend.network.configure.assert_not_called()

    def test_apn_change_stops_previous_handle_before_new_request(self):
        events = []
        active = [None, 40]
        session = Mock()
        def request(service, method, value=None):
            if method == 'start_network':
                active[0], active[1] = value.get_apn(), active[1]+1
                events.append(('start', active[0], active[1]))
                return SimpleNamespace(get_packet_data_handle=lambda: active[1])
            if method == 'get_current_settings':
                return SimpleNamespace(get_apn_name=lambda: active[0])
            if method == 'stop_network':
                events.append(('stop', value.get_packet_data_handle()))
            return SimpleNamespace()
        session.request.side_effect = request
        wds = WDSClient(session)
        wds.start('tenten')
        wds.stop()
        wds.start('internet')
        self.assertEqual(events, [('start', 'tenten', 41), ('stop', 41), ('start', 'internet', 42)])
        self.assertEqual(wds.apn_reported, 'internet')
        self.assertEqual(wds.apn_verification, 'matched')

    def test_reserved_global_handle_is_never_treated_as_owned(self):
        session = self.session()
        original = session.request.side_effect
        session.request.side_effect = lambda service, name, value=None: (
            SimpleNamespace(get_packet_data_handle=lambda: 0xFFFFFFFF) if name == 'start_network'
            else original(service, name, value))
        wds = WDSClient(session)
        with self.assertRaisesRegex(RuntimeError, 'refusing a global stop handle'):
            wds.start('internet')
        self.assertIsNone(wds.handle)
        wds.stop()
        self.assertNotIn('stop_network', [call.args[1] for call in session.request.call_args_list])
