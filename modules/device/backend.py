"""Hardware adapter; the supervisor is the sole owner of state transitions."""
import logging
import time
from pathlib import Path
from modules.device.device import discover
from modules.services.client import Session
from gi.repository import GLib, Qmi
from modules.services.dms_client import DMSClient, SimNotReady
from modules.services.nas_client import NASClient
from modules.services.wds_client import WDSClient
from modules.services.gnss_client import GNSSClient
from modules.services.indications import Indications
from modules.runtime.state import RuntimeState
from modules.device.attach_apn import AttachApn
from modules.device.query_plan import GROUPS, QueryPlan
from modules.network.manager import Network


class QmiBackend:
    # Many modems (e.g. Quectel without AT+QSIMDET) never re-read a hot-swapped
    # SIM. Power-cycle the card first; reboot the modem only if that failed.
    SIM_POWER_CYCLE_INTERVAL = 120
    SIM_RESET_INTERVAL = 1800
    SIM_READY_TIMEOUT = 15

    def __init__(self, settings, stop, state_path="/run/qmi-supervisor-state.json"):
        self.settings, self.stop = settings, stop
        self.session = Session(settings.recovery.operation_timeout, stop)
        self.network = Network(settings.network, settings.recovery.operation_timeout, stop)
        self.dms = DMSClient(self.session)
        self.nas = NASClient(self.session)
        self.wds = WDSClient(self.session)
        self.device = None
        self._opened_enumeration = None
        self.static = {}
        self.next_static = 0
        self.next_sim_identity = 0
        self.queries = QueryPlan()
        self.indications = Indications(self.session)
        self.collection_report = {}
        self.observation_state = None
        self._registration_observations = None
        self._last_status = None
        self._last_status_at = 0
        self.generation = 0
        self.state = RuntimeState(state_path)
        self.attach_apn = None
        self.attach_report = {}
        self.gnss = None
        self.network.changed = self.checkpoint

    @staticmethod
    def enumeration(device):
        try:
            return (Path(device.usb_path) / "devnum").read_text().strip()
        except OSError:
            return ""

    def checkpoint(self):
        if self.device is None or self.settings.modem.mode != "connect":
            return
        client = self.session.clients.get(Qmi.Service.WDS)
        enumeration = (self._opened_enumeration if self._opened_enumeration is not None
                       else self.enumeration(self.device))
        self.state.save({"identity": self.device.identity, "enumeration": enumeration,
                         "usb_path": str(Path(self.device.usb_path).resolve()),
                         "cid": client.get_cid() if client else None, "handle": self.wds.handle,
                         "network": self.network.checkpoint()})

    def recover(self):
        saved = self.state.load()
        if not saved:
            return
        logging.getLogger(__name__).info("Recovering resources from a previous supervisor process")
        # Keep this evidence even if allocating the old CID fails before a
        # handle can be installed in memory. Supervisor.fail() calls close().
        self._pending_recovery = saved
        current_enumeration = self.session_enumeration()
        if (saved['identity'] != self.device.identity or
                (saved.get('usb_path') and saved['usb_path'] != str(Path(self.device.usb_path).resolve()))):
            self.wds.handle = None
            raise RuntimeError('Recovery journal belongs to another physical modem; ownership was retained')
        # Do not adopt an old CID after the USB device was re-enumerated.
        same_modem = (saved["identity"] == self.device.identity and saved["enumeration"] and
                      (not saved.get('usb_path') or saved['usb_path'] == str(Path(self.device.usb_path).resolve())) and
                      saved["enumeration"] == current_enumeration)
        owned = (same_modem and not saved.get('legacy_transport_unverified') and
                 saved.get("cid") is not None and saved.get("handle") is not None)
        if not owned:
            # A backend can survive retries. Its in-memory handle must not be
            # used with a new USB enumeration or a replacement modem.
            self.wds.handle = None
        try:
            self.network.restore(saved["network"])
            self.network.close()
            if owned:
                try:
                    self.session.client(Qmi.Service.WDS, saved["cid"])
                    self.wds.handle = saved["handle"]
                    self.wds.stop()
                    if self.wds.handle is not None:
                        raise RuntimeError('Previous owned bearer stop was not confirmed')
                except GLib.Error as error:
                    if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.INVALIDCLIENTID):
                        raise
                    # This confirms disappearance of our old client, not that
                    # the modem has no other active PDN. New activation must
                    # still verify its effective APN.
                    logging.getLogger(__name__).info('Previous WDS client no longer exists')
                    self.wds.handle = None
                try:
                    self.session.release(Qmi.Service.WDS)
                except GLib.Error as error:
                    if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.INVALIDCLIENTID):
                        raise
        except Exception as error:
            saved = dict(saved, cleanup_pending=True, cleanup_error=str(error))
            self._pending_recovery = saved
            self.state.save(saved)
            logging.getLogger(__name__).warning('Previous resource cleanup is unconfirmed; retaining checkpoint: %s', error)
            raise
        else:
            if saved.get('legacy_transport_unverified'):
                RuntimeState(self.state.path.with_name('legacy-context.json')).save(dict(saved,
                    retired=True, retirement_reason='Legacy USB transport provenance was unavailable; CID was not reused'))
                logging.getLogger(__name__).warning('Legacy USB transport provenance is unavailable; '
                    'host resources were cleaned, the old CID was not reused, and its evidence was archived')
            self.state.clear()
            self._pending_recovery = None

    def open(self, device):
        # Handles belong to the closed session's CID, never to a freshly
        # opened session. Protect the old ownership record before open itself
        # can fail; recovery will explicitly re-adopt it after checking USB
        # identity and enumeration. Monitor mode must not adopt that record.
        self.wds.handle = None
        self._pending_recovery = self.state.load() if self.settings.modem.mode == 'connect' else None
        self.generation += 1
        self.device = device
        self._opened_enumeration = getattr(device, 'enumeration', '') or self.enumeration(device)
        self.static = {}
        self.next_static = 0
        self.next_sim_identity = 0
        self.queries.reset()
        self.indications.reset()
        self._last_status = None
        self._last_status_at = 0
        self.collection_report = {}
        self._registration_observations = None
        self.attach_apn = None
        self.attach_report = {}
        self.gnss = None
        self.nas.unsupported.clear()
        self.session.open(device.path)
        if not self.present():
            raise RuntimeError('USB device identity or enumeration changed while opening; reopen the modem')
        if self.settings.modem.mode == "connect":
            self.recover()
        else:
            self.start_gnss()

    def prepare(self):
        if self._opened_enumeration is not None:
            self.session_enumeration()
        self.network.attach(self.device.interface)
        self.prepare_sim()
        if self.settings.modem.mode == 'connect':
            attach_path = self.state.path.with_name(self.state.path.stem + '-attach.json')
            self.attach_apn = AttachApn(self.session, self.device, state_path=attach_path,
                                       enumeration=self.session_enumeration)
            self.prepare_attach_apn()
        self.nas.select_network(self.settings.modem.plmn)
        raw = (Path("/sys/class/net") / self.device.interface / "qmi/raw_ip").exists()
        # Ask firmware for the same framing the kernel will use.
        # GI names the IEEE 802.3 member "802_3", not ETHERNET.
        expected = Qmi.WdaLinkLayerProtocol.RAW_IP if raw else Qmi.WdaLinkLayerProtocol(1)
        value = Qmi.MessageWdaSetDataFormatInput()
        value.set_link_layer_protocol(expected)
        value.set_qos_format(False)
        value.set_uplink_data_aggregation_protocol(Qmi.WdaDataAggregationProtocol.DISABLED)
        value.set_downlink_data_aggregation_protocol(Qmi.WdaDataAggregationProtocol.DISABLED)
        output = self.session.request(Qmi.Service.WDA, "set_data_format", value)
        if output.get_link_layer_protocol() != expected:
            raise RuntimeError("Firmware did not accept the requested link framing")
        self.network.prepare(raw)
        self.start_gnss()

    def prepare_sim(self):
        # Wall-clock times survive worker restarts and the USB re-enumeration
        # after a reset, so a missing SIM cannot cause a reset loop.
        recovery = RuntimeState(self.state.path.with_name(self.state.path.stem + '-sim.json'))
        try:
            self.dms.prepare()
        except SimNotReady as error:
            try:
                saved = recovery.load() or {}
            except ValueError:
                saved = {}
            if saved.get('identity') != self.device.identity:
                saved = {'identity': self.device.identity}
            now, logger = time.time(), logging.getLogger(__name__)
            if now - saved.get('power_cycled', 0) >= self.SIM_POWER_CYCLE_INTERVAL:
                logger.warning('%s; power-cycling the SIM to detect a replaced card', error)
                recovery.save(dict(saved, power_cycled=now))
                for slot in error.slots:
                    self.dms.power_cycle_sim(slot)
                deadline = time.monotonic() + self.SIM_READY_TIMEOUT
                while True:
                    time.sleep(1)
                    try:
                        self.dms.prepare()
                        break
                    except SimNotReady:
                        if time.monotonic() >= deadline or self.stop.is_set():
                            raise
            elif saved.get('power_cycled') and now - saved.get('reset', 0) >= self.SIM_RESET_INTERVAL:
                logger.warning('%s after a SIM power cycle; resetting the modem', error)
                recovery.save(dict(saved, reset=now))
                self.dms.reset()
                raise RuntimeError(f'{error}; modem reset requested, waiting for it to enumerate again') from error
            else:
                raise
            logger.info('SIM detected after power cycle')
        recovery.clear()

    def start_gnss(self):
        if self.gnss is not None:
            return
        telemetry = self.settings.telemetry
        if not (telemetry.enabled and telemetry.gps and telemetry.gps_source == 'modem'):
            return
        self.gnss = GNSSClient(self.session)
        previous_deadline = self.session.collection_deadline
        self.session.collection_deadline = time.monotonic() + 3
        try:
            self.gnss.start()
        finally:
            self.session.collection_deadline = previous_deadline

    def set_radio_mode(self, mode):
        value = Qmi.MessageDmsSetOperatingModeInput()
        value.set_mode(mode)
        try:
            self.session.request(Qmi.Service.DMS, 'set_operating_mode', value)
        except GLib.Error as error:
            if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT):
                raise
        deadline = time.monotonic() + (3 if self.session.cleaning else self.settings.recovery.operation_timeout)
        while True:
            observed = self.session.request(Qmi.Service.DMS, 'get_operating_mode').get_mode()
            if observed == mode:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Modem did not confirm the requested radio operating mode')
            time.sleep(min(0.1, remaining))

    def prepare_attach_apn(self):
        lowered = False
        def before_change():
            nonlocal lowered
            # A timeout may follow an applied command. Always attempt ONLINE
            # in finally, even when LOW_POWER did not return confirmation.
            lowered = True
            logging.getLogger(__name__).info('Renewing modem registration for initial APN=%s', self.settings.modem.apn)
            self.set_radio_mode(Qmi.DmsOperatingMode.LOW_POWER)
        try:
            self.attach_report = self.attach_apn.ensure(self.settings.modem.apn,
                mode=self.settings.modem.mode, before_change=before_change)
        finally:
            if lowered:
                previous = self.session.cleaning
                self.session.cleaning = True
                try:
                    self.set_radio_mode(Qmi.DmsOperatingMode.ONLINE)
                finally:
                    self.session.cleaning = previous
                    self._last_status = None
                    self._last_status_at = 0
        logger = logging.getLogger(__name__)
        logger.log(logging.WARNING if str(self.attach_report.get('status', '')).startswith('fallback_') else logging.INFO,
                   'Initial attach profile: status=%s, profile=%s, stored APN=%r, changed=%s',
                   self.attach_report.get('status'), self.attach_report.get('profile_id'),
                   self.attach_report.get('profile_apn'), self.attach_report.get('changed'))

    def registered(self):
        observed, self._registration_observations = self._registration_observations, None
        registered = self.nas.registered(**observed) if observed is not None else self.nas.registered()
        if registered and self.attach_apn is not None and self.attach_report.get('needs_reregister'):
            if not self.attach_apn.mark_completed():
                raise RuntimeError('Initial APN registration checkpoint could not be completed for this modem')
            self.attach_report['needs_reregister'] = False
            logging.getLogger(__name__).info('New registration confirmed after initial APN update')
        return registered

    def connect(self):
        self.session.client(Qmi.Service.WDS)
        self.checkpoint()
        self.wds.start(self.settings.modem.apn, on_started=self.checkpoint)
        self.checkpoint()

    def configure_ip(self):
        if self.settings.network.method == "dhcp":
            self.network.start_dhcp()
        else:
            current = self.wds.settings()
            self.wds.verify_apn(current)
            self.network.configure(current)

    def healthy(self):
        # Sampled bearer status may be refreshed less often than every sample.
        period = max(self.settings.telemetry.interval, self.queries.period('wds_status'))
        fresh = time.monotonic() - self._last_status_at < 2 * period
        connected = self._last_status == 'connected' if fresh and self._last_status is not None else self.wds.connected()
        return self.present() and connected and self.network.healthy()

    def maintain_network(self):
        # Consume completed reachability checks without waiting for network I/O
        # or issuing QMI requests. An upstream outage retains the data bearer.
        self.network.maintain_connectivity()

    def present(self):
        if self.device is None:
            return False
        current = discover(self.device.path, self.device.serial)
        return (current == self.device and (self._opened_enumeration is None or
                self.enumeration(self.device) == self._opened_enumeration))

    def session_enumeration(self):
        """Verify the live transport without relabelling a predecessor's CID."""
        if self.device is None:
            raise RuntimeError('The modem session is not open')
        observed = self.enumeration(self.device)
        if self._opened_enumeration is not None and observed != self._opened_enumeration:
            raise RuntimeError('USB enumeration changed; reopen the modem before changing its state')
        return observed

    def _transport_changed(self):
        if self._opened_enumeration is None or self.device is None:
            return False
        try:
            return not self.present()
        except (OSError, RuntimeError, ValueError):
            return True

    def _stop_owned_bearer(self):
        if self.device is None:
            return
        if self.wds.handle is not None and self._transport_changed():
            raise RuntimeError('USB transport changed; old bearer stop was not sent')
        self.wds.stop()

    def metrics(self):
        settings = self.settings.telemetry
        start = time.monotonic()
        registration = (self.observation_state == 'WAIT_REGISTRATION' or
                        (getattr(self, 'actor_mode', False) and self.settings.modem.mode == 'connect'
                         and self.observation_state != 'ONLINE'))
        refresh_hardware = self.next_static <= start
        refresh_sim = self.next_sim_identity <= start
        static_specs = (self.dms.sim_specifications() if refresh_sim else []) + (
            self.dms.specifications(include_sim=False) if refresh_hardware else [])
        mode = 'off' if registration else settings.indications
        if mode != 'off':
            previous, self.session.collection_deadline = (self.session.collection_deadline,
                                                          start + settings.collection_budget)
            try:
                self.indications.ensure(settings.interval)
            finally:
                self.session.collection_deadline = previous
        groups = self.queries.due(settings, start)
        pushed = {}
        if mode == 'on':
            for group in groups & set(Indications.GROUPS):
                record = self.indications.fresh(group, settings.interval)
                if record is not None:
                    pushed[group] = record
            groups -= set(pushed)
        specifications = self.nas.specifications(groups)
        if registration:
            # One bounded acquisition supplies both observations and connection policy.
            # Do not allocate/poll WDS before this supervisor activates a data bearer.
            methods = {spec[2] for spec in specifications}
            for name, parser in (('get_serving_system', self.nas.serving),
                                 ('get_system_info', lambda output: {})):
                if name not in methods:
                    specifications.append(('nas.'+name, Qmi.Service.NAS, name, None, parser))
            if self.nas.target_plmn and 'get_cell_location_info' not in methods:
                specifications.append(('nas.get_cell_location_info', Qmi.Service.NAS,
                                       'get_cell_location_info', None, self.nas.cell))
        else:
            specifications += self.wds.specifications(groups)
        requests = (static_specs if registration else []) + specifications
        self._registration_observations = None
        budget = settings.collection_budget
        if mode != 'off':
            # Registering indications on a new client shares this sample's budget.
            budget = max(0.001, budget - (time.monotonic() - start))
        outputs, report = self.session.collect([spec[:4] for spec in requests],
            budget=budget, request_timeout=settings.request_timeout)
        self.queries.completed(groups | set(pushed), dict(report['requests'], **{
            GROUPS[group]: {'status': 'ok', 'received_at_utc': record['received_at_utc']}
            for group, record in pushed.items()}), start)
        queried = {group for group in groups
                   if report['requests'].get(GROUPS[group], {}).get('status', 'not_requested') != 'not_requested'}
        if registration:
            self._registration_observations = {
                'observations': {spec[2]: outputs[spec[0]] for spec in specifications if spec[0] in outputs},
                'attempted': {spec[2] for spec in specifications},
            }
            static_report = {'requests': {spec[0]: report['requests'].pop(spec[0], {}) for spec in static_specs}}
            static_outputs = {spec[0]: outputs[spec[0]] for spec in static_specs if spec[0] in outputs}
        dynamic, parsed = {}, {}
        for key, service, name, value, parser in specifications:
            if report['requests'].get(key, {}).get('unsupported') and service == Qmi.Service.NAS:
                self.nas.unsupported.add('txrx:' + str(int(value.get_radio_interface())) if key == 'nas.txrx' else name)
            if key in outputs:
                try:
                    parsed[key] = parser(outputs[key])
                    dynamic.update(parsed[key])
                except Exception as error:
                    report['requests'][key].update(status='parse_error', error=str(error))
        shadow = {}
        for group, item in Indications.GROUPS.items():
            if GROUPS[group] not in parsed:
                continue
            if mode == 'on' and not item[3]:
                self.indications.observe(group, parsed[GROUPS[group]])
            elif mode == 'shadow':
                record = self.indications.records.get(group)
                shadow[group] = record and dict(
                    age_ms=max(0, time.monotonic() - record['received_monotonic']) * 1000,
                    differences=sorted(key for key, value in parsed[GROUPS[group]].items()
                                       if record['values'].get(key) != value))
        for record in pushed.values():
            dynamic.update(record['values'])
        # Cell information also identifies the radio when signal info was not due.
        if (dynamic.get('nas_value_5g_signal_strength_rsrp') is not None or
                dynamic.get('nas_value_nr5g_cell_information_rsrp') is not None):
            self.nas.radio = Qmi.NasRadioInterface(12)
        elif dynamic.get('nas_value_lte_rsrp') is not None or dynamic.get('nas_value_lte_intrafrequency'):
            self.nas.radio = Qmi.NasRadioInterface(8)
        if dynamic.get('wds_value_connection_status') is not None:
            self._last_status = dynamic['wds_value_connection_status']
            self._last_status_at = time.monotonic()
        # SIM identity expires independently of the modem hardware identifiers.
        # All of these are optional and lower priority than moving UE data.
        remaining = settings.collection_budget - (time.monotonic() - start)
        if refresh_sim:
            # Expiry must also clear a stale SIM identity when moving-radio
            # requests consumed the budget and identity cannot run this cycle.
            self.static.update(dms_value_imsi=None, dms_value_sim_card_status=None)
        if (refresh_hardware or refresh_sim) and (registration or remaining > 0.01):
            if not registration:
                static_outputs, static_report = self.session.collect([spec[:4] for spec in static_specs],
                    budget=remaining, request_timeout=settings.request_timeout)
            for key, service, name, value, parser in static_specs:
                if key in static_outputs:
                    try:
                        self.static.update(parser(static_outputs[key]))
                    except Exception as error:
                        static_report['requests'][key].update(status='parse_error', error=str(error))
            report['static_requests'] = static_report['requests']
            if refresh_sim:
                card_status = self.static.get('dms_value_sim_card_status') or {}
                if card_status.get('state') in {'absent', 'not_ready', 'pin_required'}:
                    self.static['dms_value_imsi'] = None
                # Modern USB firmware may not implement the deprecated DMS UIM
                # command. Read EF_IMSI only after fresh UIM status identifies
                # a present primary card, and only within this sample's
                # remaining collection budget. This remains a 60-second task.
                remaining = settings.collection_budget - (time.monotonic() - start)
                if (self.static.get('dms_value_imsi') is None and card_status.get('state') in {'ready', 'unknown'}
                        and remaining > 0.01):
                    try:
                        fallback = self.dms.uim_imsi_specification(static_outputs.get('uim.identity_status'))
                        if fallback is not None:
                            fallback_outputs, fallback_report = self.session.collect([fallback[:4]],
                                budget=remaining, request_timeout=settings.request_timeout)
                            static_report['requests'].update(fallback_report['requests'])
                            if fallback[0] in fallback_outputs:
                                self.static.update(fallback[4](fallback_outputs[fallback[0]]))
                    except Exception:
                        # Never include SIM file bytes or personal identifiers in
                        # logs/errors; failure of this optional read is harmless.
                        static_report['requests']['uim.imsi'] = {
                            'method': 'read_transparent', 'status': 'error', 'error': 'Optional SIM identity read failed'}
                self.next_sim_identity = time.monotonic() + self.dms.SIM_IDENTITY_REFRESH_SECONDS
            if refresh_hardware:
                self.next_static = time.monotonic() + (3600 if self.static.get('dms_value_imei') else 60)
        if mode != 'off':
            report['indications'] = self.indications.report(mode, pushed, shadow)
        report.update(profile=settings.profile, schedule=self.queries.report(queried),
                      observation_state=self.observation_state,
                      observation_clock='host_response_time',
                      completeness_scope='QMI response completion; optional or invalid values remain null',
                      duration_ms=(time.monotonic() - start) * 1000,
                      unavailable_nas=sorted(self.nas.unsupported),
                      complete=all(item['status'] == 'ok' for item in report['requests'].values()),
                      units={'nas_value_5g_signal_strength_rsrp': 'dBm', 'nas_value_5g_signal_strength_snr': 'dB',
                             'nas_value_5g_signal_strength_extended': 'dB (RSRQ)',
                             'nas_value_nr5g_cell_information_rsrp': 'dBm',
                             'nas_value_nr5g_cell_information_rsrq': 'dB', 'nas_value_nr5g_cell_information_snr': 'dB',
                             'nas_value_lte_rssi': 'dBm', 'nas_value_lte_rsrp': 'dBm',
                             'nas_value_lte_rsrq': 'dB', 'nas_value_lte_snr': 'dB',
                             'nas_value_sinr': 'dB (EV-DO, not NR SNR)',
                             'throughput_upload_kb': 'KiB', 'throughput_download_kb': 'KiB',
                             'throughput_upload_speed_kbps': 'Kibit/s', 'throughput_download_speed_kbps': 'Kibit/s'})
        for key in dynamic:
            if key.startswith('wds_value_'):
                if key.endswith('_bps'):
                    report['units'][key] = 'bit/s'
                elif key.endswith('_bytes_ok'):
                    report['units'][key] = 'bytes'
            if key.startswith('nas_value_rx_chain_'):
                if key.endswith(('rx_power', 'rscp', 'rsrp')):
                    report['units'][key] = 'dBm'
                elif key.endswith('ecio'):
                    report['units'][key] = 'dB'
                elif key.endswith('phase'):
                    report['units'][key] = 'degrees'
        report['units']['nas_value_tx_power'] = 'dBm'
        for scope in ('nas_value_lte_intrafrequency.cells[]',
                      'nas_value_lte_interfrequency.frequencies[].cells[]'):
            for metric, unit in (('rsrp', 'dBm'), ('rsrq', 'dB'), ('rssi', 'dBm')):
                report['units'][scope + '.' + metric] = unit
        if settings.gps and settings.gps_source == 'modem':
            values, gps_report = (self.gnss.snapshot(settings.gps_max_age) if self.gnss is not None
                                  else ({}, {'enabled': True, 'source': 'qmi_loc', 'status': 'starting'}))
            dynamic.update(values)
            report['gps'] = gps_report
            report['units'].update(gnss_latitude='°', gnss_longitude='°', gnss_altitude_msl_m='m',
                gnss_altitude_ellipsoid_m='m', gnss_horizontal_uncertainty_m='m',
                gnss_horizontal_speed_mps='m/s', gnss_horizontal_confidence_percent='%')
        self.collection_report = report
        report['connection'] = {'reused_existing_bearer': False if registration else self.wds.reused,
                                'phase': self.observation_state,
                                'requested_apn': self.settings.modem.apn,
                                'reported_apn': None if registration else self.wds.apn_reported,
                                'apn_verification': None if registration else self.wds.apn_verification,
                                'reported_apn_source': None if registration else self.wds.apn_reported_source,
                                'attach': dict(self.attach_report),
                                'requested_plmn': self.settings.modem.plmn or None}
        return dict(self.static), dynamic

    def disconnect(self):
        """Release our data-plane resources while retaining NAS/DMS/LOC access."""
        previous = self.session.cleaning
        self.session.cleaning = True
        errors = []
        try:
            for action in (self.network.close, self._stop_owned_bearer):
                try:
                    action()
                except Exception as error:
                    errors.append(error)
            if self.settings.modem.mode == 'connect':
                if errors or self.wds.handle is not None or getattr(self, '_pending_recovery', None):
                    self.checkpoint()
                    saved = self.state.load()
                    if saved:
                        self.state.save(dict(saved, cleanup_pending=True,
                                             cleanup_error=str(errors[0]) if errors else 'Cleanup unconfirmed'))
                else:
                    self.state.clear()
            self._last_status = None
            self._last_status_at = 0
            if errors:
                raise errors[0]
        finally:
            self.session.cleaning = previous

    def close(self):
        self._registration_observations = None
        # A stop request cancels normal calls; cleanup gets a short, separate budget.
        self.session.cleaning = True
        if self.gnss is not None:
            try:
                if not self._transport_changed():
                    self.gnss.close()
            except Exception as error:
                logging.getLogger(__name__).warning('GPS cleanup failed (%s)', type(error).__name__)
            finally:
                self.gnss = None
        try:
            if not self._transport_changed():
                self.indications.close()
        except Exception as error:
            logging.getLogger(__name__).warning('Indication cleanup failed (%s)', type(error).__name__)
        finally:
            self.indications.reset()
        network_error = stop_error = None
        try:
            self.network.close()
        except Exception as error:
            network_error = error
        try:
            self._stop_owned_bearer()
        except Exception as error:
            stop_error = error
            logging.getLogger(__name__).warning("Could not confirm owned bearer stop: %s", error)
        try:
            if self.settings.modem.mode == "connect":
                pending = getattr(self, '_pending_recovery', None)
                if pending is None and self.device is None:
                    # No device was opened: this process has not recovered or
                    # disproved any resource ownership saved by its predecessor.
                    pending = self.state.load()
                uncertain = (pending is not None or self.wds.handle is not None or
                             network_error is not None or stop_error is not None)
                if uncertain:
                    if pending is None:
                        # Save CID/handle/identity before Session.close releases
                        # clients and clears its registry, even on a timeout.
                        self.checkpoint()
                        pending = self.state.load()
                    if pending is not None:
                        error = (stop_error or network_error or pending.get('cleanup_error') or
                                 'Previous recovery remains unconfirmed')
                        self.state.save(dict(pending, cleanup_pending=True, cleanup_error=str(error)))
                    logging.getLogger(__name__).warning('Unconfirmed cleanup retained for the next device recovery')
                else:
                    self.state.clear()
        finally:
            try:
                if self._transport_changed():
                    # Allocated client IDs also belong to the old transport.
                    # Closing it must not release matching IDs on a replacement.
                    self.session.close(release_clients=False)
                elif stop_error is not None or self.wds.handle is not None or getattr(self, '_pending_recovery', None):
                    # Retain the firmware WDS CID until its bearer stop is
                    # confirmed; other service clients can be released normally.
                    self.session.close(preserve_cids=(Qmi.Service.WDS,))
                else:
                    self.session.close()
            finally:
                self.device = None
                self._opened_enumeration = None
                self.wds.handle = None
        if network_error is not None:
            raise network_error
