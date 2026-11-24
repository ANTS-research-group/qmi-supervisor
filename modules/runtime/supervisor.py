"""Connection policy. No GLib, network commands or USB resets in this automaton."""
from enum import Enum, auto
import logging
import time
import math

from modules.device.device import discover
from modules.runtime import diagnosis


class State(Enum):
    WAIT_DEVICE = auto()
    OPEN = auto()
    WAIT_REGISTRATION = auto()
    CONNECT = auto()
    CONFIGURE_IP = auto()
    ONLINE = auto()
    MONITOR = auto()
    BACKOFF = auto()
    STOPPED = auto()


# Retry-loop transitions stay at DEBUG while a failure streak is active.
RETRY_TRANSITIONS = {('BACKOFF', 'WAIT_DEVICE'), ('WAIT_DEVICE', 'OPEN'), ('OPEN', 'BACKOFF'),
                     ('OPEN', 'WAIT_DEVICE'), ('WAIT_REGISTRATION', 'BACKOFF')}


class Supervisor:
    def __init__(self, settings, backend, telemetry, stop, *, discovery=discover, clock=time.monotonic,
                 utcnow=None, control=None, publish_state=None, restore_streak=None):
        self.settings, self.backend, self.telemetry, self.stop = settings, backend, telemetry, stop
        self.discovery, self.clock = discovery, clock
        self.restore_streak = restore_streak
        self.log = logging.getLogger(__name__)
        self.state = State.WAIT_DEVICE
        self.device = None
        self.failures = 0
        self.deadline = 0
        self.next_health = self.next_sample = 0
        self.control, self.publish_state = control, publish_state
        self.skipped_intervals = 0
        self.pending_skips = 0
        self.last_error = None
        self.failed_state = None
        self.tracker = diagnosis.FailureTracker(clock, utcnow)
        self.limiter = None
        self.config_issue = None
        self.last_enumeration = None
        self.startup_anchor = None
        self.transients_in_a_row = 0
        self.transient_diagnosis = None

    def transition(self, state):
        if self.state != state:
            transition = (self.state.name, state.name)
            if self.tracker.active and transition in RETRY_TRANSITIONS:
                self.log.debug("State %s -> %s", *transition)
            else:
                self.log.info("State %s -> %s", *transition)
            self.state = state
            self.tracker.step_passed(state.name)
            if state in {State.WAIT_REGISTRATION, State.CONNECT, State.CONFIGURE_IP,
                         State.ONLINE, State.MONITOR}:
                self.transients_in_a_row = 0
                self.transient_diagnosis = None
            if state in {State.WAIT_REGISTRATION, State.MONITOR, State.ONLINE}:
                self.next_sample = self.clock()
            if state in {State.MONITOR, State.ONLINE}:
                summary = self.tracker.success()
                if summary is not None:
                    self.log.info("Connection recovered after %d failed attempts over %s (last problem [%s])",
                                  summary['attempts'], diagnosis.format_duration(summary['duration_s']),
                                  summary['code'] or 'unknown')
                self.last_error = self.failed_state = None

    def cleanup(self):
        try:
            self.backend.close()
        except Exception:
            self.log.exception("Connection cleanup failed")

    def transient_anchors(self):
        """Wall-clock starts that justify a boot-time transient."""
        anchors = [self.startup_anchor]
        recovery = getattr(self.backend, 'sim_recovery', None)
        if isinstance(recovery, dict):
            anchors.append(recovery.get('reset'))
        return anchors

    def fail(self, error):
        try:
            failure = diagnosis.classify(error, self.state.name)
        except Exception:
            self.log.debug("Diagnosis of a connection failure is unavailable", exc_info=True)
            failure = None
        if failure is not None and diagnosis.is_transient(
                failure, self.transient_anchors(), time.time(), self.transients_in_a_row):
            self.transients_in_a_row += 1
            self.last_error = str(error)
            self.failed_state = self.state.name
            self.cleanup()
            self.deadline = self.clock() + self.settings.recovery.retry_initial
            self.transient_diagnosis = {
                'code': 'startup.transient', 'severity': 'info', 'active': True,
                'transient': True, 'step': failure.get('step'),
                'operation': failure.get('operation'), 'params': {},
                'detail': failure.get('detail') or '', 'attempts': self.transients_in_a_row}
            self.log.info("Modem still starting after a new USB connection [%s] (attempt %d): %s",
                          'startup.transient', self.transients_in_a_row,
                          failure.get('detail') or error)
            self.transition(State.BACKOFF)
            return
        self.transients_in_a_row = 0
        self.transient_diagnosis = None
        event = self.tracker.record(failure) if failure is not None else None
        self.last_error = str(error)
        self.failed_state = self.state.name
        self.cleanup()
        retry = self.settings.recovery
        delay = min(retry.retry_initial * 2 ** min(self.failures, 20), retry.retry_max)
        self.failures += 1
        self.deadline = self.clock() + delay
        if failure is None or event is None:
            self.log.warning("Connection step %s failed: %s", self.state.name, error)
        else:
            code = failure['code']
            if event['kind'] == 'logged':
                self.log.warning("Connection step %s failed [%s] (attempt %d): %s; retry in %.1f s",
                                 self.state.name, code, event['count'], failure['detail'], delay,
                                 extra={'qmi_diagnosis': True})
            elif event['kind'] == 'summary':
                self.log.warning("Connection step %s still failing [%s]: %d attempts since %s UTC; "
                                 "last error: %s", self.state.name, code, event['count'],
                                 self.tracker.since_text(), failure['detail'],
                                 extra={'qmi_diagnosis': True})
            else:
                self.log.debug("Connection step %s failed [%s] (attempt %d): %s; retry in %.1f s",
                               self.state.name, code, event['count'], failure['detail'], delay,
                               extra={'qmi_diagnosis': True})
            if event['escalated']:
                self.log.error("Modem needs attention [%s]: %s; failing since %s UTC (%d attempts)",
                               code, diagnosis.LOG_TITLES.get(code, 'an unexpected error'),
                               self.tracker.failing_since_text(), event['attempts'],
                               extra={'qmi_diagnosis': True})
                folded = sum(self.limiter.drain().values()) if self.limiter else 0
                if folded:
                    self.log.info("Folded %d repeated log lines during this failure", folded,
                                  extra={'qmi_diagnosis': True})
        self.transition(State.BACKOFF)
        self.log.debug("Retry in %.1f seconds", delay)

    def diagnosis(self):
        """Published failure identity; a boot transient never hides a real cause."""
        published = self.tracker.snapshot()
        if isinstance(published, dict) and published.get('active'):
            if published.get('code') == 'recovery.saved_client_unanswered':
                probe = getattr(self.backend, 'probe', None)
                if isinstance(probe, dict):
                    params = dict(published.get('params') or {})
                    params['probe'] = probe
                    published = dict(published, params=params)
            return published
        if self.transient_diagnosis is not None:
            return self.transient_diagnosis
        return published

    def sample(self):
        if not self.stop.is_set() and self.settings.telemetry.enabled and self.clock() >= self.next_sample:
            interval = self.settings.telemetry.interval
            missed = max(0, math.floor((self.clock() - self.next_sample) / interval))
            scheduled = self.next_sample + missed * interval
            skipped = self.pending_skips + missed
            self.skipped_intervals += missed
            self.pending_skips = 0
            self.next_sample = scheduled + interval
            try:
                self.backend.observation_state = self.state.name
                self.telemetry.sample(self.backend, self.device, scheduled_monotonic=scheduled,
                                      skipped_intervals=skipped)
            except Exception:
                self.log.exception("Telemetry sample failed; connection is retained")
            # Never replay expired ticks in a burst when the modem exceeds its budget.
            if self.clock() > self.next_sample:
                overrun = math.floor((self.clock() - self.next_sample) / interval) + 1
                self.next_sample += overrun * interval
                self.pending_skips += overrun
                self.skipped_intervals += overrun

    def step(self):
        if self.state == State.BACKOFF:
            if self.clock() >= self.deadline:
                self.transition(State.WAIT_DEVICE)
            return
        if self.state == State.WAIT_DEVICE:
            # Only explicit configuration pins a modem. Automatic selection must
            # accept replacements, including a different serial or USB port.
            device = self.discovery(self.settings.modem.device, self.settings.modem.serial)
            if device is None:
                return
            self.device = device
            enumeration = getattr(device, 'enumeration', None)
            if enumeration != self.last_enumeration:
                self.last_enumeration = enumeration
                self.startup_anchor = time.time()
            if self.restore_streak is not None:
                restore, self.restore_streak = self.restore_streak, None
                try:
                    record = restore(device)
                    if record:
                        self.tracker.from_record(record)
                except Exception:
                    self.log.debug("A saved failure streak could not be restored", exc_info=True)
            if self.tracker.active:
                self.log.debug("Selected %s (%s), interface %s", device.path, device.product, device.interface)
            else:
                self.log.info("Selected %s (%s), interface %s", device.path, device.product, device.interface)
            self.transition(State.OPEN)
        elif self.state == State.OPEN:
            self.backend.open(self.device)
            if self.settings.modem.mode == "monitor":
                self.transition(State.MONITOR)
            else:
                self.backend.prepare()
                self.deadline = self.clock() + self.settings.recovery.registration_timeout
                self.transition(State.WAIT_REGISTRATION)
        elif self.state == State.WAIT_REGISTRATION:
            self.sample()
            if self.stop.is_set():
                return
            with diagnosis.stage('register.wait'):
                if self.backend.registered():
                    registered = True
                elif self.clock() >= self.deadline:
                    code = 'registration.timeout'
                    params = {'seconds': self.settings.recovery.registration_timeout}
                    diagnose = getattr(self.backend, 'registration_diagnosis', None)
                    if callable(diagnose):
                        try:
                            value = diagnose()
                        except Exception:
                            value = None
                        if isinstance(value, dict) and value.get('code'):
                            code = value['code']
                            params = dict(value.get('params') or {})
                    short = diagnosis.LOG_TITLES.get(
                        code, 'check SIM, PIN, coverage and provisioning')
                    note = getattr(self.backend, 'note_registration_timeout', None)
                    if callable(note):
                        try:
                            note()
                        except Exception:
                            pass
                    raise diagnosis.annotate(
                        diagnosis.RegistrationTimeout("Network registration timed out; " + short),
                        code, params)
                else:
                    registered = False
            if registered:
                self.transition(State.CONNECT)
        elif self.state == State.CONNECT:
            with diagnosis.stage('connect.start'):
                self.backend.connect()
            self.transition(State.CONFIGURE_IP)
        elif self.state == State.CONFIGURE_IP:
            with diagnosis.stage('configure.ip'):
                self.backend.configure_ip()
                if not self.backend.healthy():
                    raise diagnosis.annotate(
                        RuntimeError("Bearer connected but host IPv4 configuration is incomplete"),
                        'ip.incomplete')
            self.failures = 0
            self.next_health = self.clock() + self.settings.recovery.poll_interval
            self.transition(State.ONLINE)
        elif self.state in {State.ONLINE, State.MONITOR}:
            if self.state == State.ONLINE:
                maintain = getattr(self.backend, 'maintain_network', None)
                if maintain:
                    maintain()
            if self.clock() >= self.next_health:
                with diagnosis.stage('health.check'):
                    if not self.backend.present():
                        raise diagnosis.annotate(
                            RuntimeError("USB modem removed or its device path changed"), 'device.removed')
                    if self.state == State.ONLINE and not self.backend.healthy():
                        reason = getattr(self.backend, 'call_end_reason', None)
                        params = {'reason': reason} if isinstance(reason, str) and reason else {}
                        raise diagnosis.annotate(
                            RuntimeError("Data session or host IPv4 configuration lost"),
                            'session.lost', params)
                self.next_health = self.clock() + self.settings.recovery.poll_interval
            self.sample()

    def run(self):
        try:
            while not self.stop.is_set():
                if self.control and self.control():
                    break
                try:
                    self.step()
                except Exception as error:
                    self.fail(error)
                if self.publish_state:
                    self.publish_state(self)
                delay = 1.0 if self.state in {State.WAIT_DEVICE, State.WAIT_REGISTRATION,
                                              State.BACKOFF, State.ONLINE, State.MONITOR} else 0
                if self.state in {State.WAIT_REGISTRATION, State.ONLINE, State.MONITOR} and self.settings.telemetry.enabled:
                    delay = min(delay, max(0.0, self.next_sample - self.clock()))
                self.stop.wait(delay)
        finally:
            self.cleanup()
            self.transition(State.STOPPED)
            if self.publish_state:
                self.publish_state(self)
