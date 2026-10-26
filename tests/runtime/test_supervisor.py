from dataclasses import replace
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from modules.device.device import Device, DiscoveryError
from modules.runtime.settings import Settings
from modules.runtime.supervisor import Supervisor, State


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', '612EDE63', 'FM160')
        self.discovery = Mock(return_value=self.device)
        self.backend = Mock()
        self.backend.registered.return_value = True
        self.backend.healthy.return_value = True
        self.backend.present.return_value = True
        self.telemetry = Mock()
        self.stop = threading.Event()
        self.machine = Supervisor(Settings(), self.backend, self.telemetry, self.stop,
                                  discovery=self.discovery, clock=lambda: self.now)

    def online(self):
        for _ in range(5):
            self.machine.step()
        self.assertEqual(self.machine.state, State.ONLINE)

    def test_connect_order_and_keep_bearer(self):
        phases = []
        self.telemetry.sample.side_effect = lambda *args, **kwargs: phases.append(self.backend.observation_state)
        self.online()
        self.assertEqual([c[0] for c in self.backend.method_calls],
                         ['open', 'prepare', 'registered', 'connect', 'configure_ip', 'healthy'])
        self.machine.step()
        self.assertEqual(phases, ['WAIT_REGISTRATION', 'ONLINE'])
        self.backend.connect.assert_called_once()
        self.backend.close.assert_not_called()

    def test_wait_for_usb_without_opening(self):
        self.discovery.return_value = None
        self.machine.step()
        self.assertEqual(self.machine.state, State.WAIT_DEVICE)
        self.backend.open.assert_not_called()

    def test_upstream_outage_maintains_routes_each_tick_without_restarting_bearer(self):
        self.online()
        # Local session remains healthy while a separate worker reports no
        # usable upstream. There is no QMI reconnect or wait for a probe here.
        self.backend.maintain_network.return_value = {'state': 'down', 'route_mode': 'fallback'}
        for second in range(5):
            self.now = second
            self.machine.step()
        self.assertEqual(self.backend.maintain_network.call_count, 5)
        self.assertEqual(self.telemetry.sample.call_count, 6)  # registration + five online ticks
        self.assertEqual(self.machine.state, State.ONLINE)
        self.backend.connect.assert_called_once()
        self.backend.close.assert_not_called()
        self.assertEqual(self.backend.healthy.call_count, 1)  # independent ten-second QMI health cadence

    def test_monitor_mode_does_not_run_active_route_or_upstream_checks(self):
        self.machine.state = State.MONITOR
        self.machine.step()
        self.backend.maintain_network.assert_not_called()

    def test_registration_waits_before_connect(self):
        self.backend.registered.return_value = False
        for _ in range(4):
            self.machine.step()
        self.assertEqual(self.machine.state, State.WAIT_REGISTRATION)
        self.backend.connect.assert_not_called()
        self.now = 181
        with self.assertRaises(TimeoutError):
            self.machine.step()

    def waiting(self, *, interval=1):
        self.machine.settings = replace(self.machine.settings,
            telemetry=replace(self.machine.settings.telemetry, interval=interval,
                              collection_budget=min(interval * .8, .8)))
        self.backend.registered.return_value = False
        self.machine.step()
        self.machine.step()
        self.assertEqual(self.machine.state, State.WAIT_REGISTRATION)

    def test_waiting_for_registration_acquires_current_observations_without_connecting(self):
        self.waiting()
        self.machine.step()
        self.telemetry.sample.assert_called_once_with(
            self.backend, self.device, scheduled_monotonic=0, skipped_intervals=0)
        self.assertEqual(self.backend.observation_state, 'WAIT_REGISTRATION')
        self.assertEqual(self.machine.state, State.WAIT_REGISTRATION)
        self.backend.connect.assert_not_called()
        self.backend.configure_ip.assert_not_called()

    def test_registration_observation_uses_interval_without_replaying_missed_ticks(self):
        self.waiting(interval=.25)
        self.machine.step()
        self.now = .1
        self.machine.step()
        self.assertEqual(self.telemetry.sample.call_count, 1)
        self.now = 1.1
        self.machine.step()
        self.assertEqual(self.telemetry.sample.call_count, 2)
        self.assertEqual(self.telemetry.sample.call_args.kwargs['scheduled_monotonic'], 1)
        self.assertEqual(self.telemetry.sample.call_args.kwargs['skipped_intervals'], 3)
        self.assertEqual(self.machine.skipped_intervals, 3)
        self.assertEqual(self.machine.state, State.WAIT_REGISTRATION)

    def test_observation_failure_does_not_abort_registration_or_connect(self):
        self.waiting()
        self.telemetry.sample.side_effect = OSError('telemetry destination unavailable')
        self.machine.step()
        self.assertEqual(self.machine.state, State.WAIT_REGISTRATION)
        self.backend.close.assert_not_called()
        self.backend.connect.assert_not_called()
        self.assertIsNone(self.machine.last_error)

    def test_disabled_telemetry_keeps_registration_checks_without_acquiring_observations(self):
        self.waiting()
        self.machine.settings = replace(self.machine.settings,
            telemetry=replace(self.machine.settings.telemetry, enabled=False))
        self.machine.step()
        self.backend.registered.assert_called_once()
        self.telemetry.sample.assert_not_called()
        self.now = 1
        self.machine.step()
        self.assertEqual(self.backend.registered.call_count, 2)
        self.assertEqual(self.machine.state, State.WAIT_REGISTRATION)

    def test_registration_observation_run_honors_fractional_interval_and_stop(self):
        self.waiting(interval=.25)
        sampled = []
        self.telemetry.sample.side_effect = lambda *args, **kwargs: sampled.append(kwargs['scheduled_monotonic'])
        stop = Mock()
        stop.is_set.side_effect = lambda: len(sampled) >= 3
        stop.wait.side_effect = lambda seconds: setattr(self, 'now', self.now + seconds)
        self.machine.stop = stop
        self.machine.run()
        self.assertEqual(sampled, [0, .25, .5])
        self.backend.connect.assert_not_called()
        self.backend.close.assert_called_once()
        self.assertEqual(self.machine.state, State.STOPPED)

    def test_shutdown_or_backoff_does_not_acquire_more_observations(self):
        self.waiting()
        self.machine.fail(RuntimeError('transport unavailable'))
        self.machine.step()
        self.telemetry.sample.assert_not_called()
        self.stop.set()
        self.machine.run()
        self.telemetry.sample.assert_not_called()
        self.assertEqual(self.machine.state, State.STOPPED)

    def test_stop_during_registration_observation_prevents_further_modem_queries(self):
        self.waiting()
        self.telemetry.sample.side_effect = lambda *args, **kwargs: self.stop.set()
        self.machine.step()
        self.backend.registered.assert_not_called()
        self.backend.connect.assert_not_called()
        self.machine.run()
        self.telemetry.sample.assert_called_once()
        self.backend.close.assert_called_once()
        self.assertEqual(self.machine.state, State.STOPPED)

    def test_apn_failure_backoff_is_capped(self):
        self.backend.connect.side_effect = RuntimeError('CallFailed: APN rejected')
        for i in range(10):
            while self.machine.state != State.CONNECT:
                self.machine.step()
            try:
                self.machine.step()
            except RuntimeError as error:
                self.machine.fail(error)
            self.assertEqual(self.machine.state, State.BACKOFF)
            self.assertEqual(self.machine.deadline - self.now, min(2 * 2 ** i, 60))
            self.now = self.machine.deadline
            self.machine.step()
        self.backend.configure_ip.assert_not_called()

    def test_telemetry_failure_does_not_disconnect(self):
        self.online()
        self.telemetry.sample.side_effect = OSError('Disk full or collector unavailable')
        self.machine.step()
        self.assertEqual(self.machine.state, State.ONLINE)
        self.backend.close.assert_not_called()

    def test_hotplug_renumber_tracks_serial(self):
        self.machine.settings = replace(Settings(), modem=replace(Settings().modem, serial='612EDE63'))
        self.online()
        self.now = 10
        self.backend.present.return_value = False
        with self.assertRaises(RuntimeError) as failure:
            self.machine.step()
        self.machine.fail(failure.exception)
        self.discovery.return_value = replace(self.device, path='/dev/cdc-wdm2', interface='wwan2')
        self.now = self.machine.deadline
        self.machine.step()
        self.machine.step()
        self.discovery.assert_called_with('auto', '612EDE63')
        self.machine.step()
        self.assertEqual(self.backend.open.call_args.args[0].path, '/dev/cdc-wdm2')

    def test_automatic_selection_accepts_replacement_modem(self):
        for original_serial in ('612EDE63', ''):
            with self.subTest(original_serial=original_serial):
                self.setUp()
                self.discovery.return_value = replace(self.device, serial=original_serial)
                self.online()
                self.backend.present.return_value = False
                self.now = 10
                with self.assertRaises(RuntimeError) as failure:
                    self.machine.step()
                self.machine.fail(failure.exception)
                replacement = Device('/dev/cdc-wdm3', 'wwan3', '/sys/usb/2-5',
                                     'replacement', 'Other QMI modem')
                self.discovery.return_value = replacement
                self.backend.present.return_value = True
                self.now = self.machine.deadline
                self.machine.step()
                self.online()
                self.discovery.assert_called_with('auto', '')
                self.assertEqual(self.machine.device, replacement)
                self.assertEqual(self.backend.open.call_args.args[0], replacement)

    def test_explicit_serial_is_kept_after_disconnect(self):
        self.machine.settings = replace(Settings(), modem=replace(Settings().modem, serial='612EDE63'))
        self.online()
        self.machine.fail(RuntimeError('USB modem removed'))
        self.discovery.return_value = None
        self.now = self.machine.deadline
        self.machine.step()
        self.machine.step()
        self.discovery.assert_called_with('auto', '612EDE63')
        self.assertEqual(self.machine.state, State.WAIT_DEVICE)
        self.backend.open.assert_called_once()

    def test_explicit_device_is_kept_after_disconnect(self):
        self.machine.settings = replace(Settings(), modem=replace(Settings().modem, device='/dev/cdc-wdm0'))
        self.online()
        self.machine.fail(RuntimeError('USB modem removed'))
        self.discovery.return_value = None
        self.now = self.machine.deadline
        self.machine.step()
        self.machine.step()
        self.discovery.assert_called_with('/dev/cdc-wdm0', '')
        self.assertEqual(self.machine.state, State.WAIT_DEVICE)

    def test_ambiguous_selection_does_not_open_a_modem(self):
        self.discovery.side_effect = DiscoveryError('Multiple QMI modems found')
        with self.assertRaises(DiscoveryError) as failure:
            self.machine.step()
        self.machine.fail(failure.exception)
        self.assertEqual(self.machine.state, State.BACKOFF)
        self.backend.open.assert_not_called()
        self.discovery.side_effect = None
        self.now = self.machine.deadline
        self.machine.step()
        self.online()

    def test_ip_configuration_is_required_for_online(self):
        self.backend.healthy.return_value = False
        for _ in range(4):
            self.machine.step()
        with self.assertRaises(RuntimeError):
            self.machine.step()
        self.assertNotEqual(self.machine.state, State.ONLINE)

    def test_failure_reason_remains_visible_until_connection_recovers(self):
        self.machine.transition(State.OPEN)
        self.machine.fail(RuntimeError('SIM absent; slot 1: absent'))
        self.assertEqual(self.machine.last_error, 'SIM absent; slot 1: absent')
        self.assertEqual(self.machine.failed_state, 'OPEN')
        self.now = self.machine.deadline
        self.machine.step()
        self.assertIsNotNone(self.machine.last_error)
        self.online()
        self.assertIsNone(self.machine.last_error)
        self.assertIsNone(self.machine.failed_state)

    def test_monitor_never_configures_or_connects(self):
        self.machine.settings = replace(Settings(), modem=replace(Settings().modem, mode='monitor'))
        for _ in range(3):
            self.machine.step()
        self.assertEqual(self.machine.state, State.MONITOR)
        self.backend.prepare.assert_not_called()
        self.backend.connect.assert_not_called()
        self.backend.configure_ip.assert_not_called()

    def test_stop_always_cleans_up(self):
        self.online()
        self.stop.set()
        self.machine.run()
        self.backend.close.assert_called_once()
        self.assertEqual(self.machine.state, State.STOPPED)
