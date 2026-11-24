"""Hardware adapter; the supervisor is the sole owner of state transitions."""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from modules.device.device import discover, usb_enumeration
from modules.services.client import Session
from gi.repository import GLib, Qmi
from modules.services.dms_client import DMSClient, SimLocked, SimNotReady
from modules.services.nas_client import NASClient
from modules.device.registration import RegistrationEpisode
from modules.services.wds_client import WDSClient
from modules.services.gnss_client import GNSSClient
from modules.services.indications import Indications
from modules.runtime import diagnosis
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
    # Get Slot Status is read only while the card is not ready, at most once a
    # minute, with a short deadline (it is not a cleaning path).
    SLOT_STATUS_MIN_INTERVAL = 60
    SLOT_STATUS_DEADLINE = 3

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
        self.sim_report = None
        self.sim_recovery = {}
        self.sim_slots = None
        self.sim_slots_unsupported = False
        self.next_slot_status = 0.0
        self.home_plmn = None
        self.home_observed = 0.0
        self.home_read_episode = False
        self.registered_plmn = None
        self.call_end_reason = None
        self.registration = self.new_registration()
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
        return usb_enumeration(device.usb_path)

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
            raise diagnosis.annotate(RuntimeError(
                'Recovery journal belongs to another physical modem; ownership was retained'),
                'recovery.foreign_record')
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
            with diagnosis.stage('open.recover.host'):
                self.network.restore(saved["network"])
                self.network.close()
            if owned:
                try:
                    with diagnosis.stage('open.recover.saved_stop'):
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
                    with diagnosis.stage('open.recover.release'):
                        self.session.release(Qmi.Service.WDS)
                except GLib.Error as error:
                    if not error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.INVALIDCLIENTID):
                        raise
        except Exception as error:
            # Name only what happened and whether a person must act. No CID,
            # handle, identity or USB path may reach the diagnosis params.
            operation, last_result = getattr(error, 'operation', None), diagnosis.cause_of(error)
            if operation == 'open.recover.saved_stop' and last_result == 'timeout' and owned:
                diagnosis.annotate(error, 'recovery.saved_client_unanswered',
                                   owned=True, last_result='timeout')
            elif operation == 'open.recover.host':
                diagnosis.annotate(error, 'recovery.host_cleanup_unconfirmed', last_result=last_result)
            else:
                diagnosis.annotate(error, 'recovery.cleanup_unconfirmed', owned=owned,
                                   stop_confirmed=operation == 'open.recover.release',
                                   last_result=last_result)
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
        self.sim_report = None
        self.sim_recovery = {}
        self.load_sim_recovery()
        self.sim_slots = None
        self.sim_slots_unsupported = False
        self.next_slot_status = 0.0
        self.home_plmn = None
        self.home_observed = 0.0
        self.home_read_episode = False
        self.registered_plmn = None
        self.call_end_reason = None
        self.registration = self.new_registration()
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
        with diagnosis.stage('prepare.network'):
            self.network.attach(self.device.interface)
        with diagnosis.stage('prepare.sim'):
            self.prepare_sim()
        self.maybe_read_home()
        if self.settings.modem.mode == 'connect':
            attach_path = self.state.path.with_name(self.state.path.stem + '-attach.json')
            self.attach_apn = AttachApn(self.session, self.device, state_path=attach_path,
                                       enumeration=self.session_enumeration)
            with diagnosis.stage('prepare.attach_apn'):
                self.prepare_attach_apn()
        with diagnosis.stage('prepare.plmn'):
            self.nas.select_network(self.settings.modem.plmn)
        raw = (Path("/sys/class/net") / self.device.interface / "qmi/raw_ip").exists()
        # Ask firmware for the same framing the kernel will use.
        # GI names the IEEE 802.3 member "802_3", not ETHERNET.
        expected = Qmi.WdaLinkLayerProtocol.RAW_IP if raw else Qmi.WdaLinkLayerProtocol(1)
        with diagnosis.stage('prepare.data_format'):
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

    def observe_sim(self, value):
        """Keep the last identifier-free SIM report from prepare or the 60-s refresh."""
        if not isinstance(value, dict) or not isinstance(value.get('report'), dict):
            return
        self.sim_report = {'code': value.get('code'), 'report': value['report'],
                           'observed_at_utc': datetime.now(timezone.utc).isoformat()}

    def sim_recovery_path(self):
        return self.state.path.with_name(self.state.path.stem + '-sim.json')

    def load_sim_recovery(self):
        """The saved ladder for this file, so a new worker honours the rate limits."""
        try:
            value = RuntimeState(self.sim_recovery_path()).load()
        except (OSError, ValueError):
            value = None
        self.sim_recovery = value if isinstance(value, dict) else {}

    @staticmethod
    def annotate_sim_ladder(error, power_cycles, action=None):
        """Make the existing ladder visible to the after_ladder severity policy."""
        current = getattr(error, 'diagnosis', None)
        current = current if isinstance(current, dict) else {}
        params = dict(current.get('params') or {})
        params['ladder_tried'] = power_cycles >= 1
        if action:
            params['action'] = action
        try:
            error.diagnosis = {'code': current.get('code') or getattr(error, 'code', None)
                               or 'sim.not_reported', 'params': params}
        except (AttributeError, TypeError):
            pass
        return error

    def sim_recovery_view(self):
        """The public, bounded view of the saved ladder published in the snapshot."""
        record = self.sim_recovery if isinstance(self.sim_recovery, dict) else {}

        def stamp(value):
            try:
                return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                return None

        def count(key):
            try:
                return max(0, int(record.get(key) or 0))
            except (TypeError, ValueError):
                return 0

        def due(key, interval):
            try:
                return stamp(float(record[key]) + interval) if record.get(key) else None
            except (TypeError, ValueError):
                return None

        return {'since_utc': stamp(record.get('since')),
                'power_cycles': count('power_cycles'), 'resets': count('resets'),
                'last_power_cycle_utc': stamp(record.get('power_cycled')),
                'last_reset_utc': stamp(record.get('reset')),
                'next_power_cycle_not_before_utc': due('power_cycled', self.SIM_POWER_CYCLE_INTERVAL),
                'next_reset_not_before_utc': due('reset', self.SIM_RESET_INTERVAL),
                'last_outcome': record.get('last_outcome') if isinstance(record.get('last_outcome'), str) else None}

    def read_sim_slots(self):
        """Rate-limited Get Slot Status; None when unknown or unsupported."""
        if self.sim_slots_unsupported:
            return None
        now = time.monotonic()
        if self.sim_slots is not None and now < self.next_slot_status:
            return self.sim_slots
        self.next_slot_status = now + self.SLOT_STATUS_MIN_INTERVAL
        previous = self.session.collection_deadline
        self.session.collection_deadline = now + self.SLOT_STATUS_DEADLINE
        try:
            self.sim_slots = DMSClient.slot_status(
                self.session.request(Qmi.Service.UIM, 'get_slot_status'))
        except GLib.Error as error:
            unsupported = (error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOTSUPPORTED)
                           or error.matches(Qmi.protocol_error_quark(),
                                            Qmi.ProtocolError.INVALIDQMICOMMAND))
            self.sim_slots = None
            self.sim_slots_unsupported = unsupported
            if not unsupported:
                logging.getLogger(__name__).debug('UIM Get Slot Status failed')
        except Exception:
            self.sim_slots = None
        finally:
            self.session.collection_deadline = previous
        return self.sim_slots

    def describe_sim_slots(self):
        """The physical-to-logical mapping as text and public params, or (None, None)."""
        slots = self.read_sim_slots()
        if not slots:
            return None, None
        active = next((slot for slot in slots if slot.get('state') == 'active'), None)
        parts = []
        if active is not None and active.get('logical_slot') is not None:
            parts.append(f"logical slot {active['logical_slot']} is physical slot "
                         f"{active['physical_slot']} (active)")
        elif active is not None:
            parts.append(f"physical slot {active['physical_slot']} (active)")
        for slot in slots:
            parts.append(f"physical slot {slot['physical_slot']} {slot.get('state')}, "
                         f"card {slot.get('card')}")
        params = {'physical_slot': active['physical_slot'] if active is not None else None,
                  'slots': slots}
        return '; '.join(parts), params

    def annotate_sim_slots(self, error):
        """Append the physical slot mapping to a SIM exception and its params."""
        text, params = self.describe_sim_slots()
        if not text:
            return error
        current = getattr(error, 'diagnosis', None)
        current = current if isinstance(current, dict) else {}
        merged = dict(current.get('params') or {})
        # The exception's own code is the card report; never reuse a code this
        # method chose on an earlier pass over the same exception object.
        code = getattr(error, 'code', None) or 'sim.not_reported'
        merged.pop('occupied', None)
        merged.pop('active', None)
        merged.update(params)
        active = params['physical_slot']
        active_slot = next((slot for slot in params['slots']
                            if slot['physical_slot'] == active), None) if active else None
        occupied = [slot for slot in params['slots']
                    if slot.get('state') == 'inactive' and slot.get('card') == 'present']
        # The Fibocom reports a dead inserted card as absent: never infer
        # "empty" from that. Only an inactive slot that reports a card while
        # the used slot reports none is unambiguous.
        if occupied and active_slot is not None and (
                active_slot.get('card') == 'absent' or code in {'sim.absent', 'sim.no_atr'}):
            code = 'sim.in_inactive_slot'
            merged['occupied'] = occupied[0]['physical_slot']
            merged['active'] = active
        try:
            error.diagnosis = {'code': code, 'params': merged}
        except (AttributeError, TypeError):
            pass
        if not getattr(error, 'sim_slots_annotated', False):
            try:
                error.args = (f'{error}; {text}',)
                error.sim_slots_annotated = True
            except (AttributeError, TypeError):
                pass
        return error

    def prepare_sim(self):
        # Wall-clock times survive worker restarts and the USB re-enumeration
        # after a reset, so a missing SIM cannot cause a reset loop.
        recovery = RuntimeState(self.sim_recovery_path())
        try:
            self.observe_sim(self.dms.prepare())
        except SimLocked as error:
            # A PIN/PUK lock is not fixed by power-cycling or resetting.
            self.observe_sim({'code': error.code, 'report': error.report})
            raise self.annotate_sim_slots(error)
        except SimNotReady as error:
            self.observe_sim({'code': error.code, 'report': error.report})
            self.annotate_sim_slots(error)
            now, logger = time.time(), logging.getLogger(__name__)
            try:
                saved = recovery.load() or {}
            except ValueError:
                saved = {}
            if saved.get('identity') != self.device.identity:
                saved = {'identity': self.device.identity}
            saved['since'] = saved.get('since') or now
            for key in ('power_cycles', 'resets'):
                try:
                    saved[key] = max(0, int(saved.get(key) or 0))
                except (TypeError, ValueError):
                    saved[key] = 0
            if now - saved.get('power_cycled', 0) >= self.SIM_POWER_CYCLE_INTERVAL:
                power_cycles = saved['power_cycles'] + 1
                logger.warning('%s; power-cycling the SIM to detect a replaced card (power cycle %d)',
                               error, power_cycles)
                record = dict(saved, power_cycled=now, power_cycles=power_cycles,
                              last_outcome='power_cycle_no_card')
                recovery.save(record)
                self.sim_recovery = dict(record)
                for slot in error.slots:
                    self.dms.power_cycle_sim(slot)
                deadline = time.monotonic() + self.SIM_READY_TIMEOUT
                while True:
                    time.sleep(1)
                    try:
                        self.observe_sim(self.dms.prepare())
                        break
                    except SimLocked as error:
                        self.observe_sim({'code': error.code, 'report': error.report})
                        raise self.annotate_sim_slots(error)
                    except SimNotReady as error:
                        self.observe_sim({'code': error.code, 'report': error.report})
                        if time.monotonic() >= deadline or self.stop.is_set():
                            raise self.annotate_sim_slots(
                                self.annotate_sim_ladder(error, power_cycles))
            elif saved.get('power_cycled') and now - saved.get('reset', 0) >= self.SIM_RESET_INTERVAL:
                resets = saved['resets'] + 1
                logger.warning('%s after a SIM power cycle; resetting the modem (reset %d)', error, resets)
                record = dict(saved, reset=now, resets=resets, last_outcome='reset_requested')
                recovery.save(record)
                self.sim_recovery = dict(record)
                self.dms.reset()
                current = getattr(error, 'diagnosis', None)
                params = dict((current if isinstance(current, dict) else {}).get('params') or {})
                params.update(ladder_tried=True, action='modem_reset')
                raise diagnosis.annotate(
                    RuntimeError(f'{error}; modem reset requested, waiting for it to enumerate again'),
                    (current if isinstance(current, dict) else {}).get('code')
                    or getattr(error, 'code', None) or 'sim.not_reported', params) from error
            else:
                raise self.annotate_sim_slots(
                    self.annotate_sim_ladder(error, saved['power_cycles']))
            logger.info('SIM detected after power cycle')
        recovery.clear()
        self.sim_recovery = {}

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

    def new_registration(self):
        return RegistrationEpisode(started=time.monotonic(),
                                   timeout=self.settings.recovery.registration_timeout)

    def expected_plmn(self):
        value = (self.settings.modem.plmn or '').strip()
        return value if len(value) in (5, 6) and value.isdigit() else None

    def reset_registration(self):
        """A new episode; BACKOFF keeps the old one so its duration accumulates."""
        self.registration = self.new_registration()
        self.home_read_episode = False
        return self.registration

    def maybe_read_home(self):
        """One bounded Get Home Network per episode, only after a registration problem."""
        if self.settings.modem.mode != 'connect' or self.home_plmn or self.home_read_episode:
            return
        if time.monotonic() - self.home_observed < 60:
            return
        unsupported = getattr(self.nas, 'unsupported', None)
        if isinstance(unsupported, set) and 'get_home_network' in unsupported:
            return
        episode = self.registration
        registration = episode.serving.get('registration')
        if not episode.samples or registration in (None, 'registered'):
            return
        self.home_read_episode = True
        previous = self.session.collection_deadline
        self.session.collection_deadline = time.monotonic() + self.SLOT_STATUS_DEADLINE
        try:
            output = self.session.request(Qmi.Service.NAS, 'get_home_network')
            home = NASClient.home(output).get('nas_value_home_network')
            if home:
                self.home_plmn = home
                self.registration.set_home(home)
                self.home_observed = time.monotonic()
        except InterruptedError:
            raise
        except Exception as error:
            # UimUninitialized or any other answer: the home stays unknown and
            # OPEN continues. A missing command is not retried until reopen.
            if (isinstance(error, GLib.Error)
                    and error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOTSUPPORTED)
                    and isinstance(unsupported, set)):
                unsupported.add('get_home_network')
        finally:
            self.session.collection_deadline = previous

    def _sync_registration(self):
        episode = self.registration
        episode.set_expected(self.expected_plmn())
        if self.home_plmn:
            episode.set_home(self.home_plmn)
        return episode

    def registration_diagnosis(self):
        """Code and params the supervisor publishes at the registration timeout."""
        try:
            code, params = self._sync_registration().classify(time.monotonic())
        except Exception:
            return None
        return {'code': code, 'params': params}

    def registration_summary(self):
        try:
            return self._sync_registration().summary()
        except Exception:
            return None

    def registration_parser(self, output):
        """Feed Get System Info to the episode; dynamic keys and CSV stay unchanged."""
        try:
            self.registration.observe_system_info(NASClient.system_info(output),
                                                  time.monotonic())
        except Exception:
            logging.getLogger(__name__).debug('Registration evidence could not be read')
        return {}

    def registration_observer(self, method, output, error=None):
        """NASClient.registered() observer, used when telemetry performs the reads."""
        now = time.monotonic()
        if error is not None:
            self.registration.observe_failure(method, type(error).__name__, now)
            return
        try:
            if method == 'get_serving_system':
                self.registration.observe_serving(NASClient.serving(output), now)
            elif method == 'get_system_info':
                self.registration.observe_system_info(NASClient.system_info(output), now)
        except Exception:
            logging.getLogger(__name__).debug('Registration evidence could not be read')

    def registration_observe(self, parsed, report):
        """Account for this acquisition: serving system and failed NAS queries."""
        now = time.monotonic()
        for key, name in (('nas.get_serving_system', 'get_serving_system'),
                          ('nas.get_system_info', 'get_system_info')):
            status = (report.get('requests') or {}).get(key, {}).get('status', 'not_requested')
            if status != 'not_requested' and status != 'ok':
                self.registration.observe_failure(name, status, now)
        serving = parsed.get('nas.get_serving_system')
        if serving is not None:
            self.registration.observe_serving(serving, now)

    def registered(self):
        observed, self._registration_observations = self._registration_observations, None
        registered = self.nas.registered(observer=self.registration_observer, **observed) \
            if observed is not None else self.nas.registered(observer=self.registration_observer)
        if registered:
            self.registered_plmn = self.registration.registered(
                getattr(self.registration, 'serving_plmn', None))
            self.reset_registration()
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
                                 ('get_system_info', self.registration_parser)):
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
        home = (parsed.get('nas.get_home_network') or {}).get('nas_value_home_network') \
            if isinstance(parsed.get('nas.get_home_network'), dict) else None
        if isinstance(home, str) and home:
            self.home_plmn = home
            self.home_observed = time.monotonic()
            self.registration.set_home(home)
        if registration:
            self.registration_observe(parsed, report)
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
            if self._last_status == 'connected':
                self.call_end_reason = None
        if dynamic.get('wds_value_call_end_reason') is not None:
            self.call_end_reason = dynamic['wds_value_call_end_reason']
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
            if refresh_sim and 'uim.identity_status' in static_outputs:
                try:
                    self.observe_sim(self.dms.sim_report(static_outputs['uim.identity_status']))
                except Exception:
                    logging.getLogger(__name__).debug('Optional SIM report could not be read')
                # Monitor mode never calls prepare_sim: read the physical slot
                # mapping here, rate-limited, only while the card is not ready.
                if (self.settings.modem.mode != 'connect'
                        and (self.sim_report or {}).get('code')):
                    self.read_sim_slots()
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
