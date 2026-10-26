"""Optional QMI indications that replace polled query groups on the owned session.

USB modems answer QMI requests serially, one interrupt-endpoint poll (~32 ms)
per response, so values the modem pushes by itself shorten every sample.
libqmi 1.30.4 offers complete indications only for WDS packet statistics, WDS
bearer status and the NAS serving system. Band and channel-rate indications
lack fields of their polled responses, and signal/cell information has no NR5G
indication; those groups remain polled.

Indications arrive on the session's GLib context, which is dispatched between
and during acquisitions, so no thread touches the QMI session.
"""
from datetime import datetime, timezone
import logging
import time

from gi.repository import GLib, Qmi
from modules.services.nas_client import NASClient
from modules.services.wds_client import WDSClient


def bearer_status(output):
    # The indication adds a reconfiguration flag to the polled status.
    status = output.get_connection_status()[0]
    return {'wds_value_connection_status': Qmi.WdsConnectionStatus.get_string(status)}


class Indications:
    # group: (service, GI signal, parser, periodic). Periodic groups are pushed
    # at a fixed rate; the others only on change and are confirmed by polling.
    GROUPS = {'wds_stats': (Qmi.Service.WDS, 'event-report', WDSClient.statistics, True),
              'wds_status': (Qmi.Service.WDS, 'packet-service-status', bearer_status, False),
              'serving': (Qmi.Service.NAS, 'serving-system', NASClient.serving, False)}
    REFRESH = 60

    def __init__(self, session, *, clock=time.monotonic, utcnow=None):
        self.session, self.clock = session, clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.handlers = []
        self.reset()

    def reset(self):
        """Forget registrations; a reopened session has new clients."""
        self.disconnect()
        self.clients, self.records, self.status, self.errors = {}, {}, {}, {}
        self.statistics_interval = None

    def disconnect(self):
        for client, handler in self.handlers:
            try:
                client.disconnect(handler)
            except Exception:
                pass
        self.handlers = []

    def ensure(self, interval):
        """Register on existing clients; each registration is one QMI request per client."""
        for service in (Qmi.Service.WDS, Qmi.Service.NAS):
            client = self.session.clients.get(service)
            if client is None or self.clients.get(service) is client:
                continue
            self.clients[service] = client
            groups = [group for group, item in self.GROUPS.items() if item[0] == service]
            for group in groups:
                # Observations of a replaced client must not be served as current.
                self.records.pop(group, None)
                self.handlers.append((client, client.connect(
                    self.GROUPS[group][1], lambda source, output, group=group: self.received(group, output))))
            try:
                if service == Qmi.Service.WDS:
                    self.statistics_interval = max(1, round(interval))
                    value = Qmi.MessageWdsSetEventReportInput()
                    value.set_transfer_statistics(self.statistics_interval,
                                                  Qmi.WdsSetEventReportTransferStatistics(1023))
                    self.session.request(service, 'set_event_report', value)
                else:
                    value = Qmi.MessageNasRegisterIndicationsInput()
                    value.set_serving_system_events(True)
                    self.session.request(service, 'register_indications', value)
            except Exception as error:
                unsupported = isinstance(error, GLib.Error) and (
                    error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOTSUPPORTED) or
                    error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.INVALIDQMICOMMAND))
                for group in groups:
                    self.status[group] = 'unsupported' if unsupported else 'error'
                    self.errors[group] = str(error)[:256]
                logging.getLogger(__name__).warning('QMI indications for %s unavailable; polling instead (%s)',
                                                    ', '.join(groups), type(error).__name__)
            else:
                for group in groups:
                    self.status[group] = 'active'
                    self.errors.pop(group, None)

    def received(self, group, output):
        try:
            values = self.GROUPS[group][2](output)
        except Exception as error:
            self.errors[group] = 'Unparseable indication: ' + str(error)[:200]
            return
        self.observe(group, values, source='indication')

    def observe(self, group, values, *, source='poll'):
        """A polled response also confirms an on-change group's current value."""
        self.records[group] = dict(values=dict(values), source=source, received_monotonic=self.clock(),
                                   received_at_utc=self.utcnow().isoformat())

    def fresh(self, group, interval):
        """Latest observation if indications keep it current, otherwise None (poll instead)."""
        record = self.records.get(group)
        if self.status.get(group) != 'active' or record is None:
            return None
        age = self.clock() - record['received_monotonic']
        periodic = self.GROUPS[group][3]
        limit = 2 * max(interval, self.statistics_interval or 1) if periodic else self.REFRESH
        if periodic and record['source'] != 'indication':
            return None
        return record if age < limit else None

    def report(self, mode, served=(), shadow=None):
        now = self.clock()
        groups = {}
        for group in self.GROUPS:
            record = self.records.get(group)
            groups[group] = dict(status=self.status.get(group, 'pending'), error=self.errors.get(group),
                                 served=group in served,
                                 source=record['source'] if record else None,
                                 received_at_utc=record['received_at_utc'] if record else None,
                                 age_ms=max(0, now - record['received_monotonic']) * 1000 if record else None)
            if shadow and group in shadow:
                groups[group]['shadow'] = shadow[group]
        return {'mode': mode, 'groups': groups}

    def close(self):
        """Stop periodic statistics on a retained WDS CID; release drops the rest."""
        try:
            if self.status.get('wds_stats') == 'active' and self.clients.get(Qmi.Service.WDS) is not None:
                value = Qmi.MessageWdsSetEventReportInput()
                value.set_transfer_statistics(0, Qmi.WdsSetEventReportTransferStatistics(0))
                try:
                    self.session.request(Qmi.Service.WDS, 'set_event_report', value)
                except Exception as error:
                    logging.getLogger(__name__).warning('Could not stop WDS statistics indications (%s)',
                                                        type(error).__name__)
        finally:
            self.reset()
