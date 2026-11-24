"""FakeBus for testing plugins without the supervisor."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading


class FakeBus:
    """A local bus for plugin tests; sets the environment connect() reads."""

    def __init__(self, capabilities=('read:status', 'read:events'), settings=None,
                 name='test-plugin', secret='test-secret'):
        self.capabilities = list(capabilities)
        self.settings = dict(settings or {})
        self.name, self.secret = name, secret
        self.requests, self.cards, self.issues = [], [], []
        self.subscriptions = []
        self._directory = tempfile.TemporaryDirectory()
        self._socket_path = str(Path(self._directory.name)/'bus.sock')
        self._settings_path = str(Path(self._directory.name)/'settings.json')
        self._connection = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._server = None
        self._thread = None
        self._saved_environment = {}
        self._response = {'ok': True, 'error': None}

    def __enter__(self):
        Path(self._settings_path).write_text(json.dumps(self.settings), encoding='utf-8')
        os.chmod(self._settings_path, 0o600)
        for key, value in {
            'QMI_SUPERVISOR_BUS': self._socket_path,
            'QMI_SUPERVISOR_PLUGIN': self.name,
            'QMI_SUPERVISOR_PLUGIN_SECRET': self.secret,
            'QMI_SUPERVISOR_PLUGIN_SETTINGS': self._settings_path,
        }.items():
            self._saved_environment[key] = os.environ.get(key)
            os.environ[key] = value
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self._socket_path)
        self._server.listen(1)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *unused):
        self._stop.set()
        try:
            if self._connection is not None:
                self._connection.close()
        except OSError:
            pass
        try:
            self._server.close()
        except (OSError, AttributeError):
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._directory.cleanup()
        for key, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _run(self):
        buffer = b''
        connection = None
        try:
            while not self._stop.is_set() and connection is None:
                try:
                    connection, _ = self._server.accept()
                except (socket.timeout, TimeoutError):
                    continue
            if connection is None:
                return
            connection.settimeout(0.2)
            with self._lock:
                self._connection = connection
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b'\n' in buffer:
                    raw, buffer = buffer.split(b'\n', 1)
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    self._handle(connection, message)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def _handle(self, connection, message):
        operation = message.get('op')
        if operation == 'hello':
            if message.get('token') != self.secret or message.get('plugin') != self.name:
                self._send(connection, {'op': 'error', 'code': 'unauthorized'})
                return
            self._send(connection, {'op': 'welcome', 'capabilities': self.capabilities,
                                    'contract_version': 1})
            self._ready.set()
        elif operation == 'subscribe':
            self.subscriptions.append(message)
            self._send(connection, {'op': 'subscribed', 'topics': message.get('topics') or [],
                                    'interval': message.get('interval', 30)})
        elif operation == 'ping':
            self._send(connection, {'op': 'pong'})
        elif operation == 'pong':
            return
        elif operation in {'request', 'card', 'issue'}:
            record = dict(message)
            if operation == 'request':
                self.requests.append(record)
                self._send(connection, {'op': 'result', 'id': record.get('id'),
                                        **self._response})
            elif operation == 'card':
                self.cards.append(record)
            else:
                self.issues.append(record)

    @staticmethod
    def _send(connection, value):
        try:
            connection.sendall(json.dumps(value).encode() + b'\n')
        except OSError:
            pass

    def ping(self):
        self._ready.wait(2)
        with self._lock:
            if self._connection is not None:
                self._send(self._connection, {'op': 'ping'})

    def push_status(self, modems):
        self._ready.wait(2)
        with self._lock:
            if self._connection is not None:
                self._send(self._connection, {'op': 'status', 'data': {'modems': modems}})

    def push_event(self, line):
        self._ready.wait(2)
        with self._lock:
            if self._connection is not None:
                self._send(self._connection, {'op': 'event', 'data': line})

    def respond(self, ok=True, error=None):
        self._response = {'ok': ok, 'error': error}
