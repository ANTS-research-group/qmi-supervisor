import threading
import time
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import Mock, patch

try:
    from modules.services.client import Session, optional
    from modules.services.wds_client import WDSClient
    from modules.services.nas_client import NASClient
    from modules.services.dms_client import DMSClient
    from gi.repository import GLib, Qmi
except (ImportError, ValueError):
    Session = None


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class QmiTests(unittest.TestCase):
    def test_requested_settings_flag_type_accepts_both_libqmi_names(self):
        from modules.services import wds_client
        new, old = object(), object()
        with patch.object(wds_client, 'Qmi', SimpleNamespace(WdsRequestedSettings=new)):
            self.assertIs(wds_client.requested_settings_mask(), new)
        with patch.object(wds_client, 'Qmi', SimpleNamespace(WdsGetCurrentSettingsRequestedSettings=old)):
            self.assertIs(wds_client.requested_settings_mask(), old)

    def test_uncertain_bearer_retains_only_its_firmware_cid(self):
        session = Session(1, threading.Event())
        device, wds, nas = Mock(), Mock(), Mock()
        session.device = device
        session.clients = {Qmi.Service.WDS: wds, Qmi.Service.NAS: nas}
        session.wait = Mock()
        session.close(preserve_cids=(Qmi.Service.WDS,))
        calls = session.wait.call_args_list
        self.assertEqual(calls[0].args[2:4], (wds, Qmi.DeviceReleaseClientFlags.NONE))
        self.assertEqual(calls[1].args[2:4], (nas, Qmi.DeviceReleaseClientFlags.RELEASE_CID))
        self.assertFalse(session.clients)
        self.assertIsNone(session.device)

    def test_closing_a_changed_transport_does_not_release_its_stale_client_ids(self):
        session = Session(1, threading.Event())
        device = Mock()
        client = Mock()
        session.device = device
        session.clients[Qmi.Service.WDS] = client
        session.wait = Mock()
        session.close(release_clients=False)
        self.assertEqual(session.wait.call_count, 2)
        self.assertEqual(session.wait.call_args_list[0].args,
            (device.release_client, device.release_client_finish, client, Qmi.DeviceReleaseClientFlags.NONE, 3))
        self.assertEqual(session.wait.call_args_list[-1].args, (device.close_async, device.close_finish, 3))
        self.assertFalse(session.clients)
        self.assertIsNone(session.device)

    def test_normal_close_still_releases_owned_client_ids_before_closing_transport(self):
        session = Session(1, threading.Event())
        device, client = Mock(), Mock()
        session.device = device
        session.clients[Qmi.Service.WDS] = client
        session.wait = Mock()
        session.close()
        first = session.wait.call_args_list[0].args
        self.assertEqual(first[:3], (device.release_client, device.release_client_finish, client))
        self.assertEqual(session.wait.call_count, 2)
    def test_bounded_call_success_and_stop(self):
        stop = threading.Event()
        session = Session(1, stop)
        def start(*args, cancellable, callback):
            source = GLib.idle_source_new()
            def ready(*unused):
                callback(None, 'response')
                return False
            source.set_callback(ready)
            source.attach(session.context)
        self.assertEqual(session.wait(start, lambda result: result), 'response')
        stop.set()
        with self.assertRaises(InterruptedError):
            session.wait(lambda **kwargs: None, lambda result: result)

    def test_protocol_error_propagates(self):
        session = Session(1, threading.Event())
        def start(*args, cancellable, callback):
            source = GLib.idle_source_new()
            source.set_callback(lambda *unused: (callback(None, None), False)[1])
            source.attach(session.context)
        def finish(result):
            raise RuntimeError('modem failure')
        with self.assertRaisesRegex(RuntimeError, 'modem failure'):
            session.wait(start, finish)

    def test_watchdog_prevents_unbounded_wait(self):
        session = Session(1, threading.Event())
        before = time.monotonic()
        with self.assertRaises(TimeoutError):
            session.wait(lambda **kwargs: None, lambda result: result)
        self.assertLess(time.monotonic() - before, 4)

    def test_pin_locked_sim_is_read_only(self):
        session = Mock()
        card = SimpleNamespace(card_state=Qmi.UimCardState.PRESENT,
                               applications=[SimpleNamespace(upin_replaces_pin1=False,
                                                              pin1_state=Qmi.UimPinState.ENABLED_NOT_VERIFIED)])
        session.request.side_effect = [SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.ONLINE),
                                       SimpleNamespace(get_card_status=lambda: (0, 0, 0, 0, [card]))]
        with self.assertRaisesRegex(RuntimeError, 'PIN/PUK'):
            DMSClient(session).prepare()
        self.assertEqual([call.args[1] for call in session.request.call_args_list],
                         ['get_operating_mode', 'get_card_status'])

    def test_missing_sim_reports_slot_and_hardware_error_without_writes(self):
        session = Mock()
        cards = [SimpleNamespace(card_state=Qmi.UimCardState.ABSENT),
                 SimpleNamespace(card_state=Qmi.UimCardState.ERROR, error_code=Qmi.UimCardError.NO_ATR_RECEIVED)]
        session.request.side_effect = [SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.ONLINE),
                                       SimpleNamespace(get_card_status=lambda: (0, 0, 0, 0, cards))]
        with self.assertRaisesRegex(RuntimeError, 'slot 1: absent; slot 2: error \\(no-atr-received\\)'):
            DMSClient(session).prepare()
        self.assertEqual([call.args[1] for call in session.request.call_args_list],
                         ['get_operating_mode', 'get_card_status'])

    def test_empty_sim_response_is_not_reported_as_an_inserted_card(self):
        self.assertEqual(DMSClient.card_summary([]), 'UIM returned no card slots')

    def test_card_status_owner_remains_alive_while_reading_borrowed_structs(self):
        # libqmi returns the card array with transfer-none ownership. Unlike
        # Mock.side_effect, this session does not retain the returned output.
        class BorrowedCard:
            def __init__(self, owner):
                self.owner = weakref.ref(owner)
            @property
            def card_state(self):
                if self.owner() is None:
                    raise AssertionError('Card status output was released before reading its structs')
                return Qmi.UimCardState.PRESENT
            @property
            def applications(self):
                if self.owner() is None:
                    raise AssertionError('Card applications outlived their owning response')
                return [SimpleNamespace(upin_replaces_pin1=False, pin1_state=Qmi.UimPinState.DISABLED)]
        class CardOutput:
            def get_card_status(self):
                return (0, 0, 0, 0, [BorrowedCard(self)])
        class ReadOnlySession:
            def request(self, service, name):
                if name == 'get_operating_mode':
                    return SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.ONLINE)
                if name == 'get_card_status':
                    return CardOutput()
                raise AssertionError('Unexpected operation: ' + name)
        DMSClient(ReadOnlySession()).prepare()

    def test_client_is_reused_and_typed_family(self):
        session = Mock()
        session.request.side_effect = lambda service, name, value=None: SimpleNamespace(
            get_packet_data_handle=lambda: 42, get_apn_name=lambda: 'internet')
        wds = WDSClient(session)
        wds.start('internet')
        service, method, value = next(call.args for call in session.request.call_args_list
                                     if call.args[1] == 'start_network')
        self.assertEqual(value.get_ip_family_preference(), Qmi.WdsIpFamily.IPV4)
        self.assertEqual(value.get_apn(), 'internet')
        wds.stop()
        service, method, value = session.request.call_args.args
        self.assertEqual(method, 'stop_network')
        self.assertEqual(value.get_packet_data_handle(), 42)
        self.assertIsNone(wds.handle)

    def test_qmi_ipv4_conversion(self):
        session = Mock()
        output = SimpleNamespace(get_ipv4_address=lambda: 0x0A2A0002,
                                 get_ipv4_gateway_subnet_mask=lambda: 0xFFFFFFFC,
                                 get_ipv4_gateway_address=lambda: 0x0A2A0001,
                                 get_primary_ipv4_dns_address=lambda: 0x01010101)
        session.request.return_value = output
        values = WDSClient(session).settings()
        self.assertEqual((values['address'], values['gateway']), ('10.42.0.2', '10.42.0.1'))
        self.assertEqual(values['netmask'], '255.255.255.252')
        self.assertEqual(values['dns'], ['1.1.1.1'])

    def test_noeffect_error_retains_its_protocol_type(self):
        session = Session(1, threading.Event())
        session.client = Mock(return_value=Mock())
        error = GLib.Error.new_literal(Qmi.protocol_error_quark(), 'Already active', Qmi.ProtocolError.NOEFFECT)
        session.wait = Mock(return_value=SimpleNamespace(get_result=Mock(side_effect=error)))
        with self.assertRaises(GLib.Error) as failure:
            session.request(Qmi.Service.WDS, 'start_network')
        self.assertTrue(failure.exception.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT))

    def noeffect_session(self, apn='tenten', connected=True, address=0xC0A80302):
        session = Mock()
        def request(service, name, value=None):
            if name == 'set_ip_family':
                self.assertEqual(value.get_preference(), Qmi.WdsIpFamily.IPV4)
                return SimpleNamespace()
            if name == 'start_network':
                raise GLib.Error.new_literal(Qmi.protocol_error_quark(), 'Already active', Qmi.ProtocolError.NOEFFECT)
            if name == 'get_packet_service_status':
                return SimpleNamespace(get_connection_status=lambda: Qmi.WdsConnectionStatus.CONNECTED if connected
                                       else Qmi.WdsConnectionStatus.DISCONNECTED)
            if name == 'get_current_settings':
                mask = value.get_requested_settings()
                self.assertTrue(mask & Qmi.WdsGetCurrentSettingsRequestedSettings.APN_NAME)
                return SimpleNamespace(get_apn_name=lambda: apn, get_ipv4_address=lambda: address,
                                       get_ipv4_gateway_subnet_mask=lambda: 0xFFFFFFFC,
                                       get_ipv4_gateway_address=lambda: 0xC0A80301)
            raise AssertionError('Unexpected operation: ' + name)
        session.request.side_effect = request
        return session

    def test_noeffect_reuses_connected_ipv4_without_global_disconnect(self):
        session = self.noeffect_session()
        wds = WDSClient(session)
        wds.start('tenten')
        self.assertTrue(wds.reused)
        self.assertEqual(wds.apn_reported, 'tenten')
        self.assertIsNone(wds.handle)
        self.assertEqual(wds.settings()['address'], '192.168.3.2')
        wds.stop()
        self.assertNotIn('stop_network', [call.args[1] for call in session.request.call_args_list])

    def test_noeffect_does_not_claim_disconnected_or_ipless_context_is_usable(self):
        for options in ({'connected': False}, {'address': 0}):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                WDSClient(self.noeffect_session(**options)).start('tenten')

    def test_noeffect_rejects_known_different_apn(self):
        wds = WDSClient(self.noeffect_session(apn='other.apn'))
        with self.assertRaisesRegex(RuntimeError, 'Existing data session uses APN'):
            wds.start('tenten')
        self.assertFalse(wds.reused)
        self.assertIsNone(wds.handle)

    def test_noeffect_rejects_unverifiable_foreign_apn_without_stopping_it(self):
        wds = WDSClient(self.noeffect_session(apn=None))
        with self.assertRaisesRegex(RuntimeError, 'cannot verify the requested APN'):
            wds.start('tenten')
        self.assertFalse(wds.reused)
        self.assertIsNone(wds.apn_reported)
        self.assertEqual(wds.apn_verification, 'unavailable')
        wds.stop()
        self.assertNotIn('stop_network', [call.args[1] for call in wds.session.request.call_args_list])

    def test_explicit_plmn_selection_retains_mnc_length_and_checks_registration(self):
        for target in ('99910', '999010'):
            with self.subTest(target=target):
                session = Mock()
                nas = NASClient(session)
                nas.select_network(target)
                value = session.request.call_args.args[2]
                self.assertEqual(value.get_network_selection_preference(),
                                 (Qmi.NasNetworkSelectionPreference.MANUAL, 999, 10))
                self.assertEqual(value.get_mnc_pcs_digit_include_status(), len(target) == 6)
                self.assertEqual(value.get_change_duration(), Qmi.NasChangeDuration.POWER_CYCLE)
                session.request.return_value = SimpleNamespace(get_serving_system=lambda:
                    (Qmi.NasRegistrationState.REGISTERED, Qmi.NasAttachState.DETACHED,
                     Qmi.NasAttachState.ATTACHED, Qmi.NasNetworkType(0), [Qmi.NasRadioInterface(12)]),
                    get_current_plmn=lambda: (999, 10, ''))
                self.assertTrue(nas.registered())
                session.request.return_value.get_current_plmn = lambda: (214, 7, '')
                self.assertFalse(nas.registered())
                session.request.return_value.get_mnc_pcs_digit_include_status = lambda: (999, 10, len(target) != 6)
                self.assertFalse(nas.registered())

    def test_default_network_selection_is_preserved_and_auto_is_explicit(self):
        session = Mock()
        nas = NASClient(session)
        nas.select_network('')
        session.request.assert_not_called()
        nas.select_network('auto')
        value = session.request.call_args.args[2]
        self.assertEqual(value.get_network_selection_preference()[0], Qmi.NasNetworkSelectionPreference.AUTOMATIC)

    def test_missing_optional_tlv_does_not_discard_strength(self):
        output = SimpleNamespace(get_signal_strength=lambda: (-71, Qmi.NasRadioInterface.LTE))
        result = NASClient.strength(output)
        self.assertEqual(result['nas_value_signal_strength_strength'], -71)
        self.assertIsNone(result['nas_value_sinr'])

    def test_registration_accepts_attached_lte(self):
        session = Mock()
        session.request.return_value = SimpleNamespace(get_serving_system=lambda:
            (Qmi.NasRegistrationState.REGISTERED, Qmi.NasAttachState.ATTACHED,
             Qmi.NasAttachState.ATTACHED, Qmi.NasNetworkType(2), []))
        self.assertTrue(NASClient(session).registered())
        session.request.assert_called_once()

    def test_sa_registration_fallback(self):
        session = Mock()
        serving = SimpleNamespace(get_serving_system=lambda:
            (Qmi.NasRegistrationState.NOT_REGISTERED, Qmi.NasAttachState.DETACHED,
             Qmi.NasAttachState.DETACHED, Qmi.NasNetworkType(2), []))
        system = SimpleNamespace(get_nr5g_service_status_info=lambda: (Qmi.NasServiceStatus.AVAILABLE, 0, True),
                                 get_nr5g_system_info=lambda: (True, Qmi.NasNetworkServiceDomain.PS,
                                                              True, Qmi.NasNetworkServiceDomain.PS,
                                                              False, 0, True, False))
        session.request.side_effect = [serving, system]
        self.assertTrue(NASClient(session).registered())
