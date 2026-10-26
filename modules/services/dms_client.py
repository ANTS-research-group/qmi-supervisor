import logging
import re
from gi.repository import GLib, Qmi
from modules.services.client import optional


class SimNotReady(RuntimeError):
    """No card is present; slots lists 1-based slots whose card is not present."""
    def __init__(self, message, slots):
        super().__init__(message)
        self.slots = slots


class DMSClient:
    SIM_IDENTITY_REFRESH_SECONDS = 60

    def __init__(self, session):
        self.session = session

    @staticmethod
    def card_summary(cards):
        """Describe each slot without exposing card identifiers or sending PINs."""
        def label(enum, value):
            try:
                return enum.get_string(value) or str(value)
            except (TypeError, ValueError):
                return str(value)
        slots = []
        for index, card in enumerate(cards, 1):
            state = label(Qmi.UimCardState, card.card_state)
            if card.card_state == Qmi.UimCardState.ERROR:
                state += ' (' + label(Qmi.UimCardError, getattr(card, 'error_code', 0)) + ')'
            slots.append(f'slot {index}: {state}')
        return '; '.join(slots) or 'UIM returned no card slots'

    def prepare(self):
        mode = self.session.request(Qmi.Service.DMS, "get_operating_mode").get_mode()
        if mode != Qmi.DmsOperatingMode.ONLINE:
            value = Qmi.MessageDmsSetOperatingModeInput()
            value.set_mode(Qmi.DmsOperatingMode.ONLINE)
            self.session.request(Qmi.Service.DMS, "set_operating_mode", value)
        # Read only: PIN retries must never be consumed by recovery loops.
        # The cards and nested applications are borrowed from this output
        # (transfer-none). Retain the owner until every struct has been read.
        card_output = self.session.request(Qmi.Service.UIM, "get_card_status")
        cards = card_output.get_card_status()[-1]
        if not any(card.card_state == Qmi.UimCardState.PRESENT for card in cards):
            raise SimNotReady("SIM absent or not ready; " + self.card_summary(cards),
                              [index for index, _ in enumerate(cards, 1)] or [1])
        for card in cards:
            if card.card_state != Qmi.UimCardState.PRESENT:
                continue
            for app in card.applications:
                state = card.upin_state if app.upin_replaces_pin1 else app.pin1_state
                if state in {Qmi.UimPinState.ENABLED_NOT_VERIFIED, Qmi.UimPinState.BLOCKED,
                             Qmi.UimPinState.PERMANENTLY_BLOCKED}:
                    raise RuntimeError("SIM requires PIN/PUK; unlock it before starting the supervisor")

    def power_cycle_sim(self, slot):
        """Make the UIM read the card again, e.g. after a hot swap the firmware did not detect."""
        value = Qmi.MessageUimPowerOffSimInput()
        value.set_slot(slot)
        try:
            self.session.request(Qmi.Service.UIM, 'power_off_sim', value)
        except GLib.Error as error:
            # A card in error may already be powered down; power on regardless.
            if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT):
                logging.getLogger(__name__).info('SIM slot %d power off failed: %s', slot, error)
        value = Qmi.MessageUimPowerOnSimInput()
        value.set_slot(slot)
        try:
            self.session.request(Qmi.Service.UIM, 'power_on_sim', value)
        except GLib.Error as error:
            if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT):
                raise

    def reset(self):
        """Reboot the modem firmware. The USB device disappears and enumerates again."""
        for mode in (Qmi.DmsOperatingMode.OFFLINE, Qmi.DmsOperatingMode.RESET):
            value = Qmi.MessageDmsSetOperatingModeInput()
            value.set_mode(mode)
            try:
                self.session.request(Qmi.Service.DMS, 'set_operating_mode', value)
            except GLib.Error as error:
                if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT):
                    raise
            except TimeoutError:
                # The modem may reboot before answering RESET.
                if mode != Qmi.DmsOperatingMode.RESET:
                    raise
    def metrics(self):
        result = {'dms_value_imsi': None}
        for key, service, name, value, parser in self.specifications():
            try:
                result.update(parser(self.session.request(service, name, value)))
            except Exception as error:
                # Identity responses and firmware errors may contain personal
                # identifiers. Method/type are sufficient for this optional sink.
                logging.getLogger(__name__).debug("Optional DMS metric %s unavailable (%s)", name, type(error).__name__)
        return result

    @staticmethod
    def imsi(output):
        # Keep leading zeroes, do not derive it from IMEI or serving PLMN, and
        # never include an invalid response value in an exception or log.
        value = optional(output, 'get_imsi')
        return {'dms_value_imsi': value if isinstance(value, str) and re.fullmatch(r'[0-9]{5,15}', value) else None}

    @staticmethod
    def decode_ef_imsi(data):
        """Decode EF_IMSI without guessing missing length/parity/digits.

        ETSI TS 131 102 4.2.2: one length octet, identity/parity nibble,
        low-nibble-first BCD digits, and F-filled unused nibbles. See also
        TS 124 008 10.5.1.4 for the odd/even indication.
        """
        if not isinstance(data, (bytes, bytearray, list, tuple)) or not 2 <= len(data) <= 9:
            return None
        if any(type(value) is not int or not 0 <= value <= 255 for value in data):
            return None
        length = data[0]
        if not 1 <= length <= 8 or length + 1 > len(data):
            return None
        if any(value != 0xFF for value in data[length + 1:]):
            return None
        parity = data[1] & 0x0F
        if parity not in (1, 9):
            return None
        digits = [data[1] >> 4]
        for value in data[2:length + 1]:
            digits.extend((value & 0x0F, value >> 4))
        if parity == 1:
            if digits[-1] != 0x0F:
                return None
            digits.pop()
        if any(value > 9 for value in digits) or not 5 <= len(digits) <= 15:
            return None
        if len(digits) % 2 != (1 if parity == 9 else 0):
            return None
        return ''.join(map(str, digits))

    @staticmethod
    def uim_imsi(output):
        card_result = optional(output, 'get_card_result')
        if card_result is not None:
            if (not isinstance(card_result, (tuple, list)) or len(card_result) != 2
                    or not (tuple(card_result) == (0x90, 0) or card_result[0] == 0x91)):
                return {'dms_value_imsi': None}
        return {'dms_value_imsi': DMSClient.decode_ef_imsi(optional(output, 'get_read_result'))}

    @staticmethod
    def uim_imsi_specification(card_output):
        """Read the selected present primary GW card unless explicitly locked.

        Read-only protocol/path follow ModemManager's uim_read/uim_get_imsi:
        https://raw.githubusercontent.com/linux-mobile-broadband/ModemManager/main/src/mm-sim-qmi.c
        MF/ADF file path identifiers are supplied as little-endian bytes.
        Some GI versions cannot marshal nested applications. Missing metadata
        must not turn a present card into an absent/locked card; the UIM service
        still enforces file access conditions without consuming PIN retries.
        """
        if card_output is None:
            return None
        primary, _, _, _, cards = card_output.get_card_status()
        if type(primary) is not int or not 0 <= primary < 0xFFFF:
            return None
        slot, application = primary >> 8, primary & 0xFF
        if slot >= len(cards):
            return None
        card = cards[slot]
        if card.card_state != Qmi.UimCardState.PRESENT:
            return None
        applications = getattr(card, 'applications', ())
        if applications:
            if application >= len(applications):
                return None
            app = applications[application]
            if getattr(app, 'type', None) not in {None, Qmi.UimCardApplicationType.UNKNOWN,
                    Qmi.UimCardApplicationType.SIM, Qmi.UimCardApplicationType.USIM}:
                return None
            if getattr(app, 'state', None) in {
                    Qmi.UimCardApplicationState.PIN1_OR_UPIN_PIN_REQUIRED,
                    Qmi.UimCardApplicationState.PUK1_OR_UPIN_PUK_REQUIRED,
                    Qmi.UimCardApplicationState.PIN1_BLOCKED}:
                return None
            pin = (getattr(card, 'upin_state', None) if getattr(app, 'upin_replaces_pin1', False)
                   else getattr(app, 'pin1_state', None))
            if pin in {Qmi.UimPinState.ENABLED_NOT_VERIFIED, Qmi.UimPinState.BLOCKED,
                       Qmi.UimPinState.PERMANENTLY_BLOCKED}:
                return None
        value = Qmi.MessageUimReadTransparentInput()
        value.set_session(Qmi.UimSessionType.PRIMARY_GW_PROVISIONING, [])
        value.set_file(0x6F07, [0x00, 0x3F, 0xFF, 0x7F])
        value.set_read_information(0, 0)
        return ('uim.imsi', Qmi.Service.UIM, 'read_transparent', value, DMSClient.uim_imsi)

    @staticmethod
    def card_identity_status(output):
        """Copy non-identifying UIM status while its borrowed structs are alive."""
        status = output.get_card_status()
        primary, cards = status[0], status[-1]
        present = [(index, card) for index, card in enumerate(cards)
                   if card.card_state == Qmi.UimCardState.PRESENT]
        if not present:
            return {'state': 'absent' if not cards or all(card.card_state == Qmi.UimCardState.ABSENT for card in cards)
                    else 'not_ready', 'slot': None}
        selected = primary >> 8 if primary != 0xFFFF else None
        if selected is None or selected >= len(cards):
            selected = present[0][0] if len(present) == 1 else None
        if selected is None:
            return {'state': 'unknown', 'slot': None}
        card = cards[selected]
        if card.card_state != Qmi.UimCardState.PRESENT:
            return {'state': 'not_ready', 'slot': selected + 1}
        applications = card.applications
        if not applications:
            return {'state': 'unknown', 'slot': selected + 1}
        application = primary & 0xFF if primary != 0xFFFF else None
        relevant = [applications[application]] if application is not None and application < len(applications) else applications
        for app in relevant:
            pin = (getattr(card, 'upin_state', None) if getattr(app, 'upin_replaces_pin1', False)
                   else getattr(app, 'pin1_state', None))
            if (pin in {Qmi.UimPinState.ENABLED_NOT_VERIFIED, Qmi.UimPinState.BLOCKED,
                        Qmi.UimPinState.PERMANENTLY_BLOCKED}
                    or getattr(app, 'state', None) in {
                        Qmi.UimCardApplicationState.PIN1_OR_UPIN_PIN_REQUIRED,
                        Qmi.UimCardApplicationState.PUK1_OR_UPIN_PUK_REQUIRED,
                        Qmi.UimCardApplicationState.PIN1_BLOCKED}):
                return {'state': 'pin_required', 'slot': selected + 1}
        return {'state': 'ready', 'slot': selected + 1}

    @staticmethod
    def sim_specifications():
        # Read-only identity/status. The backend schedules these at 60 seconds,
        # separately from the 1 Hz moving-radio measurements and hardware cache.
        return [('dms.imsi', Qmi.Service.DMS, 'uim_get_imsi', None, DMSClient.imsi),
                ('uim.identity_status', Qmi.Service.UIM, 'get_card_status', None,
                 lambda output: {'dms_value_sim_card_status': DMSClient.card_identity_status(output)})]

    @staticmethod
    def specifications(include_sim=True):
        def ids(output):
            return {'dms_value_' + name: optional(output, 'get_' + name) for name in ('imei', 'meid', 'esn')}
        def capabilities(output):
            tx, rx, data, sim, radios = output.get_info()
            return {'dms_value_info_max_tx_channel_rate': tx, 'dms_value_info_max_rx_channel_rate': rx,
                    'dms_value_info_data_service_capability': Qmi.DmsDataServiceCapability.get_string(data),
                    'dms_value_info_sim_capability': Qmi.DmsSimCapability.get_string(sim),
                    'dms_value_info_radio_interface_list': ', '.join(Qmi.DmsRadioInterface.get_string(i) for i in radios)}
        hardware = [('dms.ids', Qmi.Service.DMS, 'get_ids', None, ids),
                ('dms.version', Qmi.Service.DMS, 'get_software_version', None,
                 lambda output: {'dms_value_imei_software_version': optional(output, 'get_version')}),
                ('dms.capabilities', Qmi.Service.DMS, 'get_capabilities', None, capabilities)]
        return DMSClient.sim_specifications()[:1] + hardware if include_sim else hardware
