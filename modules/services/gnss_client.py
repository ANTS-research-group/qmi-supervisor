"""Optional QMI LOC indications on the existing session/context, without polling.

Message/indication definitions and units:
https://raw.githubusercontent.com/linux-mobile-broadband/libqmi/main/data/qmi-service-loc.json
https://raw.githubusercontent.com/linux-mobile-broadband/libqmi/main/src/qmicli/qmicli-loc.c
No assistance servers, engine locks, RF modes or foreign LOC sessions are changed.
"""
from datetime import datetime, timezone
import logging
import math
import secrets
import time

from gi.repository import GLib, Qmi
from modules.services.client import optional


def numeric(value, minimum=None, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        return None
    return value


class GNSSClient:
    def __init__(self, session, *, clock=time.monotonic, utcnow=None, session_id=None):
        self.session, self.clock = session, clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.session_id = session_id if session_id is not None else secrets.randbelow(255) + 1
        self.client = self.handler = None
        self.attempted = False
        self.record = None
        self.status = 'starting'
        self.error = None

    def start(self):
        try:
            self.client = self.session.client(Qmi.Service.LOC)
            self.handler = self.client.connect('position-report', self.position_report)
            value = Qmi.MessageLocRegisterEventsInput()
            value.set_event_registration_mask(Qmi.LocEventRegistrationFlag.POSITION_REPORT)
            self.session.request(Qmi.Service.LOC, 'register_events', value)
            value = Qmi.MessageLocStartInput()
            value.set_session_id(self.session_id)
            value.set_fix_recurrence_type(Qmi.LocFixRecurrenceType.PERIODIC_FIXES)
            value.set_intermediate_report_state(Qmi.LocIntermediateReportState.DISABLE)
            value.set_minimum_interval_between_position_reports(1000)
            # Retain ownership even if the response is lost after activation.
            self.attempted = True
            self.status = 'no_fix'
            self.session.request(Qmi.Service.LOC, 'start', value)
        except Exception as error:
            unsupported = isinstance(error, (AttributeError, ValueError)) or (
                isinstance(error, GLib.Error) and (
                    error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOTSUPPORTED) or
                    error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.INVALIDSERVICETYPE) or
                    error.matches(Qmi.core_error_quark(), Qmi.CoreError.UNSUPPORTED)))
            self.status = 'unsupported' if unsupported else 'error'
            self.error = ('The modem does not support GPS through QMI LOC.' if unsupported
                          else 'Could not start the modem GPS.')
            logging.getLogger(__name__).warning('%s (%s)', self.error, type(error).__name__)

    def position_report(self, client, output):
        # Signals are handled on the same GLib context as the radio batch.
        if not self.attempted or optional(output, 'get_session_id') != self.session_id:
            return
        self.record = None
        self.error = None
        self.status = 'no_fix'
        if optional(output, 'get_session_status') != Qmi.LocSessionStatus.SUCCESS:
            return
        latitude = numeric(optional(output, 'get_latitude'), -90, 90)
        longitude = numeric(optional(output, 'get_longitude'), -180, 180)
        stamp = numeric(optional(output, 'get_utc_timestamp'), 1)
        technology = optional(output, 'get_technology_used')
        # Cellular/injected locations must not be presented as satellite fixes.
        if latitude is None or longitude is None or technology is None or not int(technology) & int(Qmi.LocTechnologyUsed.SATELLITE):
            return
        if stamp is None:
            return
        try:
            received = self.utcnow()
            source = datetime.fromtimestamp(stamp / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return
        # An implausible future timestamp is not fresh, even if just received.
        if (source - received).total_seconds() > 2:
            return
        values = {'gnss_latitude': latitude, 'gnss_longitude': longitude,
                  'gnss_timestamp_utc': source.isoformat()}
        for field, getter, minimum, maximum in (
                ('gnss_altitude_msl_m', 'get_altitude_from_sealevel', None, None),
                ('gnss_altitude_ellipsoid_m', 'get_altitude_from_ellipsoid', None, None),
                ('gnss_horizontal_uncertainty_m', 'get_horizontal_uncertainty_circular', 0, None),
                ('gnss_horizontal_confidence_percent', 'get_horizontal_confidence', 0, 100),
                ('gnss_horizontal_speed_mps', 'get_horizontal_speed', 0, None)):
            value = numeric(optional(output, getter), minimum, maximum)
            if value is not None:
                values[field] = value
        self.record = {'values': values, 'source_epoch': stamp / 1000,
                       'source_age_at_receipt': max(0, received.timestamp() - stamp / 1000),
                       'received_monotonic': self.clock(), 'received_at_utc': received.isoformat()}
        self.status = 'fixed'

    def snapshot(self, max_age):
        report = {'enabled': True, 'source': 'qmi_loc', 'status': self.status,
                  'error': self.error, 'age_ms': None}
        if self.record is None:
            return {}, report
        record = self.record
        elapsed = max(0, self.clock() - record['received_monotonic'])
        age = max(record['source_age_at_receipt'] + elapsed,
                  self.utcnow().timestamp() - record['source_epoch'])
        report.update(age_ms=age * 1000, source_time=record['values']['gnss_timestamp_utc'],
                      received_at_utc=record['received_at_utc'])
        if age > max_age:
            report['status'] = 'stale'
            return {}, report
        return dict(record['values']), report

    def close(self):
        try:
            if self.attempted:
                value = Qmi.MessageLocStopInput()
                value.set_session_id(self.session_id)
                try:
                    self.session.request(Qmi.Service.LOC, 'stop', value)
                except Exception as error:
                    logging.getLogger(__name__).warning('Could not confirm owned GPS session stop (%s)', type(error).__name__)
        finally:
            self.attempted = False
            if self.client is not None and self.handler is not None:
                self.client.disconnect(self.handler)
            self.handler = self.client = self.record = None
