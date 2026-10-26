import logging
from gi.repository import GLib, Qmi
from modules.services.client import optional
from modules.device.query_plan import GROUPS, PRESETS
from modules.device.radio import active_band_label, format_plmn

GROUP_OF = {key: group for group, key in GROUPS.items()}


class NASClient:
    def __init__(self, session):
        self.session = session
        self.unsupported = set()
        self.radio = Qmi.NasRadioInterface(12)
        self.target_plmn = ''

    def select_network(self, plmn):
        self.target_plmn = '' if plmn in {'', 'auto'} else plmn
        if not plmn:
            return
        value = Qmi.MessageNasSetSystemSelectionPreferenceInput()
        if plmn == 'auto':
            value.set_network_selection_preference(Qmi.NasNetworkSelectionPreference.AUTOMATIC, 0, 0)
        else:
            value.set_network_selection_preference(Qmi.NasNetworkSelectionPreference.MANUAL,
                                                   int(plmn[:3]), int(plmn[3:]))
            value.set_mnc_pcs_digit_include_status(len(plmn) == 6)
        # Reapply from configuration on startup without permanently rewriting
        # the modem's stored operator preferences.
        value.set_change_duration(Qmi.NasChangeDuration.POWER_CYCLE)
        try:
            self.session.request(Qmi.Service.NAS, 'set_system_selection_preference', value)
        except GLib.Error as error:
            if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT):
                raise
        logging.getLogger(__name__).info('Requested network selection: %s', plmn)

    def matches_network(self, serving, *, location=None, location_attempted=False):
        if not self.target_plmn:
            return True
        full = optional(serving, 'get_mnc_pcs_digit_include_status')
        if full:
            return (full[0], full[1], bool(full[2])) == (
                int(self.target_plmn[:3]), int(self.target_plmn[3:]), len(self.target_plmn) == 6)
        selected = optional(serving, 'get_current_plmn')
        if selected:
            return (selected[0], selected[1]) == (int(self.target_plmn[:3]), int(self.target_plmn[3:]))
        # Some SA firmware lacks Current PLMN in Get Serving System.
        if location is None and not location_attempted:
            location = self.session.request(Qmi.Service.NAS, 'get_cell_location_info')
        if location is None:
            return False
        values = self.cell(location)
        observed = values.get('nas_value_nr5g_cell_information_plmn') or (
            values.get('nas_value_lte_intrafrequency') or {}).get('plmn')
        return observed == self.target_plmn

    def registered(self, *, observations=None, attempted=None):
        observations, attempted = observations or {}, set(attempted or ())

        def response(name):
            if name in observations:
                return observations[name]
            if name in attempted:
                return None
            attempted.add(name)
            return self.session.request(Qmi.Service.NAS, name)

        def matches(serving):
            return self.matches_network(serving, location=observations.get('get_cell_location_info'),
                                        location_attempted='get_cell_location_info' in attempted)

        output = None
        try:
            output = response('get_serving_system')
            serving = output.get_serving_system() if output is not None else None
            if serving and serving[0] == Qmi.NasRegistrationState.REGISTERED and serving[2] == Qmi.NasAttachState.ATTACHED:
                interfaces = serving[4]
                for radio in (12, 8, 4):
                    if radio in interfaces:
                        self.radio = Qmi.NasRadioInterface(radio)
                        break
                return matches(output)
        except GLib.Error:
            pass
        # Some SA firmware exposes packet registration only through Get System Info.
        try:
            info = response('get_system_info')
            if info is None:
                return False
            for radio in ("nr5g", "lte"):
                status = optional(info, "get_nr5g_service_status_info" if radio == "nr5g" else "get_lte_service_status")
                system = optional(info, "get_nr5g_system_info" if radio == "nr5g" else "get_lte_system_info_v2")
                if status and system and status[0] == Qmi.NasServiceStatus.AVAILABLE:
                    domain_valid, domain = system[:2]
                    forbidden_valid, forbidden = system[6:8]
                    if domain_valid and domain in {Qmi.NasNetworkServiceDomain.PS, Qmi.NasNetworkServiceDomain.CS_PS}:
                        if not (forbidden_valid and forbidden):
                            self.radio = Qmi.NasRadioInterface(12 if radio == 'nr5g' else 8)
                            return matches(output)
        except GLib.Error:
            pass
        return False

    def metrics(self):
        result = {}
        for name, collect in (("get_serving_system", self.serving), ("get_signal_strength", self.strength),
                              ("get_signal_info", self.signal), ("get_home_network", self.home),
                              ("get_cell_location_info", self.cell), ("get_rf_band_information", self.bands)):
            if name in self.unsupported:
                continue
            try:
                value = self.strength_input() if name == 'get_signal_strength' else None
                result.update(collect(self.session.request(Qmi.Service.NAS, name, value)))
            except GLib.Error as error:
                if error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOTSUPPORTED):
                    self.unsupported.add(name)
                logging.getLogger(__name__).debug("Optional NAS metric %s unavailable: %s", name, error)
        return result

    def specifications(self, groups='full'):
        """Requests for the scheduled query groups; a profile name selects its preset."""
        if isinstance(groups, str):
            groups = PRESETS[groups]
        methods = [('get_serving_system', self.serving), ('get_signal_strength', self.strength),
                   ('get_signal_info', self.signal), ('get_home_network', self.home),
                   ('get_cell_location_info', self.cell), ('get_rf_band_information', self.bands)]
        requests = [('nas.' + name, Qmi.Service.NAS, name,
                     self.strength_input() if name == 'get_signal_strength' else None, parser)
                    for name, parser in methods if GROUP_OF['nas.' + name] in groups and name not in self.unsupported]
        if 'txrx' in groups and ('txrx:' + str(int(self.radio))) not in self.unsupported:
            value = Qmi.MessageNasGetTxRxInfoInput()
            value.set_radio_interface(self.radio)
            requests.append(('nas.txrx', Qmi.Service.NAS, 'get_tx_rx_info', value,
                             lambda output, radio=self.radio: self.txrx(output, radio)))
        return requests

    @staticmethod
    def strength_input():
        value = Qmi.MessageNasGetSignalStrengthInput()
        mask = Qmi.NasSignalStrengthRequest
        value.set_request_mask(mask.RSSI | mask.ECIO | mask.IO | mask.SINR | mask.RSRQ | mask.LTE_RSRP | mask.LTE_SNR)
        return value

    @staticmethod
    def txrx(output, radio):
        result = {'nas_value_tx_rx_radio_interface': Qmi.NasRadioInterface.get_string(radio)}
        for index in range(4):
            values = optional(output, f'get_rx_chain_{index}_info')
            if values is None:
                continue
            tuned, power, ecio, rscp, rsrp, phase = values
            prefix = f'nas_value_rx_chain_{index}_'
            metric_prefix = prefix + 'info_'
            result.update({prefix + 'is_radio_tuned': tuned,
                           metric_prefix + 'rx_power': power / 10.0 if tuned else None,
                           metric_prefix + 'ecio': ecio / 10.0 if tuned else None,
                           metric_prefix + 'rscp': rscp / 10.0 if tuned and int(radio) == 4 else None,
                           metric_prefix + 'rsrp': rsrp / 10.0 if tuned and int(radio) in {8, 12} else None,
                           metric_prefix + 'phase': phase / 100.0 if tuned and int(radio) in {8, 12} and phase != 0xFFFFFFFF else None})
        tx = optional(output, 'get_tx_info')
        if tx:
            result.update(nas_value_tx_is_in_traffic=tx[0], nas_value_tx_power=tx[1] / 10.0 if tx[0] else None)
        return result

    @staticmethod
    def serving(output):
        registration, cs, ps, selected, interfaces = output.get_serving_system()
        result = {
            "nas_value_serving_system_registration_state": Qmi.NasRegistrationState.get_string(registration),
            "nas_value_serving_system_cs_attach_state": Qmi.NasAttachState.get_string(cs),
            "nas_value_serving_system_ps_attach_state": Qmi.NasAttachState.get_string(ps),
            "nas_value_serving_system_selected_network": Qmi.NasNetworkType.get_string(selected),
            "nas_value_serving_system_radio_interfaces": [Qmi.NasRadioInterface.get_string(i) for i in interfaces],
        }
        roaming = optional(output, "get_roaming_indicator")
        if roaming is not None:
            result["nas_value_roaming_indicator"] = Qmi.NasRoamingIndicatorStatus.get_string(roaming)
        plmn = optional(output, "get_current_plmn")
        if plmn:
            result.update(zip(("nas_value_current_plmn_mcc", "nas_value_current_plmn_mnc",
                               "nas_value_current_plmn_description"), plmn))
        pcs = optional(output, 'get_mnc_pcs_digit_include_status')
        if pcs:
            # Extra JSON metadata; existing CSV columns remain unchanged.
            result['nas_value_current_plmn'] = format_plmn(pcs[0], pcs[1], bool(pcs[2]))
        return result

    @staticmethod
    def strength(output):
        strength = optional(output, "get_signal_strength")
        sinr = optional(output, "get_sinr")
        sinr_db = {0: -9, 1: -6, 2: -4.5, 3: -3, 4: -2, 5: 1, 6: 3, 7: 6, 8: 9}
        result = {"nas_value_io": optional(output, "get_io"), "nas_value_sinr": sinr_db.get(sinr)}
        if strength:
            result.update(nas_value_signal_strength_strength=strength[0],
                          nas_value_signal_strength_radio_interface=Qmi.NasRadioInterface.get_string(strength[1]))
        return result

    @staticmethod
    def signal(output):
        def valid(value, scale=1):
            return None if value is None or value == -32768 else (value if scale == 1 else value / 10.0)
        result = {"nas_value_5g_signal_strength_extended": valid(optional(output, "get_5g_signal_strength_extended"))}
        strength = optional(output, "get_5g_signal_strength")
        if strength:
            result.update(nas_value_5g_signal_strength_rsrp=valid(strength[0]),
                          nas_value_5g_signal_strength_snr=valid(strength[1], 0.1))
        lte = optional(output, 'get_lte_signal_strength')
        if lte:
            result.update(nas_value_lte_rssi=valid(lte[0]), nas_value_lte_rsrq=valid(lte[1]),
                          nas_value_lte_rsrp=valid(lte[2]), nas_value_lte_snr=valid(lte[3], 0.1))
        return result

    @staticmethod
    def home(output):
        home = optional(output, "get_home_network")
        result = dict(zip(("nas_value_home_network_mcc", "nas_value_home_network_mnc",
                           "nas_value_home_network_description"), home)) if home else {}
        source = optional(output, "get_network_name_source")
        if source is not None:
            result["nas_value_network_name_source"] = Qmi.NasNetworkNameSource.get_string(source)
        return result

    @staticmethod
    def plmn(value):
        if value is None or len(value) != 3:
            return None
        # PLMN is 3GPP BCD; the third MNC digit is F for a two-digit MNC.
        digits = [value[0] & 15, value[0] >> 4, value[1] & 15, value[2] & 15, value[2] >> 4]
        extra = value[1] >> 4
        if any(digit > 9 for digit in digits) or (extra > 9 and extra != 15):
            return None
        return ''.join(map(str, digits)) + (str(extra) if extra != 15 else '')

    @staticmethod
    def lte_cell_values(cell, *, selection_valid=True):
        def tenths(value):
            return value / 10.0 if value is not None and value != -32768 else None
        return dict(physical_cell_id=cell.physical_cell_id, rsrq=tenths(cell.rsrq),
                    rsrp=tenths(cell.rsrp), rssi=tenths(cell.rssi),
                    cell_selection_rx_level_raw=cell.cell_selection_rx_level if selection_valid else None)

    @staticmethod
    def cell(output):
        result = {"nas_value_nr5g_arfcn": optional(output, "get_nr5g_arfcn")}
        intra = optional(output, 'get_intrafrequency_lte_info_v2')
        if intra:
            idle, plmn, tac, global_id, earfcn, serving_id, priority, non_intra, low, intra_search, cells = intra
            result['nas_value_lte_intrafrequency'] = dict(
                ue_in_idle=idle, plmn=NASClient.plmn(plmn), tracking_area_code=tac, global_cell_id=global_id,
                earfcn=earfcn, serving_cell_id=serving_id,
                cell_reselection_priority=priority if idle else None,
                s_non_intra_search_threshold_raw=non_intra if idle else None,
                serving_cell_low_threshold_raw=low if idle else None,
                s_intra_search_threshold_raw=intra_search if idle else None,
                cells=[NASClient.lte_cell_values(cell, selection_valid=idle) for cell in cells or []])
        inter = optional(output, 'get_interfrequency_lte_info')
        if inter:
            idle, frequencies = inter
            result['nas_value_lte_interfrequency'] = dict(ue_in_idle=idle, frequencies=[
                dict(earfcn=frequency.eutra_absolute_rf_channel_number,
                     cell_reselection_priority=frequency.cell_reselection_priority if idle else None,
                     cell_selection_rx_level_low_threshold_raw=frequency.cell_selection_rx_level_low_threshold,
                     cell_selection_rx_level_high_threshold_raw=frequency.cell_selection_rx_level_high_threshold,
                     cells=[NASClient.lte_cell_values(cell) for cell in frequency.cell or []])
                for frequency in frequencies or []])
        info = optional(output, "get_nr5g_cell_information")
        if info:
            plmn, tac, global_id, physical_id, rsrq, rsrp, snr = info
            result.update({
                "nas_value_nr5g_cell_information_plmn": NASClient.plmn(plmn),
                "nas_value_nr5g_cell_information_tracking_area_code": int.from_bytes(bytes(tac), "big"),
                "nas_value_nr5g_cell_information_global_cell_id": global_id,
                "nas_value_nr5g_cell_information_physical_cell_id": physical_id,
                "nas_value_nr5g_cell_information_rsrq": rsrq / 10.0 if rsrq != -32768 else None,
                "nas_value_nr5g_cell_information_rsrp": rsrp / 10.0 if rsrp != -32768 else None,
                "nas_value_nr5g_cell_information_snr": snr / 10.0 if snr != -32768 else None,
            })
        return result

    @staticmethod
    def bands(output):
        result = {}
        def label(enum, value):
            try:
                return enum.get_string(value)
            except (TypeError, ValueError):
                # Older GI enum bindings may return an unknown numeric value.
                return None
        def band_label(value):
            return label(Qmi.NasActiveBand, value) or active_band_label(int(value))
        for getter, prefix in (("get_list", "nas_list_"), ("get_extended_list", "nas_extended_list_"),
                               ("get_bandwidth_list", "nas_bandwidth_list_")):
            items = optional(output, getter, [])
            if not items:
                continue
            if getter == 'get_bandwidth_list':
                result['nas_value_rf_bandwidths'] = [
                    {'radio_interface': label(Qmi.NasRadioInterface, i.radio_interface),
                     'radio_interface_raw': int(i.radio_interface),
                     'bandwidth': label(Qmi.NasDLBandwidth, i.bandwidth),
                     'bandwidth_raw': int(i.bandwidth)} for i in items]
            else:
                result['nas_value_rf_bands' if getter == 'get_list' else 'nas_value_rf_bands_extended'] = [
                    {'radio_interface': label(Qmi.NasRadioInterface, i.radio_interface),
                     'radio_interface_raw': int(i.radio_interface),
                     'active_band_class': band_label(i.active_band_class),
                     'active_band_class_raw': int(i.active_band_class),
                     'active_channel': i.active_channel} for i in items]
            item = items[-1]
            result[prefix + "radio_interface"] = label(Qmi.NasRadioInterface, item.radio_interface)
            if getter == "get_bandwidth_list":
                result[prefix + "bandwidth"] = label(Qmi.NasDLBandwidth, item.bandwidth)
            else:
                result[prefix + "active_band_class"] = band_label(item.active_band_class)
                result[prefix + "active_channel"] = item.active_channel
        return result
