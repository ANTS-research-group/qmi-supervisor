import logging
import re
from gi.repository import GLib, Qmi
from modules.runtime import diagnosis
from modules.services.client import optional


class SimNotReady(RuntimeError):
    """No card is present; slots lists 1-based slots whose card is not present."""
    def __init__(self, message, slots, report=None, code=None):
        super().__init__(message)
        self.slots = slots
        self.report = report
        self.code = code


class SimLocked(RuntimeError):
    """A card is present but needs a PIN/PUK the supervisor never enters."""
    def __init__(self, message, report=None, code=None):
        super().__init__(message)
        self.report = report
        self.code = code


class DMSClient:
    SIM_IDENTITY_REFRESH_SECONDS = 60

    def __init__(self, session):
        self.session = session
        self.offline = None
        self.offline_logged = False

    @staticmethod
    def card_summary(cards):
        """Describe each slot without exposing card identifiers or sending PINs."""
        slots = []
        for index, card in enumerate(cards, 1):
            state = DMSClient._enum_label(Qmi.UimCardState, getattr(card, 'card_state', 0))
            if getattr(card, 'card_state', None) == Qmi.UimCardState.ERROR:
                state += ' (' + DMSClient._enum_label(Qmi.UimCardError, getattr(card, 'error_code', 0)) + ')'
            slots.append(f'logical slot {index}: {state}')
        return '; '.join(slots) or 'UIM returned no card slots'

    @staticmethod
    def _enum_label(enum, value):
        """The GI label of an enum value, or '#<n>' when the build does not know it."""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 'unknown'
        try:
            return enum.get_string(number) or f'#{number}'
        except (TypeError, ValueError):
            return f'#{number}'

    @staticmethod
    def _count(value):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def card_report(output):
        """Identifier-free copy of the UIM card/application state.

        The cards and nested applications are borrowed from ``output``
        (transfer-none), so this must run while the response is alive. PIN/PUK
        retry counters are copied; AIDs and any other identifier never are.
        Some GI builds cannot marshal nested applications, hence the defaults.
        """
        primary, _, _, _, cards = output.get_card_status()
        report = {'primary': {'logical_slot': None, 'application': None}, 'cards': []}
        if type(primary) is int and 0 <= primary < 0xFFFF:
            report['primary'] = {'logical_slot': (primary >> 8) + 1, 'application': primary & 0xFF}
        state_names = {int(Qmi.UimCardState.ABSENT): 'absent', int(Qmi.UimCardState.PRESENT): 'present',
                       int(Qmi.UimCardState.ERROR): 'error'}
        for index, card in enumerate(list(cards or ())[:4], 1):
            state_value = getattr(card, 'card_state', None)
            state = 'unknown' if state_value is None else state_names.get(
                DMSClient._count(state_value), 'unknown')
            error_number, error = None, None
            if state == 'error':
                try:
                    error_number = int(getattr(card, 'error_code', 0))
                except (TypeError, ValueError):
                    error_number = None
                error = DMSClient._enum_label(Qmi.UimCardError, error_number or 0)
            applications = []
            for app in list(getattr(card, 'applications', ()) or ())[:4]:
                applications.append({
                    'type': DMSClient._enum_label(Qmi.UimCardApplicationType, getattr(app, 'type', 0)),
                    'state': DMSClient._enum_label(Qmi.UimCardApplicationState, getattr(app, 'state', 0)),
                    'upin_replaces_pin1': bool(getattr(app, 'upin_replaces_pin1', False)),
                    'pin1': {'state': DMSClient._enum_label(Qmi.UimPinState, getattr(app, 'pin1_state', 0)),
                             'retries': DMSClient._count(getattr(app, 'pin1_retries', 0)),
                             'puk_retries': DMSClient._count(getattr(app, 'pin1_puk_retries', 0))},
                    'personalization': {
                        'state': DMSClient._enum_label(
                            Qmi.UimCardApplicationPersonalizationState,
                            getattr(app, 'personalization_state', 0)),
                        'feature': DMSClient._enum_label(
                            Qmi.UimCardApplicationPersonalizationFeature,
                            getattr(app, 'personalization_feature', 0)),
                        'retries': DMSClient._count(getattr(app, 'personalization_retries', 0)),
                        'unblock_retries': DMSClient._count(
                            getattr(app, 'personalization_unblock_retries', 0))}})
            report['cards'].append({
                'logical_slot': index, 'state': state, 'error': error, 'error_code': error_number,
                'upin': {'state': DMSClient._enum_label(Qmi.UimPinState, getattr(card, 'upin_state', 0)),
                         'retries': DMSClient._count(getattr(card, 'upin_retries', 0)),
                         'puk_retries': DMSClient._count(getattr(card, 'upin_puk_retries', 0))},
                'applications': applications})
        return report

    @staticmethod
    def sim_report(output):
        """The stored SIM report: the card detail plus its one specific code."""
        report = DMSClient.card_report(output)
        code, _ = DMSClient.card_code(report)
        return {'code': code, 'report': report}

    @staticmethod
    def _application_code(card, app):
        """One application's cause, or None when it is ready."""
        pin = card['upin'] if app.get('upin_replaces_pin1') else app['pin1']
        state = app.get('state')
        if pin.get('state') == 'permanently-blocked':
            return 'sim.blocked', {}
        if pin.get('state') == 'blocked':
            return 'sim.puk_required', {'retries': pin.get('puk_retries', 0)}
        if pin.get('state') == 'enabled-not-verified':
            return 'sim.pin_required', {'retries': pin.get('retries', 0)}
        if state == 'illegal':
            return 'sim.app_illegal', {}
        personalization = app.get('personalization') or {}
        if (personalization.get('state') in {'code-required', 'puk-code-required', 'permanently-blocked'}
                or (state == 'check-personalization-state' and personalization.get('state') != 'ready')):
            return 'sim.network_locked', {'feature': personalization.get('feature')}
        # Some firmware leaves the PIN state uninitialized while the application
        # already reports the lock; never call such a card ready.
        if state == 'pin1-or-upin-pin-required':
            return 'sim.pin_required', {'retries': pin.get('retries', 0)}
        if state == 'puk1-or-upin-puk-required':
            return 'sim.puk_required', {'retries': pin.get('puk_retries', 0)}
        if state == 'pin1-blocked':
            return 'sim.blocked', {}
        if state in {'detected', 'unknown'}:
            return 'sim.initializing', {}
        return None

    @staticmethod
    def slot_status(output):
        """Physical slot mapping from Get Slot Status, without the ICCID or eID.

        Only an active slot names a logical slot; an inactive slot's logical
        slot is meaningless. The physical slot information is index-aligned.
        """
        status = list(output.get_physical_slot_status() or ())
        try:
            information = list(output.get_physical_slot_information() or ())
        except (AttributeError, TypeError, ValueError):
            information = []
        slots = []
        for index, slot in enumerate(status[:4]):
            state = DMSClient._enum_label(Qmi.UimSlotState, getattr(slot, 'physical_slot_status', 0))
            record = {'physical_slot': index + 1, 'state': state,
                      'card': DMSClient._enum_label(Qmi.UimPhysicalCardState,
                                                    getattr(slot, 'physical_card_status', 0)),
                      'logical_slot': None, 'protocol': None, 'euicc': None, 'atr_present': None}
            if state == 'active':
                try:
                    logical = int(getattr(slot, 'logical_slot', 0))
                except (TypeError, ValueError):
                    logical = 0
                record['logical_slot'] = logical if 1 <= logical <= 4 else None
            if index < len(information):
                info = information[index]
                record['protocol'] = DMSClient._enum_label(Qmi.UimCardProtocol,
                                                           getattr(info, 'card_protocol', 0))
                euicc = getattr(info, 'is_euicc', None)
                record['euicc'] = bool(euicc) if euicc is not None else None
                try:
                    record['atr_present'] = bool(getattr(info, 'atr_value', None))
                except (TypeError, ValueError):
                    record['atr_present'] = None
            slots.append(record)
        return slots

    @staticmethod
    def card_code(report):
        """The specific SIM code for the card the modem uses, plus its params."""
        cards = list((report or {}).get('cards') or ())
        if not cards:
            return 'sim.not_reported', {}
        primary = ((report or {}).get('primary') or {}).get('logical_slot')
        selected = None
        if isinstance(primary, int):
            selected = next((item for item in cards if item.get('logical_slot') == primary), None)
        if selected is None:
            selected = next((item for item in cards if item.get('state') != 'absent'), None)
        if selected is None:
            selected = cards[0]
        params = {'slot': selected.get('logical_slot')}
        state = selected.get('state')
        if state == 'absent':
            return 'sim.absent', params
        if state == 'error':
            error_code = selected.get('error_code')
            if error_code == int(Qmi.UimCardError.NO_ATR_RECEIVED):
                return 'sim.no_atr', params
            if error_code == int(Qmi.UimCardError.POSSIBLY_REMOVED):
                return 'sim.contact_lost', params
            return 'sim.card_error', dict(params, error=selected.get('error') or 'unknown')
        if state != 'present':
            return 'sim.not_reported', params
        applications = selected.get('applications') or []
        # Only the primary GW application decides the card: an ISIM left
        # 'detected' on a ready USIM must not make the card look initializing.
        primary_app = ((report or {}).get('primary') or {}).get('application')
        if isinstance(primary_app, int) and 0 <= primary_app < len(applications):
            applications = [applications[primary_app]]
        if not applications:
            return None, params
        causes = [cause for cause in (DMSClient._application_code(selected, app)
                                      for app in applications) if cause]
        if not causes:
            return None, params
        blocking = [cause for cause in causes if cause[0] != 'sim.initializing']
        code, extra = blocking[0] if blocking else causes[0]
        return code, dict(params, **extra)

    @staticmethod
    def offline_report(output):
        """DMS offline reason and hardware restriction, as readable params (M19)."""
        result = {}
        reason = optional(output, 'get_offline_reason')
        if reason is not None:
            try:
                number = int(reason)
                defined = 0
                for name in dir(Qmi.DmsOfflineReason):
                    if name.isupper() and not name.startswith('_'):
                        defined |= int(getattr(Qmi.DmsOfflineReason, name))
                if number & ~defined:
                    raise ValueError(number)
                result['offline_reason'] = Qmi.dms_offline_reason_build_string_from_mask(
                    Qmi.DmsOfflineReason(number))
            except (TypeError, ValueError):
                result['offline_reason'] = f'#{reason}'
        restricted = optional(output, 'get_hardware_restricted_mode')
        if restricted is not None:
            result['hardware_restricted'] = bool(restricted)
        return result

    def prepare(self):
        operating = self.session.request(Qmi.Service.DMS, "get_operating_mode")
        mode = operating.get_mode()
        if mode != Qmi.DmsOperatingMode.ONLINE:
            offline = self.offline_report(operating)
            if offline:
                self.offline = offline
                if not self.offline_logged:
                    logging.getLogger(__name__).info(
                        'Modem was not ONLINE (%s); requesting ONLINE', offline)
                    self.offline_logged = True
            value = Qmi.MessageDmsSetOperatingModeInput()
            value.set_mode(Qmi.DmsOperatingMode.ONLINE)
            self.session.request(Qmi.Service.DMS, "set_operating_mode", value)
        try:
            return self._prepare_card()
        except Exception as error:
            if self.offline:
                current = getattr(error, 'diagnosis', None)
                if isinstance(current, dict):
                    params = dict(current.get('params') or {})
                    params.update(self.offline)
                    try:
                        error.diagnosis = {'code': current.get('code'), 'params': params}
                    except (AttributeError, TypeError):
                        pass
            raise

    def _prepare_card(self):
        # Read only: PIN retries must never be consumed by recovery loops.
        # The cards and nested applications are borrowed from this output
        # (transfer-none). Retain the owner until every struct has been read.
        card_output = self.session.request(Qmi.Service.UIM, "get_card_status")
        cards = card_output.get_card_status()[-1]
        report = self.card_report(card_output)
        code, params = self.card_code(report)
        if not any(card.card_state == Qmi.UimCardState.PRESENT for card in cards):
            error = SimNotReady("SIM absent or not ready; " + self.card_summary(cards),
                                [index for index, _ in enumerate(cards, 1)] or [1],
                                report=report, code=code)
            raise diagnosis.annotate(error, code or 'sim.not_reported', params)
        for card in cards:
            if card.card_state != Qmi.UimCardState.PRESENT:
                continue
            for app in card.applications:
                state = card.upin_state if app.upin_replaces_pin1 else app.pin1_state
                if state in {Qmi.UimPinState.ENABLED_NOT_VERIFIED, Qmi.UimPinState.BLOCKED,
                             Qmi.UimPinState.PERMANENTLY_BLOCKED}:
                    error = SimLocked("SIM requires PIN/PUK; unlock it before starting the supervisor",
                                      report=report, code=code)
                    raise diagnosis.annotate(error, code or 'sim.pin_required', params)
        return {'code': code, 'report': report}

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
                # The modem may reboot before answering RESET: a timeout or a
                # closed endpoint means the reset was requested, not a failure.
                requested = mode == Qmi.DmsOperatingMode.RESET and (
                    error.matches(Qmi.core_error_quark(), Qmi.CoreError.TIMEOUT)
                    or error.matches(Qmi.core_error_quark(), Qmi.CoreError.FAILED))
                if not requested and not error.matches(Qmi.protocol_error_quark(),
                                                       Qmi.ProtocolError.NOEFFECT):
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
