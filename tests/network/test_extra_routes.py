"""network.routes: owned routes through the modem beside the default route."""
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import patch

from modules.network.manager import Network
from modules.network.routes import parse_routes
from modules.runtime.settings import NetworkSettings, Settings, validate_settings
from tests.network.test_route_policy import VALUES, Kernel, foreign_route


class RoutingKernel(Kernel):
    """The route policy kernel, plus routes to networks other than the default."""
    def execute(self, args):
        if args[:5] == ('ip', '-j', '-4', 'route', 'show'):
            routes = self.routes if 'default' not in args else [route for route in self.routes if route['dst'] == 'default']
            return json.dumps(routes)
        if args[:3] in (('ip', 'route', 'add'), ('ip', 'route', 'del')):
            route = {'dst': args[3], 'dev': self.value(args, 'dev'), 'metric': int(self.value(args, 'metric', 0))}
            if 'via' in args:
                route['gateway'] = self.value(args, 'via')
            if args[2] == 'add':
                self.add_attempts.append(dict(route))
                # The kernel keys a route on its destination and metric, whatever the device.
                if any(item['dst'] == route['dst'] and item.get('metric', 0) == route['metric'] for item in self.routes):
                    self.error(args, 'File exists')
                self.routes.append(route)
            else:
                match = next((item for item in self.routes if all(item.get(key) == value for key, value in route.items())), None)
                if match is None:
                    self.error(args, 'No such process')
                self.routes.remove(match)
            return ''
        return super().execute(args)


class ExtraRouteTests(unittest.TestCase):
    def make_network(self, routes='192.168.4.0/24,10.20.0.0/16', kernel_routes=()):
        kernel = RoutingKernel(kernel_routes)
        runner = patch('modules.network.manager.subprocess.run', side_effect=kernel.run)
        runner.start()
        self.addCleanup(runner.stop)
        settings = NetworkSettings(route_policy='fixed', route_metric=700, dns='none', connectivity_check=False, routes=routes)
        network = Network(settings, 5, threading.Event())
        network.attach('wwan0')
        network.command('ip', 'link', 'set', 'dev', 'wwan0', 'up')
        return network, kernel

    @staticmethod
    def wwan(kernel):
        return sorted((route['dst'], route.get('gateway'), route['metric']) for route in kernel.routes if route['dev'] == 'wwan0')

    def test_routes_follow_the_ipv4_configuration_and_leave_with_the_connection(self):
        network, kernel = self.make_network()
        network.configure(VALUES)
        self.assertEqual(self.wwan(kernel), [('10.20.0.0/16', '198.18.0.5', 700), ('192.168.4.0/24', '198.18.0.5', 700),
                                             ('default', '198.18.0.5', 700)])
        self.assertEqual(network.connectivity_status['routes'], ['192.168.4.0/24', '10.20.0.0/16'])
        self.assertIsNone(network.connectivity_status['routes_error'])
        self.assertEqual(len(network.checkpoint()['extra_routes']), 2, 'a crash must still find them to clean up')
        self.assertTrue(network.healthy())
        network.close()
        self.assertEqual(self.wwan(kernel), [])
        self.assertEqual(network.extra_routes, [])

    def test_a_dropped_route_comes_back_and_a_new_gateway_replaces_the_old_routes(self):
        network, kernel = self.make_network(routes='192.168.4.0/24')
        network.configure(VALUES)
        kernel.routes = [route for route in kernel.routes if route['dst'] != '192.168.4.0/24']
        self.assertTrue(network.healthy())
        self.assertIn(('192.168.4.0/24', '198.18.0.5', 700), self.wwan(kernel))
        network.configure({**VALUES, 'address': '198.18.0.10', 'gateway': '198.18.0.9'})
        self.assertEqual([route for route in self.wwan(kernel) if route[0] != 'default'], [('192.168.4.0/24', '198.18.0.9', 700)])
        self.assertEqual(len(network.extra_routes), 1)

    def test_a_foreign_route_with_the_same_metric_is_reported_and_never_claimed(self):
        foreign = {'dst': '192.168.4.0/24', 'dev': 'eth0', 'gateway': '192.0.2.1', 'metric': 700}
        network, kernel = self.make_network(kernel_routes=[foreign_route(100), foreign])
        with self.assertLogs('modules.network.manager', 'WARNING') as logs:
            network.configure(VALUES)
        status = network.connectivity_status
        self.assertIn('already uses metric 700', status['routes_error'])
        self.assertIn('192.168.4.0/24', logs.output[0])
        self.assertEqual(status['routes'], ['10.20.0.0/16'], 'the other network is still routed')
        self.assertTrue(network.healthy(), 'the connection itself stays up')
        network.close()
        self.assertIn(foreign, kernel.routes)

    def test_a_restored_checkpoint_removes_the_routes_of_a_crashed_run(self):
        network, kernel = self.make_network(routes='192.168.4.0/24')
        network.configure(VALUES)
        saved = network.checkpoint()
        recovered = Network(network.settings, 5, threading.Event())
        recovered.restore(saved)
        recovered.close()
        self.assertEqual(self.wwan(kernel), [])

    def test_routes_work_without_a_default_route(self):
        network, kernel = self.make_network(routes='192.168.4.0/24')
        network.settings = replace(network.settings, default_route=False)
        network.configure(VALUES)
        self.assertEqual(self.wwan(kernel), [('192.168.4.0/24', '198.18.0.5', 700)])


class ParseRoutesTests(unittest.TestCase):
    def test_networks_are_normalized_and_bounded(self):
        self.assertEqual(parse_routes(''), ())
        self.assertEqual(parse_routes(' 192.168.4.0/24 , 10.20.0.0/16 '), ('192.168.4.0/24', '10.20.0.0/16'))
        self.assertEqual(parse_routes('192.168.4.6/32'), ('192.168.4.6/32',))
        for value in ('192.168.4.10/24', '192.168.4.0', '0.0.0.0/0', '127.0.0.0/8', '224.0.0.0/4', '0.1.0.0/16',
                      '255.255.255.255/32', '192.168.4.0/24,192.168.4.0/24', 'x', '192.168.4.0/24,',
                      ','.join(f'10.{index}.0.0/16' for index in range(9))):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_routes(value)

    def test_the_setting_is_validated_with_the_rest(self):
        settings = Settings()
        self.assertEqual(settings.network.routes, '')
        validate_settings(replace(settings, network=replace(settings.network, routes='192.168.4.0/24')))
        with self.assertRaisesRegex(ValueError, 'network.routes'):
            validate_settings(replace(settings, network=replace(settings.network, routes='192.168.4.10/24')))


if __name__ == '__main__':
    unittest.main()
