"""Verified failover changes only the owned host route, never the bearer or Ethernet."""
import copy
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from modules.network.manager import Network
from modules.runtime.settings import NetworkSettings
from tests.network.test_route_policy import Kernel, UINT32_MAX, VALUES, foreign_route


class FakeMonitor:
    def __init__(self, settings, interface, address, ifindex, clock):
        self.settings, self.interface, self.address, self.ifindex = settings, interface, address, ifindex
        self.clock = clock
        self.started = self.closed = False
        self.result = {'state': 'checking', 'checked_monotonic': None, 'target': None, 'error': None,
                       'last_success_monotonic': None, 'duration_ms': None,
                       'consecutive_successes': 0, 'consecutive_failures': 0, 'sequence': 0}

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def snapshot(self):
        return copy.deepcopy(self.result)

    def report(self, state, *, age=0):
        self.result.update(state=state, checked_monotonic=self.clock() - age, target='1.1.1.1:443',
                           error='fixture probe unavailable' if state == 'down' else None,
                           consecutive_successes=2 if state == 'up' else 0,
                           consecutive_failures=2 if state == 'down' else 0,
                           sequence=self.result['sequence'] + 1)
        if state == 'up':
            self.result['last_success_monotonic'] = self.clock() - age


class ConnectivityKernel(Kernel):
    def __init__(self, routes):
        super().__init__(routes)
        self.dns = {'eth0': {'servers': ['192.0.2.53'], 'domain': '~.', 'default_route': True}}
        self.dns_commands = []

    def execute(self, args):
        if args[0] == 'resolvectl':
            self.dns_commands.append(args)
            interface = args[2]
            if args[1] == 'revert':
                self.dns.pop(interface, None)
            else:
                entry = self.dns.setdefault(interface, {})
                if args[1] == 'dns':
                    entry['servers'] = list(args[3:])
                elif args[1] == 'domain':
                    entry['domain'] = args[3]
                elif args[1] == 'default-route':
                    entry['default_route'] = args[3] == 'yes'
                else:
                    raise AssertionError(args)
            return ''
        return super().execute(args)


class ConnectivityFailoverTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.monitors = []

    def clock(self):
        return self.now

    def make_network(self, policy='primary', metric=100, routes=None, dns='none', enabled=True, default_route=True):
        kernel = ConnectivityKernel([foreign_route(100)] if routes is None else routes)
        runner = patch('modules.network.manager.subprocess.run', side_effect=kernel.run)
        runner.start()
        self.addCleanup(runner.stop)
        tools = patch('modules.network.manager.shutil.which', return_value='/usr/bin/fixture-tool')
        tools.start()
        self.addCleanup(tools.stop)
        def factory(settings, interface, address, ifindex):
            monitor = FakeMonitor(settings, interface, address, ifindex, self.clock)
            self.monitors.append(monitor)
            return monitor
        settings = NetworkSettings(route_policy=policy, route_metric=metric, dns=dns,
                                   default_route=default_route, connectivity_check=enabled)
        network = Network(settings, 5, threading.Event(), monitor_factory=factory, clock=self.clock)
        network.attach('wwan0')
        self.assertEqual(self.monitors, [], 'Attaching must never start a probe before an address exists')
        network.command('ip', 'link', 'set', 'dev', 'wwan0', 'up')
        return network, kernel

    def configure(self, network):
        values = dict(VALUES, dns=['1.1.1.1'])
        network.configure(values)
        return values

    def status(self, network, state):
        self.monitors[-1].report(state)
        return network.maintain_connectivity()

    def assert_foreign_untouched(self, kernel, expected):
        self.assertEqual([route for route in kernel.routes if route['dev'] != 'wwan0'], expected)
        self.assertEqual(kernel.dns['eth0'], {'servers': ['192.0.2.53'], 'domain': '~.', 'default_route': True})
        for command in kernel.commands:
            if '-j' not in command:
                self.assertNotIn('eth0', command)
                self.assertNotIn('eth1', command)
            self.assertNotIn('flush', command)
            self.assertNotIn('replace', command)

    def test_unverified_wwan_is_behind_every_foreign_route_and_probe_is_bound_to_its_identity(self):
        foreign = [foreign_route(100), foreign_route(700, 'eth1')]
        network, kernel = self.make_network(routes=foreign)
        self.configure(network)
        self.assertEqual(network.route_metric, 701)
        monitor = self.monitors[-1]
        self.assertTrue(monitor.started)
        self.assertEqual((monitor.interface, monitor.address, monitor.ifindex), ('wwan0', '198.18.0.6', 23))
        self.assertEqual(network.connectivity_status['state'], 'checking')
        self.assertEqual(network.connectivity_status['route_mode'], 'fallback')
        self.assertTrue(network.healthy())
        self.assert_foreign_untouched(kernel, foreign)

    def test_verified_success_promotes_failure_demotes_and_recovery_promotes_without_touching_ip(self):
        foreign = [foreign_route(100)]
        network, kernel = self.make_network(routes=foreign)
        self.configure(network)
        self.assertGreater(network.route_metric, 100)
        self.status(network, 'up')
        self.assertEqual(network.route_metric, 99)
        self.status(network, 'down')
        self.assertGreater(network.route_metric, 100)
        self.assertTrue(network.healthy())
        self.status(network, 'up')
        self.assertEqual(network.route_metric, 99)
        self.assertEqual(kernel.addresses, {'198.18.0.6/30'})
        self.assertEqual(sum(command[:3] == ('ip', 'addr', 'add') for command in kernel.commands), 1)
        self.assertFalse(any(command[:3] == ('ip', 'addr', 'del') for command in kernel.commands))
        self.assertFalse(self.monitors[0].closed)
        self.assert_foreign_untouched(kernel, foreign)

    def test_fixed_and_backup_policies_are_restored_only_after_verification(self):
        for policy, requested, foreign_metric, expected in [('fixed', 50, 100, 50), ('backup', 700, 800, 801)]:
            with self.subTest(policy=policy):
                self.monitors = []
                network, kernel = self.make_network(policy, requested, [foreign_route(foreign_metric)])
                self.configure(network)
                self.assertGreater(network.route_metric, foreign_metric)
                self.status(network, 'up')
                self.assertEqual(network.route_metric, expected)
                self.assertEqual(network.connectivity_status['route_mode'], 'requested')
                self.status(network, 'down')
                self.assertGreater(network.route_metric, foreign_metric)
                self.assertTrue(network.healthy())
                self.assertEqual(kernel.addresses, {'198.18.0.6/30'})

    def test_stale_success_is_not_evidence_for_keeping_a_preferred_default(self):
        network, _ = self.make_network()
        self.configure(network)
        self.status(network, 'up')
        self.assertEqual(network.route_metric, 99)
        self.now += 3600
        network.maintain_connectivity()
        self.assertTrue(network.connectivity_status['stale'])
        self.assertGreater(network.route_metric, 100)
        self.assertTrue(network.healthy())

    def test_success_without_a_checked_timestamp_is_not_trusted(self):
        network, _ = self.make_network()
        self.configure(network)
        self.monitors[-1].report('up')
        self.monitors[-1].result['checked_monotonic'] = None
        network.maintain_connectivity()
        self.assertGreater(network.route_metric, 100)
        self.assertFalse(network.connectivity_status['preferred'])

    def test_a_new_address_starts_a_new_check_instead_of_reusing_previous_success(self):
        network, kernel = self.make_network()
        values = self.configure(network)
        self.status(network, 'up')
        previous = self.monitors[-1]
        network.configure(dict(values, address='198.18.0.10', gateway='198.18.0.9'))
        self.assertTrue(previous.closed)
        self.assertEqual(len(self.monitors), 2)
        self.assertEqual(self.monitors[-1].address, '198.18.0.10')
        self.assertTrue(self.monitors[-1].started)
        self.assertGreater(network.route_metric, 100)
        self.assertEqual(kernel.addresses, {'198.18.0.10/30'})

    def test_idempotent_maintenance_does_not_restart_worker_or_duplicate_routes(self):
        network, kernel = self.make_network()
        values = self.configure(network)
        self.status(network, 'up')
        additions = len(kernel.add_attempts)
        for _ in range(5):
            network.maintain_connectivity()
        network.configure(values)
        self.assertEqual(len(self.monitors), 1)
        self.assertEqual(len(kernel.add_attempts), additions)
        self.assertEqual(network.route_metric, 99)

    def test_changing_probe_targets_discards_the_old_verification(self):
        network, _ = self.make_network()
        values = self.configure(network)
        self.status(network, 'up')
        previous = self.monitors[-1]
        network.settings = replace(network.settings, connectivity_targets='8.8.8.8:443')
        network.configure(values)
        self.assertTrue(previous.closed)
        self.assertEqual(len(self.monitors), 2)
        self.assertEqual(self.monitors[-1].settings.connectivity_targets, '8.8.8.8:443')
        self.assertGreater(network.route_metric, 100)

    def test_reused_interface_index_is_rejected_before_any_new_host_mutation(self):
        network, kernel = self.make_network()
        values = self.configure(network)
        self.status(network, 'up')
        # An unplug/replacement removes the old device's owned resources.
        kernel.link['ifindex'] = 24
        kernel.addresses.clear()
        kernel.routes = [route for route in kernel.routes if route['dev'] != 'wwan0']
        before = len(kernel.commands)
        with self.assertRaisesRegex(RuntimeError, r'(?i)(ifindex|interface|changed|reus)'):
            network.configure(values)
        self.assertFalse(any('-j' not in command for command in kernel.commands[before:]))
        self.assertEqual(kernel.addresses, set())

    def test_fallback_reconciles_new_foreign_routes_on_the_maintenance_cadence(self):
        network, kernel = self.make_network()
        self.configure(network)
        kernel.routes.append(foreign_route(900, 'eth1'))
        self.now += 4
        network.maintain_connectivity()
        self.assertGreater(network.route_metric, 900)
        self.status(network, 'up')
        self.assertEqual(network.route_metric, 99)

    def test_unrepresentable_backup_metric_removes_only_our_default_and_keeps_ip_healthy(self):
        foreign = [foreign_route(UINT32_MAX)]
        network, kernel = self.make_network(routes=foreign)
        self.configure(network)
        self.assertIsNone(network.route_metric)
        self.assertEqual(network.connectivity_status['route_mode'], 'blocked')
        self.assertTrue(network.connectivity_status['route_error'])
        self.assertEqual(kernel.addresses, {'198.18.0.6/30'})
        self.assertTrue(network.healthy())
        self.assert_foreign_untouched(kernel, foreign)

    def test_a_new_uint32max_route_after_success_cannot_leave_wwan_preferred_when_down(self):
        network, kernel = self.make_network()
        self.configure(network)
        self.status(network, 'up')
        kernel.routes.append(foreign_route(UINT32_MAX, 'eth1'))
        self.status(network, 'down')
        self.assertIsNone(network.route)
        self.assertEqual(network.connectivity_status['route_mode'], 'blocked')
        self.assertTrue(network.healthy())
        self.assertEqual(kernel.addresses, {'198.18.0.6/30'})

    def test_fallback_add_collision_is_retried_without_claiming_the_racing_route(self):
        network, kernel = self.make_network()
        def race_once(current, route):
            if len(current.add_attempts) == 1:
                current.routes.append(foreign_route(route['metric'], 'eth1'))
        kernel.before_add = race_once
        self.configure(network)
        self.assertEqual(network.route_metric, 102)
        self.assertEqual([route['metric'] for route in kernel.routes if route['dev'] == 'eth1'], [101])
        self.assertEqual(len(kernel.add_attempts), 2)
        self.assertTrue(network.healthy())

    def test_a_conflicting_fixed_promotion_retains_safe_fallback_without_reconnecting(self):
        network, kernel = self.make_network('fixed', 100)
        self.configure(network)
        self.status(network, 'up')
        self.assertGreater(network.route_metric, 100)
        self.assertTrue(network.connectivity_status['route_error'])
        self.assertFalse(network.connectivity_status['preferred'])
        self.assertTrue(network.healthy())
        self.assertEqual(kernel.addresses, {'198.18.0.6/30'})

    def test_dns_capture_is_enabled_only_while_verified_and_actually_preferred(self):
        network, kernel = self.make_network(dns='resolvectl')
        self.configure(network)
        self.assertEqual(kernel.dns_commands, [])
        self.status(network, 'up')
        self.assertEqual(kernel.dns['wwan0']['domain'], '~.')
        self.assertTrue(kernel.dns['wwan0']['default_route'])
        self.status(network, 'down')
        self.assertNotIn('wwan0', kernel.dns)
        self.assertIn(('resolvectl', 'revert', 'wwan0'), kernel.dns_commands)
        self.assertTrue(network.healthy())
        self.assert_foreign_untouched(kernel, [foreign_route(100)])

    def test_verified_backup_does_not_capture_dns_from_the_preferred_ethernet(self):
        network, kernel = self.make_network('backup', 700, dns='resolvectl')
        self.configure(network)
        self.status(network, 'up')
        self.assertEqual(network.route_metric, 700)
        self.assertFalse(network.connectivity_status['preferred'])
        self.assertNotIn('wwan0', kernel.dns)
        self.assertEqual(kernel.dns_commands, [])

    def test_partial_dns_apply_failure_demotes_before_revoke_and_recovers_without_reconnecting(self):
        network, kernel = self.make_network(dns='resolvectl')
        self.configure(network)
        kernel.refuse = lambda args: args[:2] == ('resolvectl', 'default-route')
        before = len(kernel.commands)
        result = self.status(network, 'up')
        self.assertIn('Operation not permitted', result['dns_error'])
        self.assertEqual(result['route_mode'], 'fallback')
        self.assertFalse(result['preferred'])
        self.assertGreater(network.route_metric, 100)
        self.assertIsNone(network.dns_backend)
        self.assertNotIn('wwan0', kernel.dns)
        commands = kernel.commands[before:]
        self.assertIn(('resolvectl', 'domain', 'wwan0', '~.'), commands,
                      'Exercise a partial update that had already captured the DNS domain')
        demotion = next(index for index, command in enumerate(commands)
                        if command[:3] == ('ip', 'route', 'add') and command[-1] == '101')
        self.assertLess(demotion, commands.index(('resolvectl', 'revert', 'wwan0')))
        self.assertTrue(network.healthy())
        self.assertEqual(kernel.addresses, {'198.18.0.6/30'})
        self.assert_foreign_untouched(kernel, [foreign_route(100)])
        kernel.refuse = None
        self.now += 4
        result = network.maintain_connectivity()
        self.assertEqual(network.route_metric, 99)
        self.assertEqual(result['route_mode'], 'requested')
        self.assertTrue(result['preferred'])
        self.assertIsNone(result['dns_error'])
        self.assertEqual(kernel.dns['wwan0'], {'servers': ['1.1.1.1'], 'domain': '~.', 'default_route': True})
        self.assertEqual(sum(command[:3] == ('ip', 'addr', 'add') for command in kernel.commands), 1)
        self.assertFalse(any(command[:3] == ('ip', 'addr', 'del') for command in kernel.commands))
        self.assertEqual(len(self.monitors), 1)
        self.assertFalse(self.monitors[0].closed)
        self.assert_foreign_untouched(kernel, [foreign_route(100)])

    def test_partial_dns_apply_and_revoke_errors_are_both_retained_with_safe_route_fallback(self):
        network, kernel = self.make_network(dns='resolvectl')
        self.configure(network)
        kernel.refuse = lambda args: args[0] == 'resolvectl' and args[1] in {'default-route', 'revert'}
        result = self.status(network, 'up')
        self.assertIn('default-route', result['dns_error'])
        self.assertIn('DNS rollback failed', result['dns_error'])
        self.assertIn('revert', result['dns_error'])
        self.assertEqual(result['route_mode'], 'fallback')
        self.assertFalse(result['preferred'])
        self.assertGreater(network.route_metric, 100)
        self.assertEqual(network.dns_backend, 'resolvectl', 'Keep ownership of the unconfirmed partial DNS update')
        self.assertEqual(kernel.addresses, {'198.18.0.6/30'})
        self.assertTrue(network.healthy())
        self.assert_foreign_untouched(kernel, [foreign_route(100)])

    def test_a_failed_policy_change_releases_previously_preferred_wwan_dns(self):
        network, kernel = self.make_network(dns='resolvectl')
        values = self.configure(network)
        self.status(network, 'up')
        self.assertEqual(kernel.dns['wwan0']['domain'], '~.')
        network.settings = replace(network.settings, route_policy='fixed', route_metric=100)
        network.configure(values)
        self.assertTrue(network.connectivity_status['route_error'])
        self.assertGreater(network.route_metric, 100)
        self.assertNotIn('wwan0', kernel.dns)
        self.assertIsNone(network.dns_backend)
        self.assertTrue(network.healthy())

    def test_failed_dns_release_is_reported_and_retried_without_removing_ip(self):
        network, kernel = self.make_network(dns='resolvectl')
        self.configure(network)
        self.status(network, 'up')
        kernel.refuse = lambda args: args[:2] == ('resolvectl', 'revert')
        self.status(network, 'down')
        self.assertTrue(network.connectivity_status['dns_error'])
        self.assertEqual(network.dns_backend, 'resolvectl')
        self.assertEqual(kernel.addresses, {'198.18.0.6/30'})
        kernel.refuse = None
        self.now += 4
        network.maintain_connectivity()
        self.assertNotIn('wwan0', kernel.dns)
        self.assertFalse(network.connectivity_status['dns_error'])

    def test_close_stops_worker_and_checkpoint_never_persists_a_verified_success(self):
        network, kernel = self.make_network()
        self.configure(network)
        self.status(network, 'up')
        checkpoint = copy.deepcopy(network.checkpoint())
        self.assertFalse(any('connectivity' in key or 'monitor' in key for key in checkpoint))
        network.close()
        self.assertTrue(self.monitors[-1].closed)
        self.assertEqual(kernel.addresses, set())
        self.assertEqual(kernel.routes, [foreign_route(100)])
        self.assertIsNone(network.route)

    def test_recovered_ownership_is_cleaned_without_restarting_or_trusting_an_old_probe(self):
        network, kernel = self.make_network()
        self.configure(network)
        self.status(network, 'up')
        checkpoint = copy.deepcopy(network.checkpoint())
        # A successor restores owned host resources, never the predecessor's worker.
        previous = self.monitors[-1]
        previous.close()
        def unexpected_probe(*args):
            self.fail('Recovery cleanup must not start a connectivity worker')
        recovered = Network(network.settings, 5, threading.Event(), monitor_factory=unexpected_probe, clock=self.clock)
        recovered.restore(checkpoint)
        self.assertEqual(recovered.route_metric, 99)
        recovered.close()
        self.assertEqual(kernel.routes, [foreign_route(100)])
        self.assertEqual(kernel.addresses, set())

    def test_disabled_check_uses_original_route_policy_without_starting_a_worker(self):
        network, _ = self.make_network(enabled=False)
        self.configure(network)
        self.assertEqual(network.route_metric, 99)
        self.assertEqual(self.monitors, [])

    def test_no_default_route_never_installs_a_global_route_or_dns_capture(self):
        network, kernel = self.make_network(default_route=False, dns='resolvectl')
        self.configure(network)
        network.maintain_connectivity()
        self.assertIsNone(network.route)
        self.assertTrue(network.healthy())
        self.assertEqual(kernel.dns_commands, [])
        self.assertEqual(kernel.routes, [foreign_route(100)])


if __name__ == '__main__':
    unittest.main()
