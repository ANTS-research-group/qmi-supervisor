"""Bound, asynchronous TCP reachability checks, independent of QMI acquisition."""
import errno
import ipaddress
import math
import os
import selectors
import socket
import threading
import time


def parse_targets(value):
    """Use numeric IPv4 destinations: DNS must never select a different uplink."""
    if not isinstance(value, str):
        raise ValueError('network.connectivity_targets must be a comma-separated IPv4:port list')
    parts = value.split(',')
    if not 1 <= len(parts) <= 4:
        raise ValueError('network.connectivity_targets must contain between 1 and 4 IPv4:port destinations')
    targets = []
    for part in parts:
        host, separator, port = part.strip().rpartition(':')
        try:
            address = ipaddress.IPv4Address(host)
            if (not separator or not port.isascii() or not port.isdigit()
                    or not 1 <= int(port) <= 65535 or int(address) < (1 << 24)
                    or address.is_multicast or address.is_loopback
                    or address == ipaddress.IPv4Address('255.255.255.255')):
                raise ValueError()
        except (ipaddress.AddressValueError, ValueError):
            raise ValueError('network.connectivity_targets requires usable numeric IPv4:port destinations (ports 1–65535)') from None
        target = (str(address), int(port))
        if target in targets:
            raise ValueError('network.connectivity_targets cannot contain duplicate destinations')
        targets.append(target)
    return tuple(targets)


def probe_targets(interface, address, ifindex, targets, timeout, cancelled, *, clock=time.monotonic):
    """One parallel, bounded round. Never retry unbound if binding is denied.

    A backup default route remains available on this interface. Both the output
    device and the IPv4 source are fixed, so a working Ethernet route cannot
    turn a broken cellular link into a successful observation.
    """
    started = clock()
    errors = []
    sockets = []
    pending_codes = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR}
    selector = selectors.DefaultSelector()
    try:
        if cancelled.is_set():
            return None
        if socket.if_nametoindex(interface) != ifindex:
            raise OSError(errno.ENODEV, 'Interface identity changed')
        for host, port in targets:
            if cancelled.is_set():
                return None
            if host == address:
                errors.append(f'{host}:{port}: Probe target is the configured modem address')
                continue
            connection = None
            try:
                connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(connection)
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                      interface.encode('utf-8') + b'\0')
                connection.bind((address, 0))
                connection.setblocking(False)
                result = connection.connect_ex((host, port))
                if result == 0:
                    if socket.if_nametoindex(interface) != ifindex:
                        raise OSError(errno.ENODEV, 'Interface identity changed')
                    return {'success': True, 'target': f'{host}:{port}', 'error': None,
                            'duration_ms': max(0, (clock() - started) * 1000)}
                if result not in pending_codes:
                    raise OSError(result, os.strerror(result))
                selector.register(connection, selectors.EVENT_WRITE, f'{host}:{port}')
            except (OSError, ValueError) as error:
                errors.append(f'{host}:{port}: {error}')
                if connection is not None:
                    connection.close()
        deadline = started + timeout
        while selector.get_map() and not cancelled.is_set():
            remaining = deadline - clock()
            if remaining <= 0:
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                code = key.fileobj.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                selector.unregister(key.fileobj)
                if code == 0:
                    # A removal/name reuse during connect cannot validate a new modem.
                    if socket.if_nametoindex(interface) != ifindex:
                        raise OSError(errno.ENODEV, 'Interface identity changed')
                    return {'success': True, 'target': key.data, 'error': None,
                            'duration_ms': max(0, (clock() - started) * 1000)}
                errors.append(f'{key.data}: {os.strerror(code)}')
                key.fileobj.close()
        if cancelled.is_set():
            return None
        if selector.get_map():
            errors.append('TCP reachability check timed out')
    except (OSError, ValueError) as error:
        errors.append(str(error))
    finally:
        selector.close()
        for connection in sockets:
            connection.close()
    return {'success': False, 'target': None, 'error': '; '.join(errors)[:600] or 'No reachable TCP target',
            'duration_ms': max(0, (clock() - started) * 1000)}


class ConnectivityMonitor:
    """One cancellable worker; the owner alone mutates routes and checkpoints."""
    def __init__(self, settings, interface, address, ifindex, *, clock=time.monotonic,
                 probe=probe_targets):
        self.settings = settings
        self.interface, self.address, self.ifindex = interface, address, ifindex
        self.targets = parse_targets(settings.connectivity_targets)
        self.clock, self.probe = clock, probe
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.result = {'state': 'checking', 'checked_monotonic': None,
                       'last_success_monotonic': None, 'target': None, 'error': None,
                       'duration_ms': None, 'sequence': 0,
                       'consecutive_successes': 0, 'consecutive_failures': 0}

    def start(self):
        if self.thread is None and not self.cancelled.is_set():
            self.thread = threading.Thread(target=self._run, name='qmi-connectivity', daemon=True)
            self.thread.start()

    def close(self):
        self.cancelled.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.3)

    def snapshot(self):
        with self.lock:
            return dict(self.result)

    def _record(self, observation):
        now = self.clock()
        with self.lock:
            result = self.result
            success = observation['success']
            result['consecutive_successes'] = result['consecutive_successes'] + 1 if success else 0
            result['consecutive_failures'] = result['consecutive_failures'] + 1 if not success else 0
            if result['consecutive_successes'] >= self.settings.connectivity_successes:
                result['state'] = 'up'
            elif result['consecutive_failures'] >= self.settings.connectivity_failures:
                result['state'] = 'down'
            result.update(checked_monotonic=now, target=observation.get('target'),
                          error=observation.get('error'), duration_ms=observation.get('duration_ms'))
            if success:
                result['last_success_monotonic'] = now
            result['sequence'] += 1

    def _run(self):
        scheduled = self.clock()
        while not self.cancelled.is_set():
            try:
                observation = self.probe(self.interface, self.address, self.ifindex,
                    self.targets, self.settings.connectivity_timeout, self.cancelled)
            except Exception as error:
                # Unexpected probe errors fail closed; they must never stop acquisition.
                observation = {'success': False, 'target': None, 'error': str(error)[:600],
                               'duration_ms': None}
            if observation is None or self.cancelled.is_set():
                break
            self._record(observation)
            scheduled += self.settings.connectivity_interval
            now = self.clock()
            if now > scheduled:
                scheduled += (math.floor((now - scheduled) / self.settings.connectivity_interval) + 1) * self.settings.connectivity_interval
            self.cancelled.wait(max(0, scheduled - self.clock()))
