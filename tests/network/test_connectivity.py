"""Reachability must validate WWAN, never the machine's working other uplink."""
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from modules.network.connectivity import ConnectivityMonitor, parse_targets, probe_targets
from modules.runtime.settings import NetworkSettings


class ConnectivityTests(unittest.TestCase):
    def test_numeric_targets_allow_private_core_and_reject_unsafe_or_ambiguous_inputs(self):
        self.assertEqual(parse_targets(' 192.168.3.10:5201,1.1.1.1:443 '),
                         (('192.168.3.10', 5201), ('1.1.1.1', 443)))
        for value in ('', 'localhost:443', '127.0.0.1:80', '0.0.0.0:80', '0.1.2.3:80', '224.0.0.1:80',
                      '255.255.255.255:80', '1.1.1.1:0', '1.1.1.1:65536', '1.1.1.1:４４３',
                      '1.1.1.1:443,1.1.1.1:0443', '1.1.1.1', '[::1]:443',
                      ','.join(f'192.0.2.{i}:80' for i in range(1, 6))):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_targets(value)

    def monitor(self, **kwargs):
        settings = replace(NetworkSettings(), connectivity_successes=2, connectivity_failures=2)
        return ConnectivityMonitor(settings, 'wwan0', '192.168.3.2', 12, **kwargs)

    def test_hysteresis_requires_confirmations_and_records_own_attempt_times(self):
        now = [10]
        monitor = self.monitor(clock=lambda: now[0])
        self.assertIsNone(monitor.snapshot()['checked_monotonic'])
        def result(success):
            now[0] += 3
            monitor._record({'success': success, 'target': '1.1.1.1:443' if success else None,
                             'error': None if success else 'timeout', 'duration_ms': 45})
            return monitor.snapshot()
        self.assertEqual(result(True)['state'], 'checking')
        self.assertEqual(result(True)['state'], 'up')
        self.assertEqual(result(False)['state'], 'up')
        down = result(False)
        self.assertEqual(down['state'], 'down')
        self.assertEqual(down['last_success_monotonic'], 16)
        self.assertEqual(down['checked_monotonic'], 22)
        self.assertEqual(down['sequence'], 4)
        self.assertEqual(down['consecutive_failures'], 2)
        self.assertEqual(result(True)['state'], 'down')
        recovered = result(True)
        self.assertEqual(recovered['state'], 'up')
        self.assertEqual(recovered['consecutive_failures'], 0)
        recovered['state'] = 'down'
        self.assertEqual(monitor.snapshot()['state'], 'up', 'Snapshots must not mutate worker state')

    def test_slow_check_does_not_block_owner_and_close_cancels_without_publishing_late_success(self):
        entered, cancelled = threading.Event(), threading.Event()
        def slow_probe(*args):
            entered.set()
            args[-1].wait(5)
            cancelled.set()
            return {'success': True, 'target': '1.1.1.1:443'}
        monitor = self.monitor(probe=slow_probe)
        self.addCleanup(monitor.close)
        monitor.start()
        self.assertTrue(entered.wait(1))
        first_thread = monitor.thread
        monitor.start()
        self.assertIs(monitor.thread, first_thread)
        started = time.monotonic()
        for _ in range(1000):
            self.assertEqual(monitor.snapshot()['state'], 'checking')
        self.assertLess(time.monotonic() - started, .1)
        monitor.close()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(monitor.thread.is_alive())
        self.assertEqual(monitor.snapshot()['sequence'], 0)

    def test_unexpected_probe_failure_becomes_failed_observation_and_keeps_worker_alive(self):
        happened = threading.Event()
        def failed(*args):
            happened.set()
            raise OSError('permission denied')
        monitor = self.monitor(probe=failed)
        self.addCleanup(monitor.close)
        monitor.start()
        self.assertTrue(happened.wait(1))
        deadline = time.monotonic() + 1
        while monitor.snapshot()['sequence'] == 0 and time.monotonic() < deadline:
            time.sleep(.005)
        snapshot = monitor.snapshot()
        self.assertEqual(snapshot['consecutive_failures'], 1)
        self.assertIn('permission denied', snapshot['error'])
        self.assertTrue(monitor.thread.is_alive())

    def test_failed_device_binding_never_retries_without_binding_or_uses_dns(self):
        connection = Mock()
        connection.setsockopt.side_effect = PermissionError(errno.EPERM, 'Permission denied')
        with patch('modules.network.connectivity.socket.if_nametoindex', return_value=12), \
                patch('modules.network.connectivity.socket.socket', return_value=connection) as factory, \
                patch('modules.network.connectivity.socket.getaddrinfo') as dns:
            result = probe_targets('wwan0', '192.168.3.2', 12, (('1.1.1.1', 443),),
                                   .1, threading.Event())
        self.assertFalse(result['success'])
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        connection.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'wwan0\0')
        connection.bind.assert_not_called()
        connection.connect_ex.assert_not_called()
        dns.assert_not_called()
        connection.close.assert_called()

    def test_reused_interface_name_cannot_validate_old_generation(self):
        with patch('modules.network.connectivity.socket.if_nametoindex', return_value=13), \
                patch('modules.network.connectivity.socket.socket') as factory:
            result = probe_targets('wwan0', '192.168.3.2', 12, (('1.1.1.1', 443),),
                                   .1, threading.Event())
        self.assertFalse(result['success'])
        self.assertIn('identity changed', result['error'])
        factory.assert_not_called()

    def test_service_on_modem_own_address_does_not_validate_upstream(self):
        with patch('modules.network.connectivity.socket.if_nametoindex', return_value=12), \
                patch('modules.network.connectivity.socket.socket') as factory:
            result = probe_targets('wwan0', '192.168.3.2', 12, (('192.168.3.2', 8080),),
                                   .1, threading.Event())
        self.assertFalse(result['success'])
        self.assertIn('configured modem address', result['error'])
        factory.assert_not_called()

    def test_interface_replaced_during_immediate_connect_cannot_publish_success(self):
        connection = Mock()
        connection.connect_ex.return_value = 0
        with patch('modules.network.connectivity.socket.if_nametoindex', side_effect=[12, 13]), \
                patch('modules.network.connectivity.socket.socket', return_value=connection):
            result = probe_targets('wwan0', '192.168.3.2', 12, (('1.1.1.1', 443),),
                                   .1, threading.Event())
        self.assertFalse(result['success'])
        connection.bind.assert_called_once_with(('192.168.3.2', 0))
        self.assertIn('identity changed', result['error'])

    def test_actual_tcp_success_on_bound_loopback_and_failure_on_closed_service(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.2', 0))
        listener.listen(4)
        index = socket.if_nametoindex('lo')
        try:
            result = probe_targets('lo', '127.0.0.1', index, (listener.getsockname(),),
                                   .5, threading.Event())
        except PermissionError as error:
            self.skipTest(str(error))
        if not result['success'] and 'Permission denied' in str(result['error']):
            self.skipTest('Device binding not permitted')
        self.assertTrue(result['success'], result)
        peer, _ = listener.accept()
        peer.close()
        destination = listener.getsockname()
        listener.close()
        result = probe_targets('lo', '127.0.0.1', index, (destination,), .5, threading.Event())
        self.assertFalse(result['success'])
        self.assertIn('refused', result['error'].lower())


def isolated_probe(parent_namespace):
    assert os.readlink('/proc/self/ns/net') != parent_namespace, 'A new network namespace is mandatory'
    def ip(*args):
        return subprocess.run(['ip', *args], check=True, text=True, capture_output=True).stdout
    assert json.loads(ip('-j', '-4', 'route', 'show', 'table', 'main')) == []
    for name, address in (('probe-eth', '198.18.0.2/30'), ('probe-ww', '198.18.0.6/30')):
        ip('link', 'add', name, 'type', 'dummy')
        ip('addr', 'add', address, 'dev', name)
        ip('link', 'set', name, 'up')
    ip('link', 'set', 'lo', 'up')
    ip('route', 'add', 'default', 'via', '198.18.0.1', 'dev', 'probe-eth', 'metric', '100')
    ip('route', 'add', 'default', 'via', '198.18.0.5', 'dev', 'probe-ww', 'metric', '700')
    with socket.socket() as listener:
        listener.bind(('198.18.0.2', 0))
        listener.listen(4)
        target = listener.getsockname()
        with socket.create_connection(target, timeout=.5):
            peer, _ = listener.accept()
            peer.close()
        started = time.monotonic()
        result = probe_targets('probe-ww', '198.18.0.6', socket.if_nametoindex('probe-ww'),
                               (target, ('203.0.113.99', 443)), .2, threading.Event())
        elapsed = time.monotonic() - started
        assert result['success'] is False, result
        assert elapsed < .5, f'Parallel targets exceeded one round timeout: {elapsed}'
    print(json.dumps({'ethernet_service_reachable': True, 'wwan_probe_succeeded': False,
                      'parallel_duration_seconds': elapsed}))


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('unshare') and shutil.which('ip'),
                     'Requires Linux unshare and iproute2')
class IsolatedProbeTests(unittest.TestCase):
    def test_working_other_interface_cannot_validate_cellular_and_targets_share_deadline(self):
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        result = subprocess.run(['unshare', '--user', '--map-root-user', '--net', sys.executable,
                                 str(Path(__file__).resolve()), '--isolated', os.readlink('/proc/self/ns/net')],
                                text=True, capture_output=True, timeout=10, env=environment)
        if result.returncode and 'unshare failed: Operation not permitted' in result.stderr:
            self.skipTest(result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['wwan_probe_succeeded'])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--isolated':
        isolated_probe(sys.argv[2])
    else:
        unittest.main()
