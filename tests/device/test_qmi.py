import tempfile
import threading
import time
from pathlib import Path
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
        with self.assertRaisesRegex(RuntimeError,
                                    'logical slot 1: absent; logical slot 2: error \\(no-atr-received\\)'):
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


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class NasSystemInfoTests(unittest.TestCase):
    """M6: the Get System Info evidence, mirroring the verified hardware tuples of 04."""

    @staticmethod
    def nr5g_output(service=Qmi.NasServiceStatus.LIMITED, plmn=('214', '03'),
                    forbidden=True, reject_valid=False, reject_cause=0):
        info = (True, Qmi.NasNetworkServiceDomain.PS, False, Qmi.NasNetworkServiceDomain.NONE,
                True, Qmi.NasRoamingStatus.ON, True, forbidden,
                False, 0, False, 0,
                reject_valid, Qmi.NasNetworkServiceDomain.PS, reject_cause,
                plmn is not None, *(plmn or ('', '')), False, 0)
        return SimpleNamespace(
            get_sim_reject_info=lambda: Qmi.NasSimRejectState.AVAILABLE,
            get_network_selection_registration_restriction=lambda:
                Qmi.NasNetworkSelectionRegistrationRestriction.UNRESTRICTED,
            get_nr5g_service_status_info=lambda: (service, service, False),
            get_nr5g_system_info=lambda: info)

    def test_the_mocha1_limited_forbidden_reading_without_a_reject_cause(self):
        parsed = NASClient.system_info(self.nr5g_output())
        self.assertEqual(parsed['sim_reject'], 'available')
        self.assertEqual(parsed['restriction'], 'unrestricted')
        self.assertEqual(parsed['rats']['nr5g'], {
            'service': 'limited', 'true_service': 'limited', 'domain': 'ps',
            'forbidden': True, 'roaming': 'on', 'plmn': '21403',
            'service_capability': None})

    def test_a_lte_reject_cause_and_network_id_are_copied(self):
        info = (True, Qmi.NasNetworkServiceDomain.CS_PS, True,
                Qmi.NasNetworkServiceDomain.PS, False, 0, True, True,
                False, 0, False, 0, True, Qmi.NasNetworkServiceDomain.PS,
                Qmi.NasRejectCause.NO_SUITABLE_CELLS_IN_LOCATION_AREA,
                True, '999', '10', False, 0)
        output = SimpleNamespace(
            get_lte_service_status=lambda: (Qmi.NasServiceStatus.NONE,
                                            Qmi.NasServiceStatus.NONE, False),
            get_lte_system_info_v2=lambda: info)
        parsed = NASClient.system_info(output)
        self.assertNotIn('sim_reject', parsed)
        self.assertEqual(parsed['rats']['lte']['plmn'], '99910')
        self.assertEqual(parsed['rats']['lte']['domain'], 'cs-ps')
        self.assertEqual(parsed['rats']['lte']['service_capability'], 'ps')
        self.assertEqual(parsed['rats']['lte']['reject'], {'domain': 'ps', 'cause': 15})

    def test_a_wcdma_only_answer_and_unknown_enum_values_do_not_raise(self):
        info = (True, 99, False, Qmi.NasNetworkServiceDomain.NONE,
                False, 0, False, False, False, 0, False, 0,
                False, Qmi.NasNetworkServiceDomain.NONE, 0,
                True, '214', '03', False, 0)
        output = SimpleNamespace(
            get_wcdma_system_info_v2=lambda: info,
            get_sim_reject_info=lambda: 77)
        parsed = NASClient.system_info(output)
        self.assertEqual(parsed['sim_reject'], '#77')
        self.assertEqual(parsed['rats']['wcdma']['domain'], '#99')
        self.assertEqual(parsed['rats']['wcdma']['plmn'], '21403')
        self.assertNotIn('service', parsed['rats']['wcdma'])

    def test_an_empty_output_produces_no_claims(self):
        self.assertEqual(NASClient.system_info(SimpleNamespace()), {'rats': {}})


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class RegistrationBackendTests(unittest.TestCase):
    """M6: the backend keeps one bounded episode while registration is pending."""

    def setUp(self):
        from modules.device.backend import QmiBackend
        from modules.runtime.settings import Settings
        self.backend = QmiBackend(Settings(), threading.Event())

    def test_system_info_feeds_the_episode_and_keeps_dynamic_keys_unchanged(self):
        output = NasSystemInfoTests.nr5g_output()
        self.assertEqual(self.backend.registration_parser(output), {})
        self.assertEqual(self.backend.registration.samples, 1)
        self.assertEqual(self.backend.registration.sim_reject, 'available')
        self.assertIn('21403', self.backend.registration.camped)

    def test_failed_nas_queries_are_counted_and_serving_is_observed(self):
        report = {'requests': {'nas.get_serving_system': {'status': 'ok'},
                               'nas.get_system_info': {'status': 'timeout'}}}
        parsed = {'nas.get_serving_system': {
            'nas_value_serving_system_registration_state': 'registration-denied',
            'nas_value_serving_system_ps_attach_state': 'detached',
            'nas_value_current_plmn': '21403'}}
        self.backend.registration_observe(parsed, report)
        self.assertEqual(self.backend.registration.failures, 1)
        self.assertEqual(self.backend.registration.serving['plmn'], '21403')
        self.assertEqual(self.backend.registration.samples, 1)

    def test_the_timeout_diagnosis_names_a_registration_on_another_plmn(self):
        from dataclasses import replace
        from modules.runtime.settings import Settings
        backend = type(self.backend)(
            replace(Settings(), modem=replace(Settings().modem, plmn='99910')),
            threading.Event())
        backend.registration.observe_serving({
            'nas_value_serving_system_registration_state': 'registered',
            'nas_value_serving_system_ps_attach_state': 'attached',
            'nas_value_current_plmn': '21403'}, 0)
        value = backend.registration_diagnosis()
        self.assertEqual(value['code'], 'registration.wrong_network')
        self.assertEqual(value['params'], {'plmn': '21403', 'expected': '99910'})

    def test_a_successful_registration_resets_the_episode_and_keeps_the_plmn(self):
        self.backend.registration.observe_serving({
            'nas_value_serving_system_registration_state': 'registered',
            'nas_value_serving_system_ps_attach_state': 'attached',
            'nas_value_current_plmn': '99910'}, 0)
        self.backend.nas = Mock()
        self.backend.nas.registered.return_value = True
        self.assertTrue(self.backend.registered())
        self.assertEqual(self.backend.registered_plmn, '99910')
        self.assertEqual(self.backend.registration.samples, 0)
        self.assertIsNone(self.backend.registration.sim_reject)


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class NasHomeNetworkTests(unittest.TestCase):
    """M7: parse the home PLMN without guessing a missing MNC length."""

    @staticmethod
    def output(mnc=10, info=None):
        fields = {'get_home_network': lambda: (999, mnc, 'Home')}
        if info is not None:
            fields['get_home_network_3gpp_mnc'] = lambda: info
        return SimpleNamespace(**fields)

    def test_the_3gpp_tlv_decides_the_mnc_length(self):
        self.assertEqual(NASClient.home(self.output(10, (True, False)))['nas_value_home_network'],
                         '99910')
        self.assertEqual(NASClient.home(self.output(10, (True, True))), {
            'nas_value_home_network_mcc': 999, 'nas_value_home_network_mnc': 10,
            'nas_value_home_network_description': 'Home',
            'nas_value_home_network': '999010'})
        self.assertIsNone(NASClient.home(self.output(10, (False, False)))['nas_value_home_network'])

    def test_a_missing_3gpp_tlv_never_guesses_a_short_mnc(self):
        self.assertIsNone(NASClient.home(self.output(10))['nas_value_home_network'])
        self.assertEqual(NASClient.home(self.output(123))['nas_value_home_network'], '999123')

    def test_no_home_network_tlv_keeps_the_other_fields_and_adds_no_plmn(self):
        self.assertEqual(NASClient.home(SimpleNamespace()), {})


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class RegistrationHomeReadTests(unittest.TestCase):
    """M7: one bounded Get Home Network per registration problem, never on a healthy OPEN."""

    def setUp(self):
        from dataclasses import replace
        from modules.device.backend import QmiBackend
        from modules.runtime.settings import Settings
        self.backend = QmiBackend(replace(Settings(), modem=replace(
            Settings().modem, mode='connect', plmn='99910')), threading.Event())
        self.backend.session = Mock()
        self.backend.session.collection_deadline = None

    def test_no_read_on_the_first_open_of_a_healthy_modem(self):
        self.backend.maybe_read_home()
        self.backend.session.request.assert_not_called()

    def test_one_read_after_a_not_registered_observation(self):
        self.backend.registration.observe_serving({
            'nas_value_serving_system_registration_state': 'registration-denied',
            'nas_value_serving_system_ps_attach_state': 'detached',
            'nas_value_current_plmn': '21403'}, 0)
        self.backend.session.request.return_value = SimpleNamespace(
            get_home_network=lambda: (999, 10, 'Home'),
            get_home_network_3gpp_mnc=lambda: (True, False))
        self.backend.maybe_read_home()
        self.assertEqual(self.backend.home_plmn, '99910')
        self.assertEqual(self.backend.registration.home, '99910')
        self.backend.maybe_read_home()
        self.assertEqual(self.backend.session.request.call_count, 1)

    def test_a_uim_uninitialized_answer_leaves_the_home_unknown(self):
        self.backend.registration.observe_serving({
            'nas_value_serving_system_registration_state': 'registration-denied',
            'nas_value_serving_system_ps_attach_state': 'detached',
            'nas_value_current_plmn': '21403'}, 0)
        self.backend.session.request.side_effect = GLib.Error.new_literal(
            Qmi.protocol_error_quark(), 'UimUninitialized',
            Qmi.ProtocolError.UIMUNINITIALIZED)
        self.backend.maybe_read_home()
        self.assertIsNone(self.backend.home_plmn)
        self.assertEqual(self.backend.registration.samples, 1)


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class CallEndReasonTests(unittest.TestCase):
    """M19: readable call-end and offline reasons, with numeric fallbacks."""

    @staticmethod
    def output(reason=None, verbose=None):
        fields = {'get_call_end_reason': lambda: reason}
        if verbose is not None:
            fields['get_verbose_call_end_reason'] = lambda: verbose
        return SimpleNamespace(**fields)

    def test_known_reasons_and_the_3gpp_verbose_example(self):
        from modules.services.client import decode_call_end
        decoded = decode_call_end(self.output(Qmi.WdsCallEndReason.GENERIC_NO_SERVICE, (6, 33)))
        self.assertEqual(decoded['reason'], 'generic-no-service')
        self.assertEqual(decoded['verbose_type'], '3gpp')
        self.assertEqual(decoded['verbose_code'], 33)
        self.assertEqual(decoded['verbose_label'], 'option-unsubscribed')
        self.assertEqual(decoded['verbose_text'], '3GPP #33')

    def test_unknown_values_fall_back_to_numbers(self):
        from modules.services.client import decode_call_end
        decoded = decode_call_end(self.output(999, (999, 999)))
        self.assertEqual(decoded['reason'], '999')
        self.assertEqual(decoded['verbose_type'], 999)
        self.assertEqual(decoded['verbose_code'], 999)
        self.assertIsNone(decoded['verbose_label'])
        self.assertNotIn('verbose_text', decoded)
        self.assertEqual(decode_call_end(SimpleNamespace())['reason'], 'unknown')

    def test_the_bearer_status_keeps_the_call_end_reason_without_new_csv_columns(self):
        from modules.services.indications import bearer_status
        from modules.telemetry.collector import fields
        from modules.telemetry.metrics import ListDynamicMetrics
        output = SimpleNamespace(get_connection_status=lambda: (Qmi.WdsConnectionStatus.CONNECTED,),
                                 get_call_end_reason=lambda: 3)
        self.assertEqual(bearer_status(output), {
            'wds_value_connection_status': 'connected',
            'wds_value_call_end_reason': 'generic-no-service'})
        names = list(fields(ListDynamicMetrics))
        self.assertNotIn('wds_value_call_end_reason', names)

    def test_the_offline_reason_and_hardware_restriction_are_readable(self):
        from modules.services.dms_client import DMSClient
        self.assertEqual(DMSClient.offline_report(SimpleNamespace(
            get_offline_reason=lambda: 1, get_hardware_restricted_mode=lambda: True)),
            {'offline_reason': 'host-image-misconfiguration', 'hardware_restricted': True})
        self.assertEqual(DMSClient.offline_report(SimpleNamespace(
            get_offline_reason=lambda: 99)), {'offline_reason': '#99'})

    def test_a_later_sim_failure_keeps_the_offline_reason(self):
        from modules.services.client import decode_call_end
        self.assertEqual(decode_call_end(self.output(3))['reason'], 'generic-no-service')
        session = Mock()
        operating = SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.LOW_POWER,
                                    get_offline_reason=lambda: 1,
                                    get_hardware_restricted_mode=lambda: True)
        card = SimpleNamespace(card_state=Qmi.UimCardState.ABSENT, applications=[])
        session.request.side_effect = [operating, SimpleNamespace(),
                                       SimpleNamespace(get_card_status=lambda: (0, 0, 0, 0, [card]))]
        with self.assertRaises(RuntimeError) as raised:
            DMSClient(session).prepare()
        self.assertEqual(raised.exception.diagnosis['code'], 'sim.absent')
        self.assertEqual(raised.exception.diagnosis['params']['offline_reason'],
                         'host-image-misconfiguration')
        self.assertTrue(raised.exception.diagnosis['params']['hardware_restricted'])


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class SelectionAndForbiddenTests(unittest.TestCase):
    """M18: selection preference and EF_FPLMN, read-only after a registration timeout."""

    @staticmethod
    def selection(mode=0,
                  disabled=0, selection=Qmi.NasNetworkSelectionPreference.AUTOMATIC,
                  manual=None, roaming=None):
        fields = {'get_mode_preference': lambda: mode,
                  'get_disabled_modes': lambda: disabled,
                  'get_network_selection_preference': lambda: selection}
        if manual is not None:
            fields['get_manual_network_selection'] = lambda: manual
        if roaming is not None:
            fields['get_roaming_preference'] = lambda: roaming
        return SimpleNamespace(**fields)

    def test_5g_enabled_uses_the_mode_bit_and_not_the_disabled_mask(self):
        fiveg = int(getattr(Qmi.NasRatModePreference, '5GNR'))
        enabled = NASClient.system_selection(self.selection(mode=fiveg))
        self.assertTrue(enabled['nr5g_enabled'])
        self.assertEqual(enabled['selection'], 'automatic')
        disabled = NASClient.system_selection(self.selection(mode=fiveg, disabled=fiveg))
        self.assertFalse(disabled['nr5g_enabled'])

    def test_manual_selection_names_its_plmn(self):
        manual = NASClient.system_selection(self.selection(
            mode=64, selection=Qmi.NasNetworkSelectionPreference.MANUAL,
            manual=(999, 10, False)))
        self.assertEqual((manual['selection'], manual['manual_plmn']), ('manual', '99910'))
        automatic = NASClient.system_selection(self.selection(mode=64))
        self.assertNotIn('manual_plmn', automatic)

    def test_the_forbidden_list_decodes_plmns_and_skips_padding(self):
        self.assertEqual(DMSClient.decode_fplmn([0x12, 0xF4, 0x30, 0xFF, 0xFF, 0xFF,
                                                 0x99, 0xF9, 0x01]),
                         ['21403', '99910'])
        self.assertEqual(DMSClient.decode_fplmn([0x1A, 0xF4, 0x30]), [])
        self.assertEqual(DMSClient.decode_fplmn(None), [])
        output = SimpleNamespace(get_card_result=lambda: (0x90, 0),
                                 get_read_result=lambda: [0x12, 0xF4, 0x30])
        self.assertEqual(DMSClient.uim_forbidden(output), {'forbidden_plmns': ['21403']})
        failed = SimpleNamespace(get_card_result=lambda: (0x69, 0x82),
                                 get_read_result=lambda: [0x12, 0xF4, 0x30])
        self.assertEqual(DMSClient.uim_forbidden(failed), {'forbidden_plmns': []})

    def test_the_reads_happen_once_after_a_timeout_and_never_for_an_unready_card(self):
        from dataclasses import replace
        from modules.device.backend import QmiBackend
        from modules.runtime.settings import Settings
        backend = QmiBackend(replace(Settings(), modem=replace(
            Settings().modem, mode='connect')), threading.Event())
        backend.session = Mock()

        def request(service, name, value=None):
            if name == 'get_system_selection_preference':
                return SelectionAndForbiddenTests.selection(mode=64)
            if name == 'read_transparent':
                return SimpleNamespace(get_card_result=lambda: (0x90, 0),
                                       get_read_result=lambda: [0x12, 0xF4, 0x30])
            raise AssertionError(name)
        backend.session.request.side_effect = request
        backend.sim_report = {'code': None, 'report': {}}
        backend.maybe_read_registration_context()
        backend.session.request.assert_not_called()
        backend.note_registration_timeout()
        backend.maybe_read_registration_context()
        self.assertEqual(backend.registration.context['nr5g_enabled'], True)
        self.assertEqual(backend.registration.context['forbidden'], ['21403'])
        backend.maybe_read_registration_context()
        self.assertEqual(backend.session.request.call_count, 2, 'the 30-minute limiter reuses the reads')
        backend.next_registration_context = 0
        backend.maybe_read_registration_context()
        self.assertEqual(backend.session.request.call_count, 4)
        backend.sim_report = {'code': 'sim.pin_required', 'report': {}}
        backend.next_registration_context = 0
        backend.maybe_read_registration_context()
        self.assertEqual(backend.session.request.call_count, 4, 'a locked card is not read')


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class EphemeralClientTests(unittest.TestCase):
    """M17: a fresh client for the probe is always released and never cached."""

    class Device:
        def __init__(self, cid, fail_release=False):
            self.cid, self.fail_release = cid, fail_release
        def allocate_client(self, *args, **kwargs):
            pass
        def allocate_client_finish(self, *args, **kwargs):
            return EphemeralClientTests.Client(self.cid)
        def release_client(self, *args, **kwargs):
            pass
        def release_client_finish(self, *args, **kwargs):
            if self.fail_release:
                raise RuntimeError('release failed')
            return None

    class Client:
        def __init__(self, cid, fail=False):
            self.cid, self.fail, self.calls = cid, fail, []
        def get_cid(self):
            return self.cid
        def get_packet_service_status(self, *args, **kwargs):
            self.calls.append('status')
        def get_packet_service_status_finish(self, response):
            if self.fail:
                raise RuntimeError('modem did not answer')
            return SimpleNamespace(get_result=lambda: None)
        def get_current_settings(self, *args, **kwargs):
            self.calls.append('settings')
        def get_current_settings_finish(self, response):
            return SimpleNamespace(get_result=lambda: None)

    def session(self, cid, fail_release=False, fail=False):
        value = Session(1, threading.Event())
        value.device = self.Device(cid, fail_release)
        value.wait = Mock(side_effect=lambda start, finish, *args, **kwargs: finish(None))
        value.last_client = None
        original = value.device.allocate_client_finish
        def allocate(*args, **kwargs):
            client = original(*args, **kwargs)
            client.fail = fail
            value.last_client = client
            return client
        value.device.allocate_client_finish = allocate
        return value

    def test_a_fresh_client_runs_read_only_requests_and_is_released_with_its_cid(self):
        session = self.session(7)
        result = session.ephemeral(Qmi.Service.WDS,
                                   [('get_packet_service_status', None),
                                    ('get_current_settings', None)])
        self.assertTrue(result['ok'])
        self.assertTrue(result['release_ok'])
        self.assertFalse(result['same_cid'])
        self.assertEqual(set(result['results']),
                         {'get_packet_service_status', 'get_current_settings'})
        self.assertEqual(session.clients, {}, 'the probe never becomes a cached client')
        release = [call.args[2:4] for call in session.wait.call_args_list
                   if call.args[0].__name__ == 'release_client']
        self.assertEqual(release, [(session.last_client, Qmi.DeviceReleaseClientFlags.RELEASE_CID)])

    def test_a_firmware_that_reuses_the_saved_cid_sends_nothing(self):
        session = self.session(7)
        result = session.ephemeral(Qmi.Service.WDS, [('get_packet_service_status', None)],
                                   saved_cid=7)
        self.assertTrue(result['same_cid'])
        self.assertEqual(result['results'], {})
        self.assertEqual(session.last_client.calls, [])
        release = [call.args[3] for call in session.wait.call_args_list
                   if call.args[0].__name__ == 'release_client']
        self.assertEqual(release, [Qmi.DeviceReleaseClientFlags.NONE])

    def test_a_failed_request_or_release_is_reported_and_the_client_still_goes_away(self):
        failing = self.session(7, fail=True)
        result = failing.ephemeral(Qmi.Service.WDS, [('get_packet_service_status', None)])
        self.assertFalse(result['ok'])
        self.assertEqual(result['results'], {})
        leaking = self.session(7, fail_release=True)
        result = leaking.ephemeral(Qmi.Service.WDS, [('get_packet_service_status', None)])
        self.assertFalse(result['release_ok'])
        self.assertEqual(leaking.clients, {})


@unittest.skipIf(Session is None, 'Requires OS libqmi GI bindings')
class SavedSessionProbeTests(unittest.TestCase):
    """M17: the diagnostic probe is bounded, rate-limited and never retires anything."""

    def backend(self, directory):
        from dataclasses import replace
        from modules.device.backend import QmiBackend
        from modules.runtime.settings import Settings
        backend = QmiBackend(replace(Settings(), modem=replace(
            Settings().modem, mode='connect')), threading.Event(),
            str(Path(directory)/'qmi-supervisor-state.json'))
        backend.device = SimpleNamespace(identity='x', usb_path='/sys/x', path='/dev/x')
        backend.session = Mock()
        backend.session.device = object()
        backend._transport_changed = Mock(return_value=False)
        backend.session.ephemeral.return_value = {
            'ok': True, 'same_cid': False, 'release_ok': True, 'results': {
                'get_packet_service_status': SimpleNamespace(
                    get_connection_status=lambda: Qmi.WdsConnectionStatus.DISCONNECTED),
                'get_current_settings': SimpleNamespace(
                    get_ipv4_address=lambda: None, get_apn_name=lambda: None)}}
        return backend

    def test_the_probe_publishes_its_evidence_and_backs_off(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(directory)
            saved = {'identity': 'x', 'enumeration': '1', 'cid': 5, 'handle': 9}
            with patch('modules.device.backend.time.time', return_value=1000.0):
                backend.probe_saved_session(saved)
            backend.session.forget.assert_called_once_with(Qmi.Service.WDS)
            self.assertEqual(backend.probe['modem_answers'], True)
            self.assertEqual(backend.probe['packet_status'], 'disconnected')
            self.assertEqual(backend.probe['current_settings'], 'out_of_call')
            self.assertTrue((Path(directory)/'qmi-supervisor-state-probe.json').exists())
            self.assertEqual(backend.session.client.call_count, 0,
                             'the probe allocates outside the cached clients')
            backend.probe = None
            with patch('modules.device.backend.time.time', return_value=1001.0):
                backend.probe_saved_session(saved)
            self.assertIsNone(backend.probe, 'the 10-minute backoff skips a second probe')
            self.assertEqual(backend.session.ephemeral.call_count, 1)

    def test_a_second_identical_result_or_a_failed_release_stops_the_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = self.backend(directory)
            backend.session.ephemeral.return_value = {
                'ok': True, 'same_cid': False, 'release_ok': False, 'results': {
                    'get_packet_service_status': SimpleNamespace(
                        get_connection_status=lambda: Qmi.WdsConnectionStatus.DISCONNECTED),
                    'get_current_settings': SimpleNamespace(
                        get_ipv4_address=lambda: None, get_apn_name=lambda: None)}}
            saved = {'identity': 'x', 'enumeration': '1', 'cid': 5, 'handle': 9}
            with patch('modules.device.backend.time.time', return_value=1000.0):
                backend.probe_saved_session(saved)
            self.assertTrue(backend.probe.get('stopped'))

    def test_no_probe_without_a_device_an_unchanged_transport_or_a_stopped_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = {'identity': 'x', 'enumeration': '1', 'cid': 5, 'handle': 9}
            backend = self.backend(directory)
            backend.stop.set()
            backend.probe_saved_session(saved)
            backend.session.ephemeral.assert_not_called()
            backend.stop.clear()
            backend._transport_changed.return_value = True
            backend.probe_saved_session(saved)
            backend.session.ephemeral.assert_not_called()
            backend._transport_changed.return_value = False
            backend.device = None
            backend.probe_saved_session(saved)
            backend.session.ephemeral.assert_not_called()
