"""Connection policy. No GLib, network commands or USB resets in this automaton."""
from enum import Enum, auto
import logging
import time
import math

from modules.device.device import discover


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


class Supervisor:
    def __init__(self, settings, backend, telemetry, stop, *, discovery=discover, clock=time.monotonic,
                 control=None, publish_state=None):
        self.settings, self.backend, self.telemetry, self.stop = settings, backend, telemetry, stop
        self.discovery, self.clock = discovery, clock
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

    def transition(self, state):
        if self.state != state:
            self.log.info("State %s -> %s", self.state.name, state.name)
            self.state = state
            if state in {State.WAIT_REGISTRATION, State.MONITOR, State.ONLINE}:
                self.next_sample = self.clock()
            if state in {State.MONITOR, State.ONLINE}:
                self.last_error = self.failed_state = None

    def cleanup(self):
        try:
            self.backend.close()
        except Exception:
            self.log.exception("Connection cleanup failed")

    def fail(self, error):
        self.log.warning("Connection step %s failed: %s", self.state.name, error)
        self.last_error = str(error)
        self.failed_state = self.state.name
        self.cleanup()
        retry = self.settings.recovery
        delay = min(retry.retry_initial * 2 ** min(self.failures, 20), retry.retry_max)
        self.failures += 1
        self.deadline = self.clock() + delay
        self.transition(State.BACKOFF)
        self.log.info("Retry in %.1f seconds", delay)

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
            if self.backend.registered():
                self.transition(State.CONNECT)
            elif self.clock() >= self.deadline:
                raise TimeoutError("Network registration timed out; check SIM, PIN, coverage and provisioning")
        elif self.state == State.CONNECT:
            self.backend.connect()
            self.transition(State.CONFIGURE_IP)
        elif self.state == State.CONFIGURE_IP:
            self.backend.configure_ip()
            if not self.backend.healthy():
                raise RuntimeError("Bearer connected but host IPv4 configuration is incomplete")
            self.failures = 0
            self.next_health = self.clock() + self.settings.recovery.poll_interval
            self.transition(State.ONLINE)
        elif self.state in {State.ONLINE, State.MONITOR}:
            if self.state == State.ONLINE:
                maintain = getattr(self.backend, 'maintain_network', None)
                if maintain:
                    maintain()
            if self.clock() >= self.next_health:
                if not self.backend.present():
                    raise RuntimeError("USB modem removed or its device path changed")
                if self.state == State.ONLINE and not self.backend.healthy():
                    raise RuntimeError("Data session or host IPv4 configuration lost")
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
