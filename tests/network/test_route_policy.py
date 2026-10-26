"""Stateful kernel simulation: foreign routes compete but are never owned."""
import copy
import ipaddress
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from modules.network.manager import Network
from modules.runtime.settings import NetworkSettings
from modules.runtime.state import RuntimeState


UINT32_MAX = 2 ** 32 - 1
VALUES = {'address': '198.18.0.6', 'netmask': '255.255.255.252',
          'gateway': '198.18.0.5', 'mtu': 1428, 'dns': []}


def foreign_route(metric, interface='eth0'):
    return {'dst': 'default', 'gateway': '192.0.2.1', 'dev': interface,
            'metric': metric, 'protocol': 'dhcp'}


class Kernel:
    """Minimal route/address state, including actual add-exclusivity semantics."""
    def __init__(self, routes=()):
        self.routes = copy.deepcopy(list(routes))
        self.addresses = set()
        self.link = {'ifindex': 23, 'mtu': 1500, 'flags': []}
        self.commands = []
        self.calls = []
        self.add_attempts = []
        self.before_add = None
        self.refuse = None

    @staticmethod
    def value(args, name, default=None):
        return args[args.index(name) + 1] if name in args else default

    @staticmethod
    def error(args, message):
        raise subprocess.CalledProcessError(2, args, output='', stderr='RTNETLINK answers: ' + message + '\n')

    def run(self, args, **kwargs):
        args = tuple(args)
        self.commands.append(args)
        self.calls.append(kwargs)
        if self.refuse and self.refuse(args):
            self.error(args, 'Operation not permitted')
        result = self.execute(args)
        return subprocess.CompletedProcess(args, 0, stdout=result, stderr='')

    def execute(self, args):
        if args[:5] == ('ip', '-j', 'link', 'show', 'dev'):
            return json.dumps([self.link])
        if args[:6] == ('ip', '-j', '-4', 'addr', 'show', 'dev'):
            info = [{'local': value.split('/')[0], 'prefixlen': int(value.split('/')[1]),
                     'scope': 'global'} for value in sorted(self.addresses)]
            return json.dumps([{'addr_info': info}])
        if args[:5] == ('ip', '-j', '-4', 'route', 'show'):
            routes = self.routes
            if 'dev' in args:
                routes = [route for route in routes if route['dev'] == self.value(args, 'dev')]
            return json.dumps(routes)
        if args[:3] == ('ip', 'addr', 'add'):
            ipaddress.IPv4Interface(args[3])
            if args[3] in self.addresses:
                self.error(args, 'File exists')
            self.addresses.add(args[3])
            return ''
        if args[:3] == ('ip', 'addr', 'del'):
            if args[3] not in self.addresses:
                self.error(args, 'Cannot assign requested address')
            self.addresses.remove(args[3])
            return ''
        if args[:3] == ('ip', 'link', 'set'):
            if 'mtu' in args:
                self.link['mtu'] = int(self.value(args, 'mtu'))
            if args[-1] in ('up', 'down'):
                self.link['flags'] = ['UP'] if args[-1] == 'up' else []
            return ''
        if args[:3] in (('ip', 'route', 'add'), ('ip', 'route', 'del')):
            route = {'dst': 'default', 'dev': self.value(args, 'dev'),
                     'metric': int(self.value(args, 'metric', 0))}
            if 'via' in args:
                route['gateway'] = self.value(args, 'via')
            if args[2] == 'add':
                self.add_attempts.append(copy.deepcopy(route))
                if self.before_add:
                    self.before_add(self, route)
                if any(item.get('metric', 0) == route['metric'] for item in self.routes):
                    self.error(args, 'File exists')
                self.routes.append(route)
            else:
                match = next((item for item in self.routes if all(item.get(key) == value for key, value in route.items())), None)
                if match is None:
                    self.error(args, 'No such process')
                self.routes.remove(match)
            return ''
        raise AssertionError('Unexpected network command: ' + repr(args))


class RoutePolicyTests(unittest.TestCase):
    def make_network(self, policy='primary', metric=100, routes=(), default_route=True):
        kernel = Kernel(routes)
        runner = patch('modules.network.manager.subprocess.run', side_effect=kernel.run)
        runner.start()
        self.addCleanup(runner.stop)
        settings = NetworkSettings(route_policy=policy, route_metric=metric, dns='none', default_route=default_route,
                                   connectivity_check=False)
        network = Network(settings, 5, threading.Event())
        network.attach('wwan0')
        network.command('ip', 'link', 'set', 'dev', 'wwan0', 'up')
        return network, kernel

    def assert_foreign_unchanged(self, kernel, before):
        self.assertEqual([route for route in kernel.routes if route['dev'] != 'wwan0'], before)
        self.assertFalse(any('flush' in args or 'replace' in args for args in kernel.commands))
        mutations = [args for args in kernel.commands if '-j' not in args]
        self.assertFalse(any('eth0' in args or 'eth1' in args for args in mutations))

    def test_primary_equal_foreign_metric_selects_a_strictly_preferred_route(self):
        before = [foreign_route(100)]
        network, kernel = self.make_network(routes=before)
        network.configure(VALUES)
        self.assertEqual(network.route_metric, 99)
        self.assertTrue(network.healthy())
        self.assert_foreign_unchanged(kernel, before)

    def test_primary_is_preferred_to_an_already_lower_foreign_metric(self):
        network, _ = self.make_network(routes=[foreign_route(50)])
        network.configure(VALUES)
        self.assertEqual(network.route_metric, 49)

    def test_primary_requested_metric_is_a_ceiling(self):
        network, _ = self.make_network(metric=50, routes=[foreign_route(100)])
        network.configure(VALUES)
        self.assertEqual(network.route_metric, 50)

    def test_backup_equal_foreign_metric_selects_a_strictly_less_preferred_route(self):
        before = [foreign_route(700)]
        network, kernel = self.make_network('backup', 700, before)
        network.configure(VALUES)
        self.assertEqual(network.route_metric, 701)
        self.assertTrue(network.healthy())
        self.assert_foreign_unchanged(kernel, before)

    def test_backup_requested_metric_is_a_floor(self):
        network, _ = self.make_network('backup', 700, [foreign_route(100)])
        network.configure(VALUES)
        self.assertEqual(network.route_metric, 700)

    def test_fixed_collision_exposes_kernel_stderr_and_does_not_claim_foreign_route(self):
        before = [foreign_route(100)]
        network, kernel = self.make_network('fixed', 100, before)
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            network.configure(VALUES)
        self.assertIn('File exists', str(failure.exception))
        self.assertIn('File exists', failure.exception.stderr)
        self.assertEqual(failure.exception.returncode, 2)
        self.assertIsNone(network.route)
        self.assertIsNone(network.route_metric)
        self.assertEqual(len(kernel.add_attempts), 1)
        network.close()
        self.assertEqual(kernel.addresses, set())
        self.assert_foreign_unchanged(kernel, before)

    def test_primary_cannot_outrank_foreign_zero_and_reports_an_actionable_conflict(self):
        network, kernel = self.make_network(routes=[foreign_route(0)])
        with self.assertRaisesRegex(RuntimeError, r'(?i)(metric|priority|primary|prefer|route)'):
            network.configure(VALUES)
        self.assertIsNone(network.route)
        self.assertEqual(kernel.add_attempts, [])

    def test_unspecified_foreign_metric_means_zero_not_an_available_preference(self):
        route = foreign_route(0)
        del route['metric']
        network, kernel = self.make_network(routes=[route])
        with self.assertRaisesRegex(RuntimeError, r'(?i)(metric|priority|primary|prefer|route)'):
            network.configure(VALUES)
        self.assertIsNone(network.route)
        self.assertEqual(kernel.add_attempts, [])

    def test_requested_primary_zero_is_valid_when_a_strictly_higher_foreign_metric_exists(self):
        network, _ = self.make_network(metric=0, routes=[foreign_route(100)])
        network.configure(VALUES)
        self.assertEqual(network.route_metric, 0)
        self.assertTrue(network.healthy())

    def test_backup_cannot_go_beyond_uint32_and_reports_an_actionable_conflict(self):
        network, kernel = self.make_network('backup', 700, [foreign_route(UINT32_MAX)])
        with self.assertRaisesRegex(RuntimeError, r'(?i)(metric|priority|backup|route)'):
            network.configure(VALUES)
        self.assertIsNone(network.route)
        self.assertEqual(kernel.add_attempts, [])

    def test_own_effective_route_is_excluded_from_policy_recalculation(self):
        network, kernel = self.make_network(routes=[foreign_route(100)])
        network.configure(VALUES)
        network.configure(VALUES)
        self.assertTrue(network.healthy())
        self.assertTrue(network.healthy())
        self.assertEqual(network.route_metric, 99)
        self.assertEqual(len(kernel.add_attempts), 1)
        self.assertEqual(len(kernel.addresses), 1)

    def test_health_rebalances_effective_route_when_foreign_preference_changes(self):
        network, kernel = self.make_network(routes=[foreign_route(100)])
        network.configure(VALUES)
        kernel.routes.append(foreign_route(20, 'eth1'))
        before = copy.deepcopy([route for route in kernel.routes if route['dev'] != 'wwan0'])
        self.assertTrue(network.healthy())
        self.assertEqual(network.route_metric, 19)
        self.assertEqual([route['metric'] for route in kernel.routes if route['dev'] == 'wwan0'], [19])
        self.assert_foreign_unchanged(kernel, before)

    def test_effective_route_roundtrips_through_checkpoint_and_cleanup(self):
        before = [foreign_route(100)]
        network, kernel = self.make_network(routes=before)
        with tempfile.TemporaryDirectory() as directory:
            state = RuntimeState(Path(directory) / 'state.json')
            network.changed = lambda: state.save({'network': network.checkpoint()})
            network.configure(VALUES)
            saved = state.load()['network']
            self.assertEqual(saved['route'][saved['route'].index('metric') + 1], '99')
            recovered = Network(network.settings, 5, threading.Event())
            recovered.restore(saved)
            self.assertEqual(recovered.route_metric, 99)
            recovered.close()
        self.assertEqual(kernel.addresses, set())
        self.assertEqual(kernel.link['mtu'], 1500)
        self.assertNotIn('UP', kernel.link['flags'])
        self.assert_foreign_unchanged(kernel, before)

    def test_eexist_race_reloads_routes_and_never_adopts_the_racing_foreign_route(self):
        before = [foreign_route(100)]
        network, kernel = self.make_network(routes=before)
        def race_once(current, route):
            if len(current.add_attempts) == 1:
                current.routes.append(foreign_route(route['metric'], 'eth1'))
        kernel.before_add = race_once
        network.configure(VALUES)
        self.assertEqual(network.route_metric, 98)
        self.assertEqual(len(kernel.add_attempts), 2)
        self.assertEqual([route['metric'] for route in kernel.routes if route['dev'] == 'eth1'], [99])
        network.close()
        self.assertEqual([route['metric'] for route in kernel.routes if route['dev'] == 'eth1'], [99])

    def test_persistent_eexist_race_has_a_bounded_retry_budget(self):
        network, kernel = self.make_network(routes=[foreign_route(100)])
        kernel.before_add = lambda current, route: current.routes.append(foreign_route(route['metric'], 'eth1'))
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            network.configure(VALUES)
        self.assertEqual(len(kernel.add_attempts), 3)
        self.assertIsNone(network.route)
        self.assertEqual([route for route in kernel.routes if route['dev'] == 'wwan0'], [])
        foreign_before_close = copy.deepcopy(kernel.routes)
        network.close()
        self.assertEqual(kernel.routes, foreign_before_close)

    def test_unrelated_kernel_error_is_visible_and_never_retried_as_a_collision(self):
        before = [foreign_route(100)]
        network, kernel = self.make_network(routes=before)
        kernel.refuse = lambda args: args[:3] == ('ip', 'route', 'add')
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            network.configure(VALUES)
        self.assertIn('Operation not permitted', str(failure.exception))
        self.assertIsNone(network.route)
        self.assertEqual(sum(args[:3] == ('ip', 'route', 'add') for args in kernel.commands), 1)
        self.assert_foreign_unchanged(kernel, before)

    def test_no_default_route_does_not_modify_foreign_policy(self):
        before = [foreign_route(0)]
        network, kernel = self.make_network(routes=before, default_route=False)
        network.configure(VALUES)
        self.assertTrue(network.healthy())
        self.assertIsNone(network.route_metric)
        self.assertFalse(any('route' in args and '-j' not in args for args in kernel.commands))
        self.assert_foreign_unchanged(kernel, before)

    def test_foreign_route_with_identical_gateway_and_metric_is_never_adopted(self):
        before = [{'dst': 'default', 'dev': 'wwan0', 'gateway': VALUES['gateway'], 'metric': 100}]
        network, kernel = self.make_network('fixed', 100, before)
        with self.assertRaises(subprocess.CalledProcessError):
            network.configure(VALUES)
        self.assertIsNone(network.route)
        network.close()
        self.assertEqual(kernel.routes, before)

    def test_failed_address_removal_retains_ownership_and_raises_for_checkpoint_retention(self):
        network, kernel = self.make_network(default_route=False)
        network.configure(VALUES)
        kernel.refuse = lambda args: args[:3] == ('ip', 'addr', 'del')
        with self.assertRaises((RuntimeError, subprocess.SubprocessError)):
            network.close()
        self.assertEqual(network.checkpoint()['interface'], 'wwan0')
        self.assertEqual(network.checkpoint()['ifindex'], 23)
        self.assertEqual(network.checkpoint()['address'], '198.18.0.6/30')
        self.assertIn('198.18.0.6/30', kernel.addresses)
        kernel.refuse = None
        network.close()
        self.assertEqual(kernel.addresses, set())
        self.assertIsNone(network.address)

    def test_failed_route_removal_retains_exact_effective_route_without_losing_foreign_routes(self):
        before = [foreign_route(100)]
        network, kernel = self.make_network(routes=before)
        network.configure(VALUES)
        owned_route = copy.deepcopy(network.route)
        kernel.refuse = lambda args: args[:3] == ('ip', 'route', 'del')
        with self.assertRaises((RuntimeError, subprocess.SubprocessError)):
            network.close()
        self.assertEqual(network.route, owned_route)
        self.assertEqual(network.route_metric, 99)
        self.assertEqual(network.checkpoint()['interface'], 'wwan0')
        self.assert_foreign_unchanged(kernel, before)
        kernel.refuse = None
        network.close()
        self.assertEqual(kernel.routes, before)


if __name__ == '__main__':
    unittest.main()
