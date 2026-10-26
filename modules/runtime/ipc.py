"""Bounded, local process communication; this module never imports GI."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Queue, Empty, Full
import socket
import threading
import time

MAX_MESSAGE = 2 * 1024 * 1024


def settings_values(settings):
    """Canonicalise typed numbers so INI round trips do not change revisions."""
    value = asdict(settings)
    for name, options in vars(settings).items():
        for key, definition in options.__dataclass_fields__.items():
            if definition.type is float and type(value[name][key]) is int:
                value[name][key] = float(value[name][key])
    return value


def revision(settings, section=None):
    value = settings_values(settings)
    if section:
        value = {name: value[name] for name in section}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def read_json(path):
    try:
        with Path(path).open('rb') as source:
            raw = source.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


class LatestFile:
    """One replaceable snapshot; filesystem work never holds up an actor."""
    def __init__(self, path, *, transform=None):
        self.path = Path(path)
        self.condition = threading.Condition()
        self.latest = None
        self.closing = False
        self.transform = transform
        self.thread = threading.Thread(target=self._run, name='actor-snapshot', daemon=True)
        self.thread.start()

    def offer(self, value):
        with self.condition:
            self.latest = value
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.latest is not None or self.closing)
                value, self.latest = self.latest, None
                if value is None and self.closing:
                    return
            try:
                if self.transform is not None:
                    value = self.transform(value)
                raw = json.dumps(value, allow_nan=False, separators=(',', ':'))
                if len(raw.encode()) > MAX_MESSAGE:
                    raise ValueError('Actor snapshot exceeds the size limit')
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(raw)
                temporary.chmod(0o600)
                temporary.replace(self.path)
            except (OSError, ValueError, TypeError):
                # A stopped publisher becomes stale; it cannot fabricate progress.
                pass

    def close(self):
        with self.condition:
            self.closing = True
            self.condition.notify()
        self.thread.join(2)


class Heartbeat:
    def __init__(self, path, worker_id, settings):
        self.writer = LatestFile(path)
        self.worker_id, self.settings = worker_id, settings
        self.last = float('-inf')

    def beat(self, *, force=False, **values):
        now = time.monotonic()
        if not force and now - self.last < 1:
            return
        self.last = now
        self.writer.offer(dict(values, worker_id=self.worker_id,
            heartbeat_monotonic=now, heartbeat_at_utc=datetime.now(timezone.utc).isoformat(),
            config_revision=revision(self.settings)))

    def close(self):
        self.writer.close()


def receive(connection):
    data = bytearray()
    while len(data) <= MAX_MESSAGE:
        part = connection.recv(min(65536, MAX_MESSAGE + 1 - len(data)))
        if not part:
            break
        data.extend(part)
        if b'\n' in part:
            break
    if len(data) > MAX_MESSAGE:
        raise ValueError('IPC message exceeds the size limit')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('IPC message must be an object')
    return value


def request(path, value, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(json.dumps(value, allow_nan=False).encode() + b'\n')
        result = receive(connection)
    if result.get('error'):
        raise RuntimeError(result['error'])
    return result


class AcquisitionServer:
    """The socket thread queues work; only the modem actor executes QMI."""
    def __init__(self, path, stop):
        self.path, self.stop = Path(path), stop
        self.pending = Queue(maxsize=1)
        self.wakeup = lambda: None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.path.unlink(missing_ok=True)
        self.socket.bind(str(self.path))
        self.path.chmod(0o600)
        self.socket.listen(2)
        self.socket.settimeout(0.2)
        self.thread = threading.Thread(target=self._run, name='acquisition-ipc', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                try:
                    value = receive(connection)
                    if set(value) != {'method', 'deadline', 'run_id'} or value['method'] != 'acquire':
                        raise ValueError('Unsupported IPC request')
                    deadline = value['deadline']
                    if (type(deadline) not in (int, float)
                            or not time.monotonic() < deadline <= time.monotonic() + 60):
                        raise ValueError('Invalid acquisition deadline')
                    item = dict(value, queued=time.monotonic(), done=threading.Event())
                    self.pending.put_nowait(item)
                    self.wakeup()
                    item['done'].wait(max(0, deadline-time.monotonic()) + 0.1)
                    response = item.get('response', {'error': 'Acquisition deadline expired'})
                except (OSError, ValueError, Full) as error:
                    response = {'error': str(error) or 'Acquisition queue is full'}
                try:
                    raw = json.dumps(response, allow_nan=False).encode() + b'\n'
                    if len(raw) > MAX_MESSAGE:
                        raw = b'{"error":"Acquisition exceeds the IPC size limit"}\n'
                    connection.sendall(raw)
                except (OSError, ValueError, TypeError):
                    pass

    def take(self):
        try:
            item = self.pending.get_nowait()
            self.pending.task_done()
            return item
        except Empty:
            return None

    def close(self):
        self.socket.close()
        self.thread.join(2)
        self.path.unlink(missing_ok=True)
