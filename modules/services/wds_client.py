import ipaddress
import logging
from gi.repository import GLib, Qmi
from modules.services.client import optional
from modules.device.query_plan import PRESETS


def requested_settings_mask():
    """Flag type for WDS get_current_settings; libqmi 1.32 renamed it, both carry the same flags."""
    return getattr(Qmi, 'WdsRequestedSettings', None) or Qmi.WdsGetCurrentSettingsRequestedSettings


class WDSClient:
    def __init__(self, session):
        self.session = session
        self.handle = None
        self.reused = False
        self.apn_reported = None
        self.apn_requested = None
        self.apn_verification = None
        self.apn_reported_source = None

    def connected(self):
        output = self.session.request(Qmi.Service.WDS, "get_packet_service_status")
        return output.get_connection_status() == Qmi.WdsConnectionStatus.CONNECTED

    def start(self, apn, *, on_started=None):
        if self.handle is not None:
            raise RuntimeError('An owned bearer has not been confirmed stopped; refusing to start another APN')
        self.reused = False
        self.apn_reported = None
        self.apn_requested = apn
        self.apn_verification = None
        self.apn_reported_source = None
        family = Qmi.MessageWdsSetIpFamilyInput()
        family.set_preference(Qmi.WdsIpFamily.IPV4)
        try:
            self.session.request(Qmi.Service.WDS, 'set_ip_family', family)
        except GLib.Error as error:
            if not any(error.matches(Qmi.protocol_error_quark(), code)
                       for code in (Qmi.ProtocolError.NOEFFECT, Qmi.ProtocolError.NOTSUPPORTED)):
                raise
        value = Qmi.MessageWdsStartNetworkInput()
        value.set_apn(apn)
        value.set_ip_family_preference(Qmi.WdsIpFamily.IPV4)
        value.set_enable_autoconnect(False)
        # Keep this exact WDS client alive until stop. Never allocate a new CID to check it.
        try:
            handle = self.session.request(Qmi.Service.WDS, "start_network", value).get_packet_data_handle()
            if isinstance(handle, bool) or not isinstance(handle, int) or not 0 < handle < 0xFFFFFFFF:
                raise RuntimeError('Modem did not return an owned packet handle; refusing a global stop handle')
            self.handle = handle
        except GLib.Error as error:
            if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT):
                raise
            if not self.connected():
                raise RuntimeError('Start Network returned NoEffect but the IPv4 session is disconnected') from error
            current = self.settings()
            if not current.get('address'):
                raise RuntimeError('Existing data session has no usable IPv4 configuration') from error
            self.verify_apn(current, inherited=True)
            # No owned packet handle was returned. Reuse verified host settings,
            # but never stop an unrelated context with the global handle.
            self.handle = None
            self.reused = True
            logging.getLogger(__name__).warning(
                'Reusing active IPv4 session after NoEffect: address=%s, requested APN=%s, reported APN=%s; '
                'the pre-existing bearer will be retained on shutdown', current['address'], apn,
                self.apn_reported)
            return
        if on_started is not None:
            on_started()
        # A returned handle proves ownership, not that firmware/network honored
        # the requested APN. Check on this same CID before configuring host IP.
        self.verify_apn(self.settings())

    def verify_apn(self, current, *, inherited=False):
        previous = self.apn_reported, self.apn_verification
        reported = current.get('apn')
        self.apn_reported = reported.strip() if isinstance(reported, str) and reported.strip() else None
        self.apn_reported_source = 'current_settings' if self.apn_reported else None
        if self.apn_reported is None:
            self.apn_verification = 'unavailable'
            if inherited:
                raise RuntimeError('Existing data session APN is unavailable; cannot verify the requested APN. '
                                   'Disconnect its owner before changing APN; no foreign bearer was stopped')
            if previous != (None, 'unavailable'):
                logging.getLogger(__name__).warning(
                    'Started owned data session with requested APN=%s; the modem does not report its effective APN '
                    '(APN not independently verified)', self.apn_requested)
        elif self.apn_reported.casefold() != self.apn_requested.casefold():
            self.apn_verification = 'mismatch'
            prefix = 'Existing data session uses APN' if inherited else 'Modem reports APN'
            raise RuntimeError(f'{prefix} {self.apn_reported!r}, requested {self.apn_requested!r}; '
                               'the session was not accepted as the configured APN')
        else:
            self.apn_verification = 'matched'
            if previous != (self.apn_reported, 'matched'):
                logging.getLogger(__name__).info('Effective APN verified: %s', self.apn_reported)

    def stop(self):
        if self.handle is not None:
            value = Qmi.MessageWdsStopNetworkInput()
            value.set_packet_data_handle(self.handle)
            try:
                self.session.request(Qmi.Service.WDS, "stop_network", value)
            except GLib.Error as error:
                if not any(error.matches(Qmi.protocol_error_quark(), code)
                           for code in (Qmi.ProtocolError.NOEFFECT, Qmi.ProtocolError.INVALIDHANDLE,
                                        Qmi.ProtocolError.OUTOFCALL)):
                    raise
            # A timeout/transport failure must preserve ownership for recovery.
            self.handle = None

    def settings(self):
        value = Qmi.MessageWdsGetCurrentSettingsInput()
        mask = requested_settings_mask()
        value.set_requested_settings(mask.IP_ADDRESS | mask.GATEWAY_INFO | mask.DNS_ADDRESS | mask.MTU |
                                     mask.APN_NAME | mask.IP_FAMILY)
        output = self.session.request(Qmi.Service.WDS, "get_current_settings", value)
        def address(getter):
            raw = optional(output, getter)
            return str(ipaddress.IPv4Address(raw)) if raw else None
        return {
            "apn": optional(output, 'get_apn_name'),
            "address": address("get_ipv4_address"),
            "netmask": address("get_ipv4_gateway_subnet_mask"),
            "gateway": address("get_ipv4_gateway_address"),
            "dns": [ip for ip in (address("get_primary_ipv4_dns_address"),
                                   address("get_secondary_ipv4_dns_address")) if ip],
            "mtu": optional(output, "get_mtu"),
        }

    def metrics(self):
        result = {"wds_value_connection_status": "connected" if self.connected() else "disconnected"}
        value = Qmi.MessageWdsGetPacketStatisticsInput()
        value.set_mask(Qmi.WdsPacketStatisticsMaskFlag(1023))
        output = self.session.request(Qmi.Service.WDS, "get_packet_statistics", value)
        result.update(self.statistics(output))
        output = self.session.request(Qmi.Service.WDS, "get_channel_rates")
        result.update(self.rates(output))
        return result

    @staticmethod
    def specifications(groups='full'):
        if isinstance(groups, str):
            groups = PRESETS[groups]
        value = Qmi.MessageWdsGetPacketStatisticsInput()
        value.set_mask(Qmi.WdsPacketStatisticsMaskFlag(1023))
        requests = []
        if 'wds_stats' in groups:
            requests.append(('wds.statistics', Qmi.Service.WDS, 'get_packet_statistics', value, WDSClient.statistics))
        if 'wds_status' in groups:
            requests.append(('wds.status', Qmi.Service.WDS, 'get_packet_service_status', None,
                             lambda output: {'wds_value_connection_status': Qmi.WdsConnectionStatus.get_string(output.get_connection_status())}))
        if 'wds_rates' in groups:
            requests.append(('wds.rates', Qmi.Service.WDS, 'get_channel_rates', None, WDSClient.rates))
        return requests

    @staticmethod
    def statistics(output):
        result = {}
        for name in ('tx_packets_ok', 'rx_packets_ok', 'tx_packets_error', 'rx_packets_error',
                     'tx_overflows', 'rx_overflows', 'tx_bytes_ok', 'rx_bytes_ok',
                     'tx_packets_dropped', 'rx_packets_dropped'):
            value = optional(output, 'get_' + name)
            # libqmi/qmicli defines UINT32_MAX as unavailable for the 32-bit
            # packet counters. Byte counters are 64-bit and retain this value.
            result['wds_value_' + name] = None if not name.endswith('bytes_ok') and value == 0xFFFFFFFF else value
        return result

    @staticmethod
    def rates(output):
        rates = optional(output, 'get_channel_rates')
        if rates is None:
            return {}
        values = [None if rate == 0xFFFFFFFF and index < 2 else rate for index, rate in enumerate(rates)]
        return dict(zip(('wds_value_channel_rates_channel_tx_rate_bps', 'wds_value_channel_rates_channel_rx_rate_bps',
                         'wds_value_channel_rates_max_channel_tx_rate_bps',
                         'wds_value_channel_rates_max_channel_rx_rate_bps'), values))
