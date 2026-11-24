"""Python SDK for qmi-supervisor plugins.

Pure standard library. A plugin calls connect(), subscribes to status or
events and consumes messages; the SDK answers bus pings and reconnects with
backoff, restoring its subscriptions.
"""
from collections import deque
import json
import os
import socket
import sys
import time

PROTOCOL = 1
MAX_MESSAGE = 64 * 1024
BACKOFF_START = 1
BACKOFF_MAX = 60

__all__ = ['Bus', 'PluginError', 'connect']


class PluginError(Exception):
    """A coded plugin error: unauthorized, unavailable, protocol or capability."""

    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _settings_path():
    return os.environ.get('QMI_SUPERVISOR_PLUGIN_SETTINGS', '')


def _read_settings(path):
    if not path:
        return {}
    try:
        with open(path, encoding='utf-8') as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class Bus:
    def __init__(self, sock, path, name, capabilities, settings, *, timeout=10.0):
        self._sock = sock
        self.path = path
        self.name = name
        self.capabilities = tuple(capabilities)
        self._settings = dict(settings)
        self._timeout = timeout
        self._topics = ()
        self._interval = 30
        self._buffer = b''
        self._pending = deque()
        self._next_id = 1

    @classmethod
    def connect(cls, timeout=10.0):
        path = os.environ.get('QMI_SUPERVISOR_BUS', '')
        name = os.environ.get('QMI_SUPERVISOR_PLUGIN', '')
        secret = os.environ.get('QMI_SUPERVISOR_PLUGIN_SECRET', '')
        if not path or not name or not secret:
            raise PluginError('unavailable', 'the plugin environment is incomplete')
        sock = cls._open(path, timeout)
        bus = cls(sock, path, name, (), _read_settings(_settings_path()), timeout=timeout)
        try:
            welcome = bus._handshake(secret)
        except Exception:
            sock.close()
            raise
        bus.capabilities = tuple(str(item) for item in welcome.get('capabilities') or ())
        return bus

    @staticmethod
    def _open(path, timeout):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except OSError as error:
            sock.close()
            raise PluginError('unavailable', str(error)) from None
        return sock

    def _send(self, value):
        body = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        if len(body) > MAX_MESSAGE:
            raise PluginError('protocol', 'message too large')
        self._sock.sendall(body)

    def _receive(self):
        while b'\n' not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise OSError('bus closed the connection')
            self._buffer += chunk
            if len(self._buffer) > MAX_MESSAGE:
                raise PluginError('protocol', 'message too large')
        raw, self._buffer = self._buffer.split(b'\n', 1)
        try:
            return json.loads(raw)
        except ValueError:
            raise PluginError('protocol', 'invalid JSON from the bus') from None

    def _handshake(self, secret):
        self._send({'op': 'hello', 'protocol': PROTOCOL, 'plugin': self.name, 'token': secret})
        message = self._receive()
        if message.get('op') == 'welcome':
            return message
        if message.get('op') == 'error' and message.get('code') == 'unauthorized':
            raise PluginError('unauthorized')
        raise PluginError('protocol', 'unexpected welcome')

    def settings(self):
        return dict(self._settings)

    def subscribe(self, topics=('events',), interval=5):
        topics = tuple(topics)
        if any(topic not in {'status', 'events'} for topic in topics):
            raise PluginError('protocol', 'unknown topic')
        if type(interval) is not int or not 5 <= interval <= 300:
            raise PluginError('protocol', 'interval must be 5..300 seconds')
        self._topics, self._interval = topics, interval
        self._send({'op': 'subscribe', 'topics': list(topics), 'interval': interval})

    def log(self, text):
        print(str(text), file=sys.stderr, flush=True)

    def _id(self):
        identifier = str(self._next_id)
        self._next_id += 1
        return identifier

    def request(self, action, modem=None, timeout=10.0, **kwargs):
        if not any(capability.startswith('act:') for capability in self.capabilities):
            raise PluginError('capability', 'actions are not available to this plugin')
        identifier = self._id()
        message = {'op': 'request', 'id': identifier, 'action': action, 'modem': modem}
        message.update(kwargs)
        self._send(message)
        deadline = time.monotonic() + timeout
        self._sock.settimeout(max(0.1, timeout))
        try:
            while time.monotonic() < deadline:
                reply = self._receive()
                if reply.get('op') == 'ping':
                    self._send({'op': 'pong'})
                    continue
                if reply.get('op') == 'result' and reply.get('id') == identifier:
                    return {'ok': bool(reply.get('ok')), 'error': reply.get('error')}
                self._pending.append(reply)
            raise PluginError('unavailable', 'the bus did not answer the request')
        finally:
            self._sock.settimeout(self._timeout)

    def card(self, card_id, *, modem=None, title='', rows=(), link=None, ttl=300):
        self._send({'op': 'card', 'id': str(card_id), 'modem': modem, 'title': title,
                    'rows': list(rows), 'link': link, 'ttl': ttl})

    def issue(self, issue_id, *, modem=None, title='', text='', tone='warning',
              action=None, active=True):
        self._send({'op': 'issue', 'id': str(issue_id), 'modem': modem, 'title': title,
                    'text': text, 'tone': tone, 'action': action, 'active': active})

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def _reconnect(self):
        delay = BACKOFF_START
        while True:
            time.sleep(delay)
            try:
                sock = self._open(self.path, self._timeout)
                probe = Bus(sock, self.path, self.name, (), {}, timeout=self._timeout)
                secret = os.environ.get('QMI_SUPERVISOR_PLUGIN_SECRET', '')
                welcome = probe._handshake(secret)
                self._sock, self._buffer = sock, b''
                self.capabilities = tuple(str(item) for item in welcome.get('capabilities') or ())
                if self._topics:
                    self._send({'op': 'subscribe', 'topics': list(self._topics),
                                'interval': self._interval})
                return
            except (OSError, PluginError):
                try:
                    sock.close()
                except (OSError, UnboundLocalError):
                    pass
                delay = min(delay * 2, BACKOFF_MAX)

    def messages(self):
        """Yield bus messages; pings are answered here and never yielded."""
        while True:
            if self._pending:
                yield self._pending.popleft()
                continue
            try:
                message = self._receive()
            except (OSError, PluginError) as error:
                if isinstance(error, PluginError) and error.code in {'protocol', 'unauthorized'}:
                    raise
                self._reconnect()
                yield {'op': 'reconnected'}
                continue
            operation = message.get('op')
            if operation == 'ping':
                try:
                    self._send({'op': 'pong'})
                except OSError:
                    self._reconnect()
                    yield {'op': 'reconnected'}
                continue
            if operation == 'pong':
                continue
            if operation == 'welcome':
                self.capabilities = tuple(str(item) for item in message.get('capabilities') or ())
                continue
            if operation in {'subscribed', 'ok'}:
                continue
            if operation == 'error':
                code = message.get('code')
                if code == 'unauthorized':
                    raise PluginError('unauthorized')
                if code in {'capability', 'protocol'}:
                    raise PluginError(code)
                yield message
                continue
            yield message


def connect(timeout=10.0):
    """Connect to the bus using the environment the plugin host prepared."""
    return Bus.connect(timeout)
