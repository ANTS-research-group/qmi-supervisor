"""Optional real TCP path test; all mutations require two isolated net namespaces.

WWAN reaches a remote /32 through a gateway while the preferred Ethernet default
cannot reach it. Strict reverse-path filtering can reject asymmetric WWAN
replies. Its initial/strict/loose observations are recorded; any sysctl writes
affect only the disposable outer network namespace, never the host.
"""
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import subprocess
import sys
import threading
import unittest

from modules.network.connectivity import probe_targets


class NamespaceUnavailable(Exception):
    pass


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=3)
    if result.returncode:
        if any(message in result.stderr for message in ('Operation not permitted', 'Permission denied',
                                                       'Operation not supported')):
            raise NamespaceUnavailable(result.stderr.strip())
        raise RuntimeError(f'{args!r}: {result.stderr}')
    return result.stdout


def receive(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(3):
            raise RuntimeError('Isolated peer did not reply within three seconds')
    line = process.stdout.readline()
    if not line:
        process.wait(timeout=1)
        error = process.stderr.read()
        if 'unshare failed: Operation not permitted' in error:
            raise NamespaceUnavailable(error.strip())
        raise RuntimeError('Isolated peer exited: ' + error)
    return json.loads(line)


def send(process, message):
    process.stdin.write(message + '\n')
    process.stdin.flush()


def emit(value):
    print(json.dumps(value), flush=True)


def peer_server(outer_namespace):
    namespace = os.readlink('/proc/self/ns/net')
    assert namespace != outer_namespace, 'Peer must have a separate network namespace'
    assert json.loads(command('ip', '-j', '-4', 'route', 'show', 'table', 'main')) == []
    emit({'pid': os.getpid(), 'namespace': namespace})
    assert sys.stdin.readline().strip() == 'serve'
    sources = []
    with socket.socket() as listener, selectors.DefaultSelector() as selector:
        listener.bind(('203.0.113.9', 0))
        listener.listen(8)
        selector.register(listener, selectors.EVENT_READ, 'tcp')
        selector.register(sys.stdin, selectors.EVENT_READ, 'control')
        emit({'target': listener.getsockname()})
        while True:
            for key, _ in selector.select(5):
                if key.data == 'tcp':
                    connection, address = listener.accept()
                    sources.append(address[0])
                    connection.close()
                else:
                    request = sys.stdin.readline().strip()
                    if request == 'report':
                        # Drain any successful handshake whose accept event raced
                        # the control pipe, so its source is part of the evidence.
                        listener.setblocking(False)
                        try:
                            while True:
                                connection, address = listener.accept()
                                sources.append(address[0])
                                connection.close()
                        except BlockingIOError:
                            pass
                        finally:
                            listener.setblocking(True)
                        emit({'accepted_sources': sources})
                    elif request in ('quit', ''):
                        return


def isolated_path(host_namespace):
    namespace = os.readlink('/proc/self/ns/net')
    assert namespace != host_namespace, 'A fresh outer network namespace is mandatory'
    assert json.loads(command('ip', '-j', '-4', 'route', 'show', 'table', 'main')) == []
    command('ip', 'link', 'set', 'lo', 'up')
    command('ip', 'link', 'add', 'probe-eth', 'type', 'dummy')
    command('ip', 'addr', 'add', '198.18.0.2/30', 'dev', 'probe-eth')
    command('ip', 'link', 'set', 'probe-eth', 'up')
    command('ip', 'route', 'add', 'default', 'via', '198.18.0.1', 'dev', 'probe-eth', 'metric', '100')

    process = subprocess.Popen(['unshare', '--net', sys.executable, str(Path(__file__).resolve()),
                                '--peer', namespace], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        peer = receive(process)
        assert peer['namespace'] not in (namespace, host_namespace)
        assert os.readlink(f"/proc/{peer['pid']}/ns/net") == peer['namespace']
        command('ip', 'link', 'add', 'probe-ww', 'type', 'veth', 'peer', 'name', 'probe-peer')
        command('ip', 'link', 'set', 'probe-peer', 'netns', str(peer['pid']))
        command('ip', 'addr', 'add', '198.18.1.2/30', 'dev', 'probe-ww')
        command('ip', 'link', 'set', 'probe-ww', 'up')
        command('ip', 'route', 'add', 'default', 'via', '198.18.1.1', 'dev', 'probe-ww', 'metric', '700')
        for args in (('link', 'set', 'lo', 'up'), ('addr', 'add', '203.0.113.9/32', 'dev', 'lo'),
                     ('addr', 'add', '198.18.1.1/30', 'dev', 'probe-peer'), ('link', 'set', 'probe-peer', 'up')):
            command('nsenter', '--target', str(peer['pid']), '--net', '--', 'ip', *args)
        send(process, 'serve')
        target = tuple(receive(process)['target'])
        unbound_route = json.loads(command('ip', '-j', '-4', 'route', 'get', target[0]))[0]
        bound_route = json.loads(command('ip', '-j', '-4', 'route', 'get', target[0],
                                          'from', '198.18.1.2', 'oif', 'probe-ww'))[0]
        assert unbound_route['dev'] == 'probe-eth', unbound_route
        assert bound_route['dev'] == 'probe-ww' and bound_route['gateway'] == '198.18.1.1', bound_route
        unbound_success = False
        try:
            with socket.create_connection(target, timeout=.2):
                unbound_success = True
        except OSError:
            pass
        assert not unbound_success, 'An unbound connection must follow the unreachable Ethernet default'

        def probe():
            return probe_targets('probe-ww', '198.18.1.2', socket.if_nametoindex('probe-ww'),
                                 (target,), .5, threading.Event())

        paths = {name: Path('/proc/sys/net/ipv4/conf') / name / 'rp_filter'
                 for name in ('all', 'probe-ww')}
        initial = {name: int(path.read_text()) for name, path in paths.items()}
        initial_result, strict_result, strict_error = probe(), None, None
        try:
            try:
                for path in paths.values():
                    path.write_text('1\n')
                strict_result = probe()
            except OSError as error:
                strict_error = str(error)
            try:
                paths['all'].write_text('0\n')
                paths['probe-ww'].write_text('2\n')
                positive_result = probe()
            except OSError as error:
                if not initial_result['success']:
                    raise NamespaceUnavailable('Cannot test the WWAN path with loose rp_filter inside the '
                                               f'isolated namespace; initial={initial}, error={error}') from error
                # The original namespace settings already admitted the real
                # WWAN connection; inability to vary sysctls is not a failure.
                positive_result = initial_result
            assert positive_result['success'], positive_result
        finally:
            for name, path in paths.items():
                try:
                    path.write_text(str(initial[name]) + '\n')
                except OSError:
                    pass  # Namespace is destroyed next; no host setting was changed.
        send(process, 'report')
        observed = receive(process)
        assert observed['accepted_sources'] and set(observed['accepted_sources']) == {'198.18.1.2'}, observed
        emit({'isolated': True, 'remote_target': f'{target[0]}:{target[1]}',
              'unbound_success': unbound_success, 'unbound_interface': unbound_route['dev'],
              'bound_gateway': bound_route['gateway'], 'bound_result': positive_result,
              'accepted_sources': observed['accepted_sources'], 'initial_rp_filter': initial,
              'initial_bound_result': initial_result, 'strict_bound_result': strict_result,
              'strict_configuration_error': strict_error})
    finally:
        if process.poll() is None:
            try:
                send(process, 'quit')
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        process.communicate(timeout=2)


@unittest.skipUnless(sys.platform.startswith('linux') and all(shutil.which(name) for name in ('ip', 'unshare', 'nsenter')),
                     'Requires Linux, iproute2, unshare and nsenter; never runs against the host network')
class IsolatedWWANPathTests(unittest.TestCase):
    def test_bound_probe_uses_remote_wwan_gateway_behind_preferred_ethernet(self):
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        result = subprocess.run(['unshare', '--user', '--map-root-user', '--net', sys.executable,
                                 str(Path(__file__).resolve()), '--isolated', os.readlink('/proc/self/ns/net')],
                                capture_output=True, text=True, timeout=20, env=environment)
        if result.returncode and 'unshare failed: Operation not permitted' in result.stderr:
            self.skipTest(result.stderr.strip())
        if result.returncode == 77:
            self.skipTest(json.loads(result.stdout)['skip'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout)
        self.assertTrue(observed['isolated'])
        self.assertFalse(observed['unbound_success'])
        self.assertTrue(observed['bound_result']['success'])
        self.assertEqual(observed['unbound_interface'], 'probe-eth')
        self.assertEqual(observed['bound_gateway'], '198.18.1.1')
        self.assertEqual(set(observed['accepted_sources']), {'198.18.1.2'})
        print('Isolated WWAN TCP path: ' + json.dumps(observed, sort_keys=True))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--peer':
        peer_server(sys.argv[2])
    elif len(sys.argv) == 3 and sys.argv[1] == '--isolated':
        try:
            isolated_path(sys.argv[2])
        except NamespaceUnavailable as error:
            emit({'skip': str(error)})
            raise SystemExit(77)
    else:
        unittest.main()
