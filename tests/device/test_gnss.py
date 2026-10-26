"""Typed optional GNSS sessions and fix validity, without any hardware."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

try:
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import GLib, Qmi
    from modules.services.gnss_client import GNSSClient
    from modules.device.backend import QmiBackend
    from modules.runtime.settings import Settings
except (ImportError, ValueError):
    GNSSClient = None


@unittest.skipIf(GNSSClient is None, 'Requires OS libqmi GI bindings')
class GNSSClientTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.monotonic = 10
        self.session = Mock()
        self.session.client.return_value.connect.return_value = 123
        self.gnss = GNSSClient(self.session, session_id=7, clock=lambda: self.monotonic, utcnow=lambda: self.now)

    def report(self, **changes):
        values = dict(session_id=7, session_status=Qmi.LocSessionStatus.SUCCESS,
                      latitude=0.0, longitude=0.0, utc_timestamp=int(self.now.timestamp()*1000),
                      technology_used=Qmi.LocTechnologyUsed.SATELLITE,
                      horizontal_uncertainty_circular=2.5, horizontal_confidence=68,
                      horizontal_speed=0, altitude_from_sealevel=-1)
        values.update(changes)
        return SimpleNamespace(**{'get_'+key:lambda v=value:v for key,value in values.items()})

    def test_start_uses_owned_session_and_indications_without_extra_polling(self):
        self.gnss.start()
        self.assertEqual([c.args[1] for c in self.session.request.call_args_list], ['register_events','start'])
        value = self.session.request.call_args.args[2]
        self.assertEqual(value.get_session_id(),7)
        self.assertEqual(value.get_fix_recurrence_type(), Qmi.LocFixRecurrenceType.PERIODIC_FIXES)
        self.assertEqual(value.get_minimum_interval_between_position_reports(), 1000)
        self.assertEqual(value.get_intermediate_report_state(),Qmi.LocIntermediateReportState.DISABLE)
        self.gnss.snapshot(10)
        self.assertEqual(self.session.request.call_count,2)
        self.gnss.close()
        self.assertEqual(self.session.request.call_args.args[1],'stop')
        self.assertEqual(self.session.request.call_args.args[2].get_session_id(),7)
        self.session.client.return_value.disconnect.assert_called_once_with(123)

    def test_zero_coordinates_speed_and_source_time_are_valid(self):
        self.gnss.start()
        self.gnss.position_report(None,self.report())
        values, status = self.gnss.snapshot(10)
        self.assertEqual(values['gnss_latitude'],0)
        self.assertEqual(values['gnss_longitude'],0)
        self.assertEqual(values['gnss_horizontal_speed_mps'],0)
        self.assertEqual(values['gnss_horizontal_uncertainty_m'],2.5)
        self.assertEqual(values['gnss_horizontal_confidence_percent'],68)
        self.assertEqual(values['gnss_altitude_msl_m'],-1)
        self.assertNotIn('gnss_altitude_ellipsoid_m',values)
        self.assertEqual(values['gnss_timestamp_utc'],self.now.isoformat())
        self.assertEqual((status['status'],status['age_ms']),('fixed',0))

    def test_foreign_session_reports_cannot_replace_our_fix(self):
        self.gnss.start()
        self.gnss.position_report(None,self.report())
        self.gnss.position_report(None,self.report(session_id=8,latitude=30))
        values,_ = self.gnss.snapshot(10)
        self.assertEqual(values['gnss_latitude'],0)

    def test_first_fix_during_start_response_keeps_fixed_status(self):
        def request(service, method, value):
            if method == 'start':
                self.gnss.position_report(None, self.report())
        self.session.request.side_effect=request
        self.gnss.start()
        self.assertEqual(self.gnss.snapshot(10)[1]['status'],'fixed')

    def test_backward_clock_step_does_not_rejuvenate_an_old_fix(self):
        self.gnss.start()
        self.gnss.position_report(None,self.report(utc_timestamp=int((self.now.timestamp()-9)*1000)))
        self.monotonic+=2
        self.now-=timedelta(seconds=10)
        values,status=self.gnss.snapshot(10)
        self.assertEqual(values,{})
        self.assertEqual(status['age_ms'],11000)
        self.assertEqual(status['status'],'stale')

    def test_new_no_fix_report_retires_previous_coordinates(self):
        self.gnss.start()
        self.gnss.position_report(None,self.report())
        self.gnss.position_report(None,self.report(session_status=Qmi.LocSessionStatus.IN_PROGRESS))
        values,status = self.gnss.snapshot(10)
        self.assertEqual(values,{})
        self.assertEqual(status['status'],'no_fix')

    def test_cellular_or_injected_positions_are_not_satellite_fixes(self):
        self.gnss.start()
        for technology in (None,Qmi.LocTechnologyUsed.CELLULAR,Qmi.LocTechnologyUsed.INJECTED_POSITION):
            self.gnss.position_report(None,self.report(technology_used=technology))
            self.assertEqual(self.gnss.snapshot(10)[0],{})

    def test_missing_invalid_and_future_fix_fields_never_become_zeroes(self):
        self.gnss.start()
        for change in ({'latitude':float('nan')},{'latitude':91},{'longitude':181},
                       {'longitude':False},{'utc_timestamp':None},{'utc_timestamp':float('inf')},
                       {'utc_timestamp':int((self.now.timestamp()+20)*1000)}):
            with self.subTest(change=change):
                self.gnss.position_report(None,self.report(**change))
                self.assertEqual(self.gnss.snapshot(10)[0],{})

    def test_stale_receiver_timestamp_and_monotonic_age_both_expire(self):
        self.gnss.start()
        self.gnss.position_report(None,self.report(utc_timestamp=int((self.now.timestamp()-30)*1000)))
        self.assertEqual(self.gnss.snapshot(10)[1]['status'],'stale')
        self.gnss.position_report(None,self.report())
        self.monotonic+=11
        values,status=self.gnss.snapshot(10)
        self.assertEqual(values,{})
        self.assertEqual(status['status'],'stale')

    def test_unsupported_gps_does_not_raise_or_change_data_session(self):
        self.session.client.side_effect=GLib.Error.new_literal(Qmi.protocol_error_quark(),'Unsupported',Qmi.ProtocolError.NOTSUPPORTED)
        self.gnss.start()
        values,status=self.gnss.snapshot(10)
        self.assertEqual(values,{})
        self.assertEqual(status['status'],'unsupported')
        self.gnss.close()
        self.session.request.assert_not_called()

    def test_lost_start_response_retains_only_our_cleanup_handle(self):
        self.session.request.side_effect=[SimpleNamespace(),TimeoutError('Start timed out'),SimpleNamespace()]
        self.gnss.start()
        self.assertEqual(self.gnss.snapshot(10)[1]['status'],'error')
        self.gnss.close()
        self.assertEqual(self.session.request.call_args.args[1],'stop')
        self.assertEqual(self.session.request.call_args.args[2].get_session_id(),7)

    def test_service_unavailable_and_core_errors_use_installed_enum_names(self):
        for domain, code, expected in (
                (Qmi.protocol_error_quark(), Qmi.ProtocolError.INVALIDSERVICETYPE, 'unsupported'),
                (Qmi.core_error_quark(), Qmi.CoreError.UNSUPPORTED, 'unsupported'),
                (Qmi.protocol_error_quark(), Qmi.ProtocolError.INTERNAL, 'error')):
            with self.subTest(code=code):
                gnss = GNSSClient(self.session)
                self.session.client.side_effect = GLib.Error.new_literal(domain, 'LOC unavailable', code)
                gnss.start()
                self.assertEqual(gnss.snapshot(10)[1]['status'], expected)
                gnss.close()

    def test_disabled_or_gpsd_source_does_not_allocate_loc(self):
        backend=QmiBackend(Settings(),threading.Event())
        backend.session=Mock()
        for enabled,gps,source in ((True,False,'modem'),(False,True,'modem'),(True,True,'gpsd')):
            backend.settings=replace(backend.settings,telemetry=replace(backend.settings.telemetry,enabled=enabled,gps=gps,gps_source=source))
            backend.start_gnss()
        backend.session.client.assert_not_called()

    def test_modem_source_start_has_a_separate_bounded_setup_budget(self):
        backend=QmiBackend(Settings(),threading.Event())
        backend.settings=replace(backend.settings,telemetry=replace(backend.settings.telemetry,gps=True,gps_source='modem'))
        backend.session=Mock(collection_deadline=None)
        backend.start_gnss()
        self.assertIsInstance(backend.gnss,GNSSClient)
        self.assertIsNone(backend.session.collection_deadline)
        self.assertEqual([c.args[1] for c in backend.session.request.call_args_list],['register_events','start'])
