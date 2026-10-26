"""Registration observations use a bounded read batch and never activate WDS."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from modules.runtime.settings import Settings

try:
    from gi.repository import Qmi
    from modules.device.backend import QmiBackend
    from modules.services.nas_client import NASClient
except (ImportError, ValueError):
    QmiBackend = None


@unittest.skipIf(QmiBackend is None, 'Requires libqmi GI')
class RegistrationObservationTests(unittest.TestCase):
    @staticmethod
    def serving(registered=True):
        return SimpleNamespace(get_serving_system=lambda: (
            Qmi.NasRegistrationState.REGISTERED if registered else Qmi.NasRegistrationState.NOT_REGISTERED,
            Qmi.NasAttachState.DETACHED,
            Qmi.NasAttachState.ATTACHED if registered else Qmi.NasAttachState.DETACHED,
            Qmi.NasNetworkType(0), [Qmi.NasRadioInterface(12)]))

    @staticmethod
    def cell():
        return SimpleNamespace(get_nr5g_cell_information=lambda:
            ([0x99, 0xF9, 0x01], [0, 0, 1], 1, 2, -123, -987, 143))

    def test_waiting_fast_profile_acquires_identity_and_nas_once_without_wds(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(Settings(), modem=replace(Settings().modem, plmn='99910'),
                telemetry=replace(Settings().telemetry, profile='fast', collection_budget=.4))
            backend = QmiBackend(settings, threading.Event(), state_path=Path(directory)/'state.json')
            backend.observation_state = 'WAIT_REGISTRATION'
            backend.nas.target_plmn = '99910'
            backend.session = Mock()
            backend.nas.session = backend.session
            backend.dms.session = backend.session
            backend.dms.specifications = Mock(return_value=[
                ('dms.identity', Qmi.Service.DMS, 'get_ids', None,
                 lambda output: {'dms_value_imei': 'test-imei'})])
            backend.dms.sim_specifications = Mock(return_value=[
                ('uim.identity', Qmi.Service.UIM, 'get_card_status', None,
                 lambda output: {'dms_value_imsi': '001010123456789',
                                 'dms_value_sim_card_status': {'state': 'ready'}})])
            backend.wds.specifications = Mock(side_effect=AssertionError('No data session before registration'))
            backend.wds.reused = True
            backend.wds.apn_reported = 'previous-session.apn'
            backend.wds.apn_verification = 'matched'
            backend.wds.apn_reported_source = 'current_settings'
            serving, cell = self.serving(), self.cell()
            responses = {'get_serving_system': serving, 'get_cell_location_info': cell,
                'get_signal_strength': SimpleNamespace(get_signal_strength=lambda: (-71, Qmi.NasRadioInterface.LTE))}

            def collect(requests, **kwargs):
                outputs = {key: responses.get(method, SimpleNamespace()) for key, service, method, value in requests}
                return outputs, {'requests': {key: {'status': 'ok', 'method': method}
                    for key, service, method, value in requests}}

            backend.session.collect.side_effect = collect
            static, dynamic = backend.metrics()
            backend.session.collect.assert_called_once()
            requests = backend.session.collect.call_args.args[0]
            self.assertFalse(any(service == Qmi.Service.WDS for key, service, method, value in requests))
            self.assertTrue({Qmi.Service.NAS, Qmi.Service.DMS, Qmi.Service.UIM}
                            <= {service for key, service, method, value in requests})
            self.assertEqual(backend.session.collect.call_args.kwargs['budget'], .4)
            self.assertEqual(static['dms_value_imei'], 'test-imei')
            self.assertEqual(static['dms_value_imsi'], '001010123456789')
            self.assertEqual(dynamic['nas_value_signal_strength_strength'], -71)
            self.assertEqual(dynamic['nas_value_serving_system_registration_state'], 'registered')
            self.assertFalse(any(key.startswith('wds_value_') for key in dynamic))
            self.assertEqual(backend.collection_report['observation_state'], 'WAIT_REGISTRATION')
            self.assertEqual(backend.collection_report['connection']['phase'], 'WAIT_REGISTRATION')
            self.assertFalse(backend.collection_report['connection']['reused_existing_bearer'])
            self.assertIsNone(backend.collection_report['connection']['reported_apn'])
            self.assertIsNone(backend.collection_report['connection']['apn_verification'])
            self.assertIsNone(backend.collection_report['connection']['reported_apn_source'])
            self.assertTrue(backend.registered())
            backend.session.request.assert_not_called()  # serving + PLMN came from the same batch
            backend.session.request.return_value = serving
            backend.registered()
            self.assertTrue(backend.session.request.called)  # stale observations cannot be consumed twice

    def test_cached_serving_and_cell_confirm_plmn_without_duplicate_requests(self):
        session = Mock()
        nas = NASClient(session)
        nas.target_plmn = '99910'
        self.assertTrue(nas.registered(observations={
            'get_serving_system': self.serving(), 'get_cell_location_info': self.cell()},
            attempted={'get_serving_system', 'get_cell_location_info'}))
        session.request.assert_not_called()

    def test_failed_bounded_requests_are_not_retried_outside_the_observation_budget(self):
        session = Mock()
        nas = NASClient(session)
        self.assertFalse(nas.registered(observations={},
            attempted={'get_serving_system', 'get_system_info', 'get_cell_location_info'}))
        session.request.assert_not_called()

    def test_cached_sa_fallback_does_not_issue_a_second_serving_or_system_query(self):
        session = Mock()
        nas = NASClient(session)
        system = SimpleNamespace(get_nr5g_service_status_info=lambda: (Qmi.NasServiceStatus.AVAILABLE, 0, True),
            get_nr5g_system_info=lambda: (True, Qmi.NasNetworkServiceDomain.PS,
                True, Qmi.NasNetworkServiceDomain.PS, False, 0, True, False))
        self.assertTrue(nas.registered(observations={
            'get_serving_system': self.serving(registered=False), 'get_system_info': system},
            attempted={'get_serving_system', 'get_system_info'}))
        session.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
