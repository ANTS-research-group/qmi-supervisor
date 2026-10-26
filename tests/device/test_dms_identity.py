from types import SimpleNamespace
from dataclasses import replace
import threading
import unittest
from unittest.mock import Mock, patch

try:
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import GLib, Qmi
    from modules.services.dms_client import DMSClient
    from modules.device.backend import QmiBackend
    from modules.runtime.settings import Settings
except (ImportError, ValueError):
    DMSClient = None


@unittest.skipIf(DMSClient is None, 'Requires libqmi GI')
class DmsIdentityTests(unittest.TestCase):
    def test_imsi_is_an_optional_static_query_without_changing_existing_csv_columns(self):
        from modules.telemetry.metrics import ListStaticMetrics
        from modules.telemetry.collector import fields
        specs = DMSClient.specifications()
        spec = next(item for item in specs if item[0] == 'dms.imsi')
        self.assertEqual(spec[:4], ('dms.imsi', Qmi.Service.DMS, 'uim_get_imsi', None))
        self.assertNotIn('dms_value_imsi', fields(ListStaticMetrics))
        self.assertTrue(callable(getattr(Qmi.ClientDms, spec[2])))
        self.assertIn('get_imsi', dir(Qmi.MessageDmsUimGetImsiOutput))

    def test_imsi_preserves_leading_zeroes_as_a_string(self):
        imsi = '001010000000001'  # Synthetic test identifier, never a real SIM.
        self.assertEqual(DMSClient.imsi(SimpleNamespace(get_imsi=lambda: imsi)), {'dms_value_imsi': imsi})

    def test_missing_or_malformed_imsi_is_unavailable(self):
        self.assertEqual(DMSClient.imsi(SimpleNamespace()), {'dms_value_imsi': None})
        for value in (None, '', 101000000001, '1234', '1'*16, '00101abc', '001010000000001\n'):
            with self.subTest(type=type(value).__name__):
                self.assertEqual(DMSClient.imsi(SimpleNamespace(get_imsi=lambda: value)), {'dms_value_imsi': None})

    def test_unsupported_imsi_does_not_discard_device_identification_or_log_identity(self):
        synthetic = '001010000000001'
        session = Mock()
        def request(service, name, value=None):
            if name == 'uim_get_imsi':
                raise RuntimeError('unsupported response containing ' + synthetic)
            if name == 'get_ids':
                return SimpleNamespace(get_imei=lambda: 'test-device')
            if name == 'get_software_version':
                return SimpleNamespace(get_version=lambda: 'test-version')
            if name == 'get_capabilities':
                return SimpleNamespace(get_info=lambda: (1, 2, Qmi.DmsDataServiceCapability.CS_PS,
                    Qmi.DmsSimCapability.SUPPORTED, [Qmi.DmsRadioInterface.LTE]))
            raise AssertionError('Unexpected request')
        session.request.side_effect = request
        with self.assertLogs('modules.services.dms_client', level='DEBUG') as captured:
            values = DMSClient(session).metrics()
        self.assertIsNone(values['dms_value_imsi'])
        self.assertEqual(values['dms_value_imei'], 'test-device')
        self.assertEqual(values['dms_value_imei_software_version'], 'test-version')
        self.assertNotIn(synthetic, '\n'.join(captured.output))
        self.assertEqual(sum(call.args[1] == 'uim_get_imsi' for call in session.request.call_args_list), 1)

    def test_pin_locked_sim_preparation_never_requests_imsi_or_consumes_retries(self):
        session = Mock()
        card = SimpleNamespace(card_state=Qmi.UimCardState.PRESENT,
            applications=[SimpleNamespace(upin_replaces_pin1=False, pin1_state=Qmi.UimPinState.ENABLED_NOT_VERIFIED)])
        session.request.side_effect = [SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.ONLINE),
                                       SimpleNamespace(get_card_status=lambda: (0, 0, 0, 0, [card]))]
        with self.assertRaisesRegex(RuntimeError, 'PIN/PUK'):
            DMSClient(session).prepare()
        self.assertEqual([call.args[1] for call in session.request.call_args_list],
                         ['get_operating_mode', 'get_card_status'])

    def test_absent_sim_preparation_never_requests_imsi(self):
        session = Mock()
        card = SimpleNamespace(card_state=Qmi.UimCardState.ABSENT)
        session.request.side_effect = [SimpleNamespace(get_mode=lambda: Qmi.DmsOperatingMode.ONLINE),
                                       SimpleNamespace(get_card_status=lambda: (0, 0, 0, 0, [card]))]
        with self.assertRaisesRegex(RuntimeError, 'SIM absent'):
            DMSClient(session).prepare()
        self.assertEqual([call.args[1] for call in session.request.call_args_list],
                         ['get_operating_mode', 'get_card_status'])

    def test_primary_sim_status_ignores_an_unrelated_locked_slot(self):
        unlocked = SimpleNamespace(card_state=Qmi.UimCardState.PRESENT,
            applications=[SimpleNamespace(upin_replaces_pin1=False, pin1_state=Qmi.UimPinState.DISABLED)])
        locked = SimpleNamespace(card_state=Qmi.UimCardState.PRESENT,
            applications=[SimpleNamespace(upin_replaces_pin1=False, pin1_state=Qmi.UimPinState.ENABLED_NOT_VERIFIED)])
        output = SimpleNamespace(get_card_status=lambda: (0, 0xFFFF, 0xFFFF, 0xFFFF, [unlocked, locked]))
        self.assertEqual(DMSClient.card_identity_status(output), {'state': 'ready', 'slot': 1})
        output = SimpleNamespace(get_card_status=lambda: (0x0100, 0xFFFF, 0xFFFF, 0xFFFF, [unlocked, locked]))
        self.assertEqual(DMSClient.card_identity_status(output), {'state': 'pin_required', 'slot': 2})

    def test_present_card_with_unavailable_applications_is_unknown(self):
        card = SimpleNamespace(card_state=Qmi.UimCardState.PRESENT, applications=[])
        output = SimpleNamespace(get_card_status=lambda: (0, 0xFFFF, 0xFFFF, 0xFFFF, [card]))
        self.assertEqual(DMSClient.card_identity_status(output), {'state': 'unknown', 'slot': 1})

    def test_ef_imsi_bcd_length_parity_and_leading_zeroes(self):
        # Fixed synthetic vectors exercise both parity variants and FF padding.
        for encoded, expected in (
                ([8, 0x09, 0x10, 0x10, 0, 0, 0, 0, 0x10], '001010000000001'),
                ([8, 0x01, 0x10, 0x10, 0, 0, 0, 0, 0xF1], '00101000000001'),
                ([5, 0x09, 0x10, 0x10, 0, 0, 0xFF, 0xFF, 0xFF], '001010000'),
                (bytes([5, 0x09, 0x10, 0x10, 0, 0]), '001010000')):
            with self.subTest(length=len(encoded), digits=len(expected)):
                self.assertEqual(DMSClient.decode_ef_imsi(encoded), expected)

    def test_invalid_ef_imsi_is_unavailable_without_guessing_digits(self):
        for encoded in (None, '', [], [0], [0, 9], [9, 9], [8, 9, 0],
                [5, 0, 0x10, 0x10, 0, 0], [5, 3, 0x10, 0x10, 0, 0],
                [5, 0xF9, 0x10, 0x10, 0, 0], [5, 9, 0x1A, 0x10, 0, 0],
                [5, 9, 0x10, 0x10, 0, 0xF0], [5, 1, 0x10, 0x10, 0, 0],
                [5, 9, 0x10, 0x10, 0, 0, 0], [2, 9, 0x10],
                [5, 9, True, 0x10, 0, 0], [5, 9, 256, 0x10, 0, 0],
                [5, 9, -1, 0x10, 0, 0], [5, 9, '16', 0x10, 0, 0]):
            with self.subTest(encoded=encoded):
                self.assertIsNone(DMSClient.decode_ef_imsi(encoded))

    def test_uim_imsi_respects_card_file_errors_and_missing_result(self):
        encoded = [8, 0x09, 0x10, 0x10, 0, 0, 0, 0, 0x10]
        for result in ((0x90, 0), (0x91, 0x0F)):
            output = SimpleNamespace(get_card_result=lambda: result, get_read_result=lambda: encoded)
            self.assertEqual(DMSClient.uim_imsi(output)['dms_value_imsi'], '001010000000001')
        for result in ((0x69, 0x82), (), (0x90,), 'bad', (0x90, 1)):
            with self.subTest(result=result):
                output = SimpleNamespace(get_card_result=lambda: result, get_read_result=lambda: encoded)
                self.assertIsNone(DMSClient.uim_imsi(output)['dms_value_imsi'])
        self.assertIsNone(DMSClient.uim_imsi(SimpleNamespace())['dms_value_imsi'])

    def test_fallback_uses_primary_gw_read_only_ef_imsi_protocol(self):
        card = SimIdentityCacheTests.present_card()
        output = SimpleNamespace(get_card_status=lambda: (0, 0xFFFF, 0xFFFF, 0xFFFF, [card]))
        key, service, method, value, parser = DMSClient.uim_imsi_specification(output)
        self.assertEqual((key, service, method), ('uim.imsi', Qmi.Service.UIM, 'read_transparent'))
        session, aid = value.get_session()
        file_id, path = value.get_file()
        self.assertEqual((session, list(aid)), (Qmi.UimSessionType.PRIMARY_GW_PROVISIONING, []))
        self.assertEqual((file_id, list(path)), (0x6F07, [0x00, 0x3F, 0xFF, 0x7F]))
        self.assertEqual(value.get_read_information(), (0, 0))
        self.assertIs(parser, DMSClient.uim_imsi)

    def test_fallback_requires_present_provisioned_card_but_allows_unknown_app_metadata(self):
        card = SimpleNamespace(card_state=Qmi.UimCardState.PRESENT, applications=[])
        output = lambda primary, cards: SimpleNamespace(
            get_card_status=lambda: (primary, 0xFFFF, 0xFFFF, 0xFFFF, cards))
        self.assertIsNotNone(DMSClient.uim_imsi_specification(output(0, [card])))
        for primary, cards in ((0xFFFF, [card]), (0x0100, [card]), (0, []),
                (0, [SimpleNamespace(card_state=Qmi.UimCardState.ABSENT)]),
                (1, [SimIdentityCacheTests.present_card()])):
            with self.subTest(primary=primary, cards=len(cards)):
                self.assertIsNone(DMSClient.uim_imsi_specification(output(primary, cards)))

    def test_fallback_skips_explicit_pin_locks_and_incompatible_applications(self):
        for field, value in (
                ('pin1_state', Qmi.UimPinState.ENABLED_NOT_VERIFIED),
                ('pin1_state', Qmi.UimPinState.BLOCKED),
                ('pin1_state', Qmi.UimPinState.PERMANENTLY_BLOCKED),
                ('state', Qmi.UimCardApplicationState.PIN1_OR_UPIN_PIN_REQUIRED),
                ('state', Qmi.UimCardApplicationState.PUK1_OR_UPIN_PUK_REQUIRED),
                ('state', Qmi.UimCardApplicationState.PIN1_BLOCKED),
                ('type', Qmi.UimCardApplicationType.ISIM),
                ('type', Qmi.UimCardApplicationType.RUIM)):
            with self.subTest(field=field, value=int(value)):
                card = SimIdentityCacheTests.present_card()
                setattr(card.applications[0], field, value)
                output = SimpleNamespace(get_card_status=lambda: (0, 0xFFFF, 0xFFFF, 0xFFFF, [card]))
                self.assertIsNone(DMSClient.uim_imsi_specification(output))


@unittest.skipIf(DMSClient is None, 'Requires libqmi GI')
class SimIdentityCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.dynamic_cost = 0
        self.static_cost = 0
        self.imsi = '001010000000001'
        self.identity_available = True
        self.status_available = True
        self.ef_imsi_available = False
        self.ef_imsi_data = [8, 0x09, 0x10, 0x10, 0, 0, 0, 0, 0x10]
        self.primary = 0
        self.card = self.present_card()
        self.batches = []
        self.budgets = []
        settings = replace(Settings(), modem=replace(Settings().modem, mode='monitor'))
        self.backend = QmiBackend(settings, threading.Event())
        self.backend.session = Mock()
        self.backend.session.collect.side_effect = self.collect
        self.backend.nas.specifications = Mock(return_value=[])
        self.backend.wds.specifications = Mock(return_value=[])
        clock = patch('modules.device.backend.time.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    @staticmethod
    def present_card(pin=None):
        return SimpleNamespace(card_state=Qmi.UimCardState.PRESENT, applications=[SimpleNamespace(
            type=Qmi.UimCardApplicationType.USIM, state=Qmi.UimCardApplicationState.READY,
            upin_replaces_pin1=False, pin1_state=pin if pin is not None else Qmi.UimPinState.DISABLED)])

    def collect(self, requests, *, budget, request_timeout):
        self.batches.append([item[0] for item in requests])
        self.budgets.append(budget)
        if not requests:
            self.now += self.dynamic_cost
        elif any(item[0] == 'dms.imsi' for item in requests):
            self.now += self.static_cost
        outputs, records = {}, {}
        for key, service, name, value in requests:
            records[key] = {'status': 'ok'}
            if key == 'dms.imsi':
                if self.identity_available:
                    current = self.imsi
                    outputs[key] = SimpleNamespace(get_imsi=lambda: current)
                else:
                    records[key] = {'status': 'error', 'unsupported': True}
            elif key == 'uim.identity_status':
                if self.status_available:
                    outputs[key] = SimpleNamespace(get_card_status=lambda: (self.primary, 0xFFFF, 0xFFFF, 0xFFFF, [self.card]))
                else:
                    records[key] = {'status': 'error', 'unsupported': True}
            elif key == 'uim.imsi':
                if self.ef_imsi_available:
                    outputs[key] = SimpleNamespace(get_card_result=lambda: (0x90, 0),
                        get_read_result=lambda: self.ef_imsi_data)
                else:
                    records[key] = {'status': 'error', 'unsupported': True}
            elif key == 'dms.ids':
                outputs[key] = SimpleNamespace(get_imei=lambda: 'test-device')
            elif key == 'dms.version':
                outputs[key] = SimpleNamespace(get_version=lambda: 'test-version')
            elif key == 'dms.capabilities':
                outputs[key] = SimpleNamespace(get_info=lambda: (1, 2, Qmi.DmsDataServiceCapability.CS_PS,
                    Qmi.DmsSimCapability.SUPPORTED, [Qmi.DmsRadioInterface.LTE]))
            else:
                raise AssertionError('Unexpected static request ' + key)
        return outputs, {'requests': records, 'complete': all(item['status'] == 'ok' for item in records.values())}

    def test_imsi_is_cached_for_sixty_seconds_while_hardware_is_cached_for_an_hour(self):
        for second in range(60):
            self.now = second
            static, _ = self.backend.metrics()
            self.assertEqual(static['dms_value_imsi'], '001010000000001')
        self.assertEqual(sum('dms.imsi' in batch for batch in self.batches), 1)
        self.assertEqual(sum('uim.identity_status' in batch for batch in self.batches), 1)
        self.assertEqual(sum('dms.ids' in batch for batch in self.batches), 1)
        self.now = 60
        self.backend.metrics()
        self.assertEqual(sum('dms.imsi' in batch for batch in self.batches), 2)
        self.assertEqual(sum('dms.ids' in batch for batch in self.batches), 1)

    def test_sim_change_replaces_imsi_and_absent_or_pin_required_clears_it(self):
        self.backend.metrics()
        self.now = 60
        self.imsi = '001010000000002'
        static, _ = self.backend.metrics()
        self.assertEqual(static['dms_value_imsi'], self.imsi)
        self.now = 120
        self.card = SimpleNamespace(card_state=Qmi.UimCardState.ABSENT)
        static, _ = self.backend.metrics()
        self.assertIsNone(static['dms_value_imsi'], 'Firmware-cached IMSI must not survive a known absent card')
        self.assertEqual(static['dms_value_sim_card_status']['state'], 'absent')
        self.now = 180
        self.card = self.present_card(Qmi.UimPinState.ENABLED_NOT_VERIFIED)
        static, _ = self.backend.metrics()
        self.assertIsNone(static['dms_value_imsi'])
        self.assertEqual(static['dms_value_sim_card_status']['state'], 'pin_required')

    def test_unsupported_identity_clears_old_cache_without_failing_dynamic_metrics(self):
        self.backend.metrics()
        self.now = 60
        self.identity_available = False
        static, dynamic = self.backend.metrics()
        self.assertIsNone(static['dms_value_imsi'])
        self.assertEqual(static['dms_value_imei'], 'test-device')
        self.assertEqual(dynamic, {})
        self.assertEqual(self.backend.collection_report['static_requests']['dms.imsi']['status'], 'error')
        self.assertTrue(self.backend.collection_report['complete'])

    def test_missing_optional_uim_status_does_not_discard_direct_imsi_response(self):
        self.status_available = False
        static, _ = self.backend.metrics()
        self.assertEqual(static['dms_value_imsi'], self.imsi)
        self.assertIsNone(static['dms_value_sim_card_status'])

    def test_gi_missing_applications_preserves_direct_imsi_and_reports_unknown_status(self):
        self.card.applications = []
        static, _ = self.backend.metrics()
        self.assertEqual(static['dms_value_imsi'], self.imsi)
        self.assertEqual(static['dms_value_sim_card_status'], {'state': 'unknown', 'slot': 1})
        self.assertFalse(any('uim.imsi' in batch for batch in self.batches))

    def test_unsupported_dms_uses_uim_fallback_cached_for_sixty_seconds(self):
        self.identity_available = False
        self.ef_imsi_available = True
        for second in range(60):
            self.now = second
            static, dynamic = self.backend.metrics()
            self.assertEqual(static['dms_value_imsi'], '001010000000001')
            self.assertEqual(dynamic, {})
        self.assertEqual(sum('uim.imsi' in batch for batch in self.batches), 1)
        self.assertEqual(sum('dms.imsi' in batch for batch in self.batches), 1)
        self.now = 60
        self.ef_imsi_data = [8, 0x09, 0x10, 0x10, 0, 0, 0, 0, 0x20]
        static, _ = self.backend.metrics()
        self.assertEqual(static['dms_value_imsi'], '001010000000002')
        self.assertEqual(sum('uim.imsi' in batch for batch in self.batches), 2)
        self.assertEqual(self.backend.collection_report['static_requests']['uim.imsi']['status'], 'ok')

    def test_unknown_application_metadata_allows_uim_read_for_valid_primary_card(self):
        self.identity_available = False
        self.ef_imsi_available = True
        self.card.applications = []
        static, _ = self.backend.metrics()
        self.assertEqual(static['dms_value_imsi'], '001010000000001')
        self.assertEqual(static['dms_value_sim_card_status'], {'state': 'unknown', 'slot': 1})
        self.assertIn(['uim.imsi'], self.batches)

    def test_read_fallback_is_not_sent_for_absent_locked_or_unprovisioned_sim(self):
        self.identity_available = False
        self.ef_imsi_available = True
        cases = ((0, SimpleNamespace(card_state=Qmi.UimCardState.ABSENT)),
                 (0, self.present_card(Qmi.UimPinState.ENABLED_NOT_VERIFIED)),
                 (0xFFFF, self.present_card()), (0x0100, self.present_card()))
        for index, (primary, card) in enumerate(cases):
            with self.subTest(case=index):
                self.now = index * 60
                self.primary, self.card = primary, card
                static, _ = self.backend.metrics()
                self.assertIsNone(static['dms_value_imsi'])
                self.assertFalse(any('uim.imsi' in batch for batch in self.batches))

    def test_fallback_uses_only_remaining_budget_and_never_sends_pin_commands(self):
        self.identity_available = False
        self.ef_imsi_available = True
        self.dynamic_cost = 0.1
        self.static_cost = 0.2
        static, _ = self.backend.metrics()
        self.assertEqual(static['dms_value_imsi'], '001010000000001')
        total = self.backend.settings.telemetry.collection_budget
        self.assertAlmostEqual(self.budgets[-1], total - self.dynamic_cost - self.static_cost)
        methods = [item[2] for call in self.backend.session.collect.call_args_list for item in call.args[0]]
        self.assertEqual(set(methods), {'uim_get_imsi', 'get_card_status', 'get_ids',
            'get_software_version', 'get_capabilities', 'read_transparent'})

    def test_fallback_waits_for_next_identity_refresh_when_static_queries_exhaust_budget(self):
        self.identity_available = False
        self.ef_imsi_available = True
        self.static_cost = self.backend.settings.telemetry.collection_budget
        static, _ = self.backend.metrics()
        self.assertIsNone(static['dms_value_imsi'])
        self.assertFalse(any('uim.imsi' in batch for batch in self.batches))
        self.now += 1
        self.static_cost = 0
        self.backend.metrics()
        self.assertFalse(any('uim.imsi' in batch for batch in self.batches))

    def test_unsupported_uim_fallback_leaves_null_identity_and_dynamic_pipeline_healthy(self):
        self.identity_available = False
        static, dynamic = self.backend.metrics()
        self.assertIsNone(static['dms_value_imsi'])
        self.assertEqual(dynamic, {})
        self.assertEqual(self.backend.collection_report['static_requests']['uim.imsi'],
                         {'status': 'error', 'unsupported': True})
        self.assertTrue(self.backend.collection_report['complete'])

    def test_optional_uim_failure_does_not_copy_personal_data_into_error_report(self):
        self.identity_available = False
        collect = self.collect
        def failing(requests, **kwargs):
            if requests and requests[0][0] == 'uim.imsi':
                raise RuntimeError('synthetic identifier: ' + self.imsi)
            return collect(requests, **kwargs)
        self.backend.session.collect.side_effect = failing
        static, _ = self.backend.metrics()
        self.assertIsNone(static['dms_value_imsi'])
        self.assertNotIn(self.imsi, repr(self.backend.collection_report))
        self.assertEqual(self.backend.collection_report['static_requests']['uim.imsi']['status'], 'error')

    def test_expired_identity_is_not_retained_when_dynamic_collection_uses_whole_budget(self):
        self.backend.metrics()
        self.now = 60
        self.dynamic_cost = self.backend.settings.telemetry.collection_budget
        static, _ = self.backend.metrics()
        self.assertIsNone(static['dms_value_imsi'])
        self.assertEqual(sum('dms.imsi' in batch for batch in self.batches), 1)
        self.assertEqual(self.backend.next_sim_identity, 60)

    def test_reopening_a_device_invalidates_sim_and_hardware_caches(self):
        self.backend.metrics()
        device = SimpleNamespace(path='/dev/cdc-wdm2', usb_path='/sys/synthetic', serial='synthetic', enumeration='7')
        with patch('modules.device.backend.discover', return_value=device), \
                patch.object(self.backend, 'enumeration', return_value='7'):
            self.backend.open(device)
        self.assertEqual(self.backend.static, {})
        self.assertEqual(self.backend.next_sim_identity, 0)
        self.assertEqual(self.backend.next_static, 0)
        self.assertEqual(self.backend.generation, 1)


if __name__ == '__main__':
    unittest.main()
