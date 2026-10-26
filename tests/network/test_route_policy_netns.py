"""Optional real-kernel regression; every mutation happens after unshare --net."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from modules.network.manager import Network
from modules.runtime.settings import NetworkSettings
from modules.runtime.state import RuntimeState


ROOT = Path(__file__).resolve().parents[2]
ETHERNET = 'qmi-test-eth'
WWAN = 'qmi-test-ww'
VALUES = {'address': '198.18.0.6', 'netmask': '255.255.255.252',
          'gateway': '198.18.0.5', 'mtu': 1428, 'dns': []}


def ip(*args):
    return subprocess.run(['ip', *args], check=True, text=True, capture_output=True).stdout


def route_records():
    return json.loads(ip('-j', '-4', 'route', 'show', 'table', 'main'))


def connectivity_worker(scenario):
    from tests.network.test_connectivity_failover import FakeMonitor

    configurations = {
        'verified-primary': ('primary', 100, 100, 99),
        'verified-fixed': ('fixed', 100, 200, 100),
        'verified-backup': ('backup', 700, 100, 700),
        'verified-fixed-conflict': ('fixed', 100, 100, None),
        'verified-stale': ('primary', 100, 100, 99),
        'verified-max': ('primary', 100, (1 << 32) - 1, 100),
    }
    policy, metric, foreign_metric, promoted = configurations[scenario]
    for interface in (ETHERNET, WWAN):
        ip('link', 'add', interface, 'type', 'dummy')
    ip('addr', 'add', '198.18.0.2/30', 'dev', ETHERNET)
    ip('link', 'set', 'dev', ETHERNET, 'up')
    ip('route', 'add', 'default', 'via', '198.18.0.1', 'dev', ETHERNET, 'metric', str(foreign_metric))
    foreign_before = [route for route in route_records() if route.get('dev') == ETHERNET]
    now = [1000.0]
    monitors = []
    def factory(settings, interface, address, ifindex):
        monitor = FakeMonitor(settings, interface, address, ifindex, lambda: now[0])
        monitors.append(monitor)
        return monitor
    settings = NetworkSettings(route_policy=policy, route_metric=metric, dns='none', connectivity_check=True)
    network = Network(settings, 5, threading.Event(), monitor_factory=factory, clock=lambda: now[0])
    network.attach(WWAN)
    ip('link', 'set', 'dev', WWAN, 'up')
    network.configure(VALUES)

    def selected_interface():
        return json.loads(ip('-j', '-4', 'route', 'get', '203.0.113.7'))[0]['dev']

    def assert_local_ip_kept():
        assert network.healthy()
        addresses = json.loads(ip('-j', '-4', 'addr', 'show', 'dev', WWAN))
        assert any(address.get('local') == VALUES['address'] for item in addresses for address in item.get('addr_info', []))
        assert 'UP' in json.loads(ip('-j', 'link', 'show', 'dev', WWAN))[0]['flags']
        assert [route for route in route_records() if route.get('dev') == ETHERNET] == foreign_before

    # The kernel must keep Ethernet selected until a verified result exists.
    assert selected_interface() == ETHERNET
    assert_local_ip_kept()
    assert len(monitors) == 1 and monitors[0].started
    assert (monitors[0].interface, monitors[0].address) == (WWAN, VALUES['address'])
    monitors[0].report('up')
    network.maintain_connectivity()
    if promoted is None:
        assert network.connectivity_status['route_error']
        assert network.route_metric > foreign_metric and selected_interface() == ETHERNET
    else:
        assert network.route_metric == promoted
        assert selected_interface() == (ETHERNET if policy == 'backup' else WWAN)
    assert_local_ip_kept()
    if scenario == 'verified-stale':
        now[0] += 3600
        network.maintain_connectivity()
        assert network.connectivity_status['stale']
    else:
        monitors[0].report('down')
        network.maintain_connectivity()
    assert selected_interface() == ETHERNET
    if scenario == 'verified-max':
        assert network.route is None and network.connectivity_status['route_mode'] == 'blocked'
    else:
        assert network.route_metric > foreign_metric
    assert_local_ip_kept()
    monitors[0].report('up')
    network.maintain_connectivity()
    assert_local_ip_kept()
    checkpoint = network.checkpoint()
    assert not any('connectivity' in key or 'monitor' in key for key in checkpoint)
    network.close()
    assert monitors[0].closed
    assert [route for route in route_records() if route.get('dev') == ETHERNET] == foreign_before
    assert not any(route.get('dev') == WWAN and route.get('dst') == 'default' for route in route_records())
    addresses = json.loads(ip('-j', '-4', 'addr', 'show', 'dev', WWAN))
    assert not any(address.get('scope') == 'global' for item in addresses for address in item.get('addr_info', []))
    link = json.loads(ip('-j', 'link', 'show', 'dev', WWAN))[0]
    assert link['mtu'] == 1500 and 'UP' not in link['flags']
    print(json.dumps({'scenario': scenario, 'foreign_routes_preserved': True,
                      'unverified_never_preferred': True, 'failover_kept_local_ip': True}))


def worker(scenario, parent_namespace):
    # A mistaken direct invocation must never create interfaces in the caller's network.
    assert os.readlink('/proc/self/ns/net') != parent_namespace, 'A fresh network namespace is mandatory'
    assert route_records() == [], 'The test requires an empty, isolated main route table'
    if scenario.startswith('verified-'):
        return connectivity_worker(scenario)
    policies = {
        'primary-equal': ('primary', 100, 100, 99, WWAN),
        'primary-lower-foreign': ('primary', 100, 50, 49, WWAN),
        'primary-ceiling': ('primary', 50, 100, 50, WWAN),
        'backup-equal': ('backup', 700, 700, 701, ETHERNET),
        'fixed-collision': ('fixed', 100, 100, None, ETHERNET),
        'health-rebalance': ('primary', 100, 100, 19, WWAN),
    }
    policy, metric, foreign_metric, expected, selected_interface = policies[scenario]
    for interface in (ETHERNET, WWAN):
        ip('link', 'add', interface, 'type', 'dummy')
    ip('addr', 'add', '198.18.0.2/30', 'dev', ETHERNET)
    ip('link', 'set', 'dev', ETHERNET, 'up')
    ip('route', 'add', 'default', 'via', '198.18.0.1', 'dev', ETHERNET, 'metric', str(foreign_metric))
    foreign_before = [route for route in route_records() if route.get('dev') == ETHERNET]
    settings = NetworkSettings(route_policy=policy, route_metric=metric, dns='none', connectivity_check=False)
    network = Network(settings, 5, threading.Event())
    network.attach(WWAN)
    ip('link', 'set', 'dev', WWAN, 'up')
    if scenario == 'fixed-collision':
        try:
            network.configure(VALUES)
        except subprocess.CalledProcessError as error:
            assert error.returncode == 2 and 'File exists' in error.stderr
            assert 'File exists' in str(error), 'Diagnostics must preserve kernel stderr'
            assert network.route is None and network.route_metric is None
        else:
            raise AssertionError('The kernel must reject two fixed default routes with the same priority')
    else:
        network.configure(VALUES)
        if scenario == 'health-rebalance':
            assert network.route_metric == 99
            ip('route', 'add', 'default', 'via', '198.18.0.1', 'dev', ETHERNET, 'metric', '20')
            foreign_before = [route for route in route_records() if route.get('dev') == ETHERNET]
        assert network.healthy(), 'The host configuration must remain healthy after policy reconciliation'
        assert network.route_metric == expected, (network.route_metric, expected)
        # configure() must exclude its own route; it cannot drift down/up each time.
        network.configure(VALUES)
        assert network.route_metric == expected and network.healthy()
        owned = [route for route in route_records() if route.get('dev') == WWAN and route.get('dst') == 'default']
        assert len(owned) == 1 and owned[0].get('metric', 0) == expected
    selected = json.loads(ip('-j', '-4', 'route', 'get', '203.0.113.7'))
    assert selected[0]['dev'] == selected_interface, selected
    with tempfile.TemporaryDirectory(prefix='qmi-route-checkpoint-') as directory:
        state = RuntimeState(Path(directory) / 'state.json')
        state.save({'network': network.checkpoint()})
        recovered = Network(settings, 5, threading.Event())
        recovered.restore(state.load()['network'])
        assert recovered.route_metric == expected
        recovered.close()
    assert [route for route in route_records() if route.get('dev') == ETHERNET] == foreign_before
    assert not any(route.get('dev') == WWAN and route.get('dst') == 'default' for route in route_records())
    addresses = json.loads(ip('-j', '-4', 'addr', 'show', 'dev', WWAN))
    assert not any(address.get('scope') == 'global' for item in addresses for address in item.get('addr_info', []))
    link = json.loads(ip('-j', 'link', 'show', 'dev', WWAN))[0]
    assert link['mtu'] == 1500 and 'UP' not in link['flags']
    print(json.dumps({'scenario': scenario, 'effective_metric': expected, 'selected_interface': selected_interface,
                      'foreign_routes_preserved': True, 'checkpoint_cleanup_restored': True}))


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('unshare') and shutil.which('ip'),
                     'Requires Linux, iproute2 and unshare; no host network changes are permitted')
class RoutePolicyNamespaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = dict(os.environ, LC_ALL='C', PYTHONPATH=str(ROOT))
        try:
            probe = subprocess.run(['unshare', '--user', '--map-root-user', '--net', 'ip', 'link', 'add',
                                    'qmi-probe', 'type', 'dummy'], capture_output=True, text=True,
                                   timeout=10, env=cls.environment, cwd=ROOT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise unittest.SkipTest(f'Isolated network namespaces unavailable: {error}') from error
        if probe.returncode:
            raise unittest.SkipTest('Isolated user/network namespaces unavailable: ' + probe.stderr.strip()[:240])

    def scenario(self, name):
        parent = os.readlink('/proc/self/ns/net')
        result = subprocess.run(['unshare', '--user', '--map-root-user', '--net', sys.executable,
                                 str(Path(__file__).resolve()), '--netns-case', name, parent],
                                text=True, capture_output=True, timeout=25, cwd=ROOT, env=self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['scenario'], name)

    def test_primary_equal_metric_uses_wwan_and_preserves_ethernet_after_checkpoint_cleanup(self):
        self.scenario('primary-equal')

    def test_primary_really_outranks_lower_foreign_metric(self):
        self.scenario('primary-lower-foreign')

    def test_primary_ceiling_is_honored_by_the_kernel(self):
        self.scenario('primary-ceiling')

    def test_backup_equal_metric_keeps_ethernet_selected(self):
        self.scenario('backup-equal')

    def test_fixed_metric_eexist_does_not_claim_or_remove_ethernet(self):
        self.scenario('fixed-collision')

    def test_health_rebalances_against_a_new_foreign_route_without_modem_queries(self):
        self.scenario('health-rebalance')

    def test_unverified_primary_keeps_ethernet_then_success_and_failure_switch_real_egress(self):
        self.scenario('verified-primary')

    def test_verified_fixed_metric_obeys_the_same_failover_gate(self):
        self.scenario('verified-fixed')

    def test_verified_backup_never_steals_the_preferred_ethernet(self):
        self.scenario('verified-backup')

    def test_conflicting_fixed_promotion_keeps_safe_fallback_and_ip(self):
        self.scenario('verified-fixed-conflict')

    def test_expired_success_demotes_real_egress_without_disconnect(self):
        self.scenario('verified-stale')

    def test_metric_limit_withdraws_only_the_owned_default_and_keeps_ip(self):
        self.scenario('verified-max')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--netns-case':
        worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
