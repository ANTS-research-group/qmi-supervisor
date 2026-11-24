"""Local bus for supervised plugins: newline-delimited JSON on a UNIX socket.

The bus only reads published contract objects (per-profile status files and
events.jsonl) and never talks to the modem actors. Each client has a bounded
outgoing queue; slow plugins lose old messages and are told how many.
"""
from collections import deque
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets as random_secrets
import socket
import struct
import threading
import time

from modules.runtime.settings import load_settings

PROTOCOL = 1
MAX_MESSAGE = 64 * 1024
QUEUE_SIZE = 100
PING_SECONDS = 20
UNHEALTHY_SECONDS = 60
SOCKET_DIR = '/run/qmi-supervisor-ext'
SOCKET_NAME = 'bus.sock'
SECRETS_PATH = '/run/qmi-supervisor/bus-secrets.json'
TOPIC_CAPABILITY = {'status': 'read:status', 'events': 'read:events'}
ACTIONS = {'objective': 'act:objective', 'reconnect': 'act:reconnect'}
TONES = {'info', 'warning', 'bad'}
CARD_TONES = {'neutral', 'good', 'warning', 'bad'}
ACTION_LIMIT = 6
ACTION_WINDOW = 60
RECONNECT_INTERVAL = 300
MAX_ISSUES = 5
MAX_CARDS = 20
MAX_ROWS = 12
LOG = logging.getLogger(__name__)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_secrets(path=SECRETS_PATH):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get('version') != 1:
        return {}
    plugins = value.get('plugins')
    return plugins if isinstance(plugins, dict) else {}


def write_secrets(plugins, path=SECRETS_PATH):
    """Atomically publish the per-plugin secrets and effective capabilities."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps({'version': 1, 'plugins': plugins}, sort_keys=True),
                         encoding='utf-8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def serialize(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()


class Client:
    def __init__(self, connection, address):
        self.connection, self.address = connection, address
        self.name = None
        self.capabilities = ()
        self.topics = set()
        self.interval = 30
        self.queue = deque(maxlen=QUEUE_SIZE)
        self.dropped = 0
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        self.closed = False
        self.connected = False
        self.last_activity = time.monotonic()
        self.last_status = 0.0
        self.drops_total = 0

    def send(self, value):
        body = serialize(value)
        if len(body) > MAX_MESSAGE:
            return False
        with self.lock:
            if self.closed:
                return False
            while len(self.queue) + 1 + (1 if self.dropped else 0) > self.queue.maxlen:
                if not self.queue:
                    break
                self.queue.popleft()
                self.dropped += 1
                self.drops_total += 1
            if self.dropped:
                self.queue.append({'op': 'dropped', 'count': self.dropped})
                self.dropped = 0
            self.queue.append(value)
        self.wakeup.set()
        return True

    def send_now(self, value):
        """Fatal protocol reply: write it before closing, never through the queue."""
        try:
            self.connection.sendall(serialize(value))
            return True
        except OSError:
            return False

    def close(self):
        with self.lock:
            self.closed = True
        self.wakeup.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass


class BusServer:
    def __init__(self, config_path, runtime, settings, stop, *,
                 secrets_path=SECRETS_PATH, socket_path=None, clock=time.monotonic):
        self.config_path = Path(config_path)
        self.runtime = Path(runtime)
        self.settings = settings
        self.stop = stop
        self.secrets_path = Path(secrets_path)
        self.socket_path = Path(socket_path or (Path(SOCKET_DIR)/SOCKET_NAME))
        self.clock = clock
        self.clients = []
        self.clients_lock = threading.Lock()
        self.listener = None
        self.threads = []
        self.tail = {}
        self._status_fingerprint = None
        self._next_ping = 0.0
        self.manager_socket = self.runtime/'manager.sock'
        self.actions = {}
        self.reconnects = {}
        # Issues and cards belong to a plugin's live connection; reader threads and the
        # worker loop share them under this lock.
        self.issues = {}
        self.cards = {}
        self.extensions_lock = threading.RLock()
        self.extensions = {'issues': [], 'cards': []}

    # -- socket ---------------------------------------------------------
    def _prepare_socket(self):
        directory = self.socket_path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        try:
            import grp
            group = grp.getgrnam('qmi-supervisor-ext').gr_gid
            os.chown(directory, 0, group)
        except (KeyError, OSError, ImportError):
            pass
        os.chmod(directory, 0o750)
        if self.socket_path.exists():
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        try:
            import grp
            group = grp.getgrnam('qmi-supervisor-ext').gr_gid
            os.chown(self.socket_path, 0, group)
        except (KeyError, OSError, ImportError):
            pass
        os.chmod(self.socket_path, 0o660)
        listener.listen(16)
        listener.settimeout(1.0)
        self.listener = listener

    def serve(self):
        self._prepare_socket()
        # A restarted bus starts clean: issues and cards of the previous run are gone.
        self._publish_extensions()
        while not self.stop.is_set():
            try:
                connection, address = self.listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            connection.settimeout(1.0)
            client = Client(connection, address)
            with self.clients_lock:
                self.clients.append(client)
            reader = threading.Thread(target=self._read_client, args=(client,), daemon=True,
                                      name='bus-client')
            writer = threading.Thread(target=self._write_client, args=(client,), daemon=True,
                                      name='bus-writer')
            reader.start()
            writer.start()
            self.threads.extend((reader, writer))
        self.close()

    def _read_client(self, client):
        buffer = b''
        try:
            while not self.stop.is_set() and not client.closed:
                try:
                    chunk = client.connection.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_MESSAGE:
                    client.send_now({'op': 'error', 'code': 'message-too-large'})
                    break
                while b'\n' in buffer:
                    raw, buffer = buffer.split(b'\n', 1)
                    if not raw:
                        continue
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        client.send({'op': 'error', 'code': 'invalid-json'})
                        continue
                    if not self._handle(client, message):
                        return
        finally:
            client.close()
            with self.clients_lock:
                if client in self.clients:
                    self.clients.remove(client)
                remaining = any(other.name == client.name and not other.closed
                                for other in self.clients)
            if client.name and not remaining:
                self._forget_plugin(client.name)

    def _forget_plugin(self, name):
        """A plugin that left takes its issues and cards with it; it sends them again when it reconnects."""
        with self.extensions_lock:
            known = name in self.issues or name in self.cards
            self.issues.pop(name, None)
            self.cards.pop(name, None)
            if known:
                self._publish_extensions()

    def _write_client(self, client):
        while not client.closed and not self.stop.is_set():
            client.wakeup.wait(0.5)
            client.wakeup.clear()
            while True:
                with client.lock:
                    if not client.queue or client.closed:
                        break
                    value = client.queue.popleft()
                try:
                    client.connection.sendall(serialize(value))
                except OSError:
                    client.close()
                    return

    def _handle(self, client, message):
        if not isinstance(message, dict):
            client.send({'op': 'error', 'code': 'invalid-message'})
            return False
        operation = message.get('op')
        if operation == 'hello':
            plugins = read_secrets(self.secrets_path)
            entry = plugins.get(message.get('plugin')) if isinstance(message.get('plugin'), str) else None
            token = message.get('token')
            if (message.get('protocol') != PROTOCOL or not isinstance(entry, dict)
                    or not isinstance(token, str)
                    or not hmac.compare_digest(str(entry.get('secret', '')).encode(),
                                               token.encode())):
                client.send_now({'op': 'error', 'code': 'unauthorized'})
                return False
            client.name = message['plugin']
            client.capabilities = tuple(str(item) for item in entry.get('capabilities') or ()
                                        if isinstance(item, str))
            client.connected = True
            client.last_activity = self.clock()
            client.send({'op': 'welcome', 'capabilities': list(client.capabilities),
                         'contract_version': 1})
            return True
        if not client.connected:
            client.send({'op': 'error', 'code': 'unauthorized'})
            return False
        client.last_activity = self.clock()
        if operation == 'ping':
            client.send({'op': 'pong'})
            return True
        if operation == 'pong':
            return True
        if operation == 'subscribe':
            topics = message.get('topics')
            interval = message.get('interval', 30)
            if (not isinstance(topics, list) or not topics
                    or any(topic not in TOPIC_CAPABILITY for topic in topics)
                    or type(interval) is not int or not 5 <= interval <= 300):
                client.send({'op': 'error', 'code': 'protocol'})
                return True
            missing = [topic for topic in topics
                       if TOPIC_CAPABILITY[topic] not in client.capabilities]
            if missing:
                client.send({'op': 'error', 'code': 'capability'})
                return True
            client.topics = set(topics)
            client.interval = interval
            client.last_status = 0.0
            client.send({'op': 'subscribed', 'topics': sorted(client.topics),
                         'interval': interval})
            return True
        if operation in {'request', 'issue', 'card'}:
            self._extension(client, message)
            return True
        client.send({'op': 'error', 'code': 'protocol'})
        return True

    @staticmethod
    def _text(value, limit):
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError('text')
        return ''.join(character for character in value if character >= ' ' or character == '\n').strip()

    def _audit(self, plugin, action, modem, result):
        try:
            from modules.runtime.ipc import request as ipc_request
            ipc_request(self.manager_socket,
                        {'action': 'audit', 'plugin': plugin, 'name': action,
                         'modem': modem, 'result': result}, 2)
        except Exception:
            pass
        LOG.info('Plugin %s requested %s for %s: %s', plugin, action, modem, result)

    def _action_allowed(self, client):
        now = self.clock()
        bucket = self.actions.setdefault(client.name, deque())
        while bucket and now - bucket[0] >= ACTION_WINDOW:
            bucket.popleft()
        if len(bucket) >= ACTION_LIMIT:
            return False
        bucket.append(now)
        return True

    def _extension(self, client, message):
        operation = message.get('op')
        if operation == 'request':
            action = message.get('action')
            modem = message.get('modem')
            capability = ACTIONS.get(action)
            if capability is None or capability not in client.capabilities:
                result = {'ok': False, 'error': 'refused'}
            elif not self._action_allowed(client):
                result = {'ok': False, 'error': 'rate_limited'}
            elif action == 'reconnect':
                now = self.clock()
                last = self.reconnects.get(modem, 0)
                if now - last < RECONNECT_INTERVAL:
                    result = {'ok': False, 'error': 'rate_limited'}
                else:
                    result = self._manager({'action': 'reconnect', 'modem': modem})
                    if result.get('ok'):
                        self.reconnects[modem] = now
            else:
                result = self._manager({'action': 'objective', 'modem': modem,
                                        'objective': message.get('objective'),
                                        'enabled': message.get('enabled')})
            reply = {'op': 'result', 'id': message.get('id'), 'ok': bool(result.get('ok')),
                     'error': result.get('error')}
            client.send(reply)
            self._audit(client.name, action, modem, reply['error'] or ('ok' if reply['ok'] else 'refused'))
            return
        if operation == 'issue':
            if 'ui:issues' not in client.capabilities:
                client.send({'op': 'error', 'code': 'capability'})
                return
            try:
                modem = message.get('modem') or None
                tone = message.get('tone')
                if tone not in TONES:
                    raise ValueError('tone')
                title = self._text(message.get('title'), 200)
                if not title:
                    raise ValueError('title')
                record = {'id': str(message.get('id') or '')[:64], 'plugin': client.name,
                          'modem': modem, 'tone': tone, 'title': title,
                          'text': self._text(message.get('text'), 200) or '',
                          'action': self._text(message.get('action'), 200),
                          'active': bool(message.get('active', True))}
            except ValueError:
                client.send({'op': 'error', 'code': 'protocol'})
                return
            with self.extensions_lock:
                active = [item for item in self.issues.get(client.name, {}).values() if item['active']]
                if record['active'] and len(active) >= MAX_ISSUES and record['id'] not in self.issues.get(client.name, {}):
                    client.send({'op': 'error', 'code': 'limit'})
                    return
                self.issues.setdefault(client.name, {})[record['id']] = record
                self._publish_extensions()
            client.send({'op': 'ok', 'id': record['id']})
            return
        if operation == 'card':
            if 'ui:cards' not in client.capabilities:
                client.send({'op': 'error', 'code': 'capability'})
                return
            try:
                title = self._text(message.get('title'), 200)
                if not title:
                    raise ValueError('title')
                rows = message.get('rows')
                if not isinstance(rows, list) or len(rows) > MAX_ROWS:
                    raise ValueError('rows')
                clean = []
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError('row')
                    label = self._text(row.get('label'), 200)
                    value = self._text(row.get('value'), 120)
                    tone = row.get('tone', 'neutral')
                    if not label or value is None or tone not in CARD_TONES:
                        raise ValueError('row')
                    clean.append({'label': label, 'value': value, 'tone': tone})
                link = message.get('link')
                if link is not None:
                    if (not isinstance(link, dict) or not str(link.get('href', '')).startswith(('http://', 'https://'))):
                        raise ValueError('link')
                    link = {'label': self._text(link.get('label'), 200) or '',
                            'href': self._text(link.get('href'), 500)}
                ttl = message.get('ttl', 300)
                if type(ttl) is not int or not 30 <= ttl <= 3600:
                    raise ValueError('ttl')
                record = {'id': str(message.get('id') or '')[:64], 'plugin': client.name,
                          'modem': message.get('modem') or None, 'title': title,
                          'rows': clean, 'link': link, 'ttl': ttl,
                          'expires': self.clock() + ttl}
            except ValueError:
                client.send({'op': 'error', 'code': 'protocol'})
                return
            with self.extensions_lock:
                cards = self.cards.setdefault(client.name, {})
                if len(cards) >= MAX_CARDS and record['id'] not in cards:
                    client.send({'op': 'error', 'code': 'limit'})
                    return
                cards[record['id']] = record
                self._publish_extensions()
            client.send({'op': 'ok', 'id': record['id']})
            return

    def _manager(self, payload):
        try:
            from modules.runtime.ipc import request as ipc_request
            value = ipc_request(self.manager_socket, payload, 35)
            return value if isinstance(value, dict) else {'ok': False, 'error': 'unavailable'}
        except Exception:
            return {'ok': False, 'error': 'unavailable'}

    def _publish_extensions(self):
        with self.extensions_lock:
            now = self.clock()
            issues, cards = [], []
            for plugin, records in self.issues.items():
                for record in records.values():
                    if record['active']:
                        issues.append(dict(record))
            for plugin, records in self.cards.items():
                for record in list(records.values()):
                    if record['expires'] <= now:
                        del records[record['id']]
                        continue
                    cards.append({key: record[key] for key in
                                  ('id', 'plugin', 'modem', 'title', 'rows', 'link', 'ttl')})
            self.extensions = {'issues': issues, 'cards': cards}
            path = self.runtime/'extensions.json'
            temporary = path.with_name('.' + path.name + '.tmp')
            try:
                temporary.write_text(json.dumps({'version': 1, 'updated_utc': _now_iso(),
                                                 **self.extensions}, sort_keys=True),
                                     encoding='utf-8')
                os.chmod(temporary, 0o600)
                temporary.replace(path)
            except OSError:
                pass

    def close(self):
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        with self.clients_lock:
            clients = list(self.clients)
            self.clients.clear()
        for client in clients:
            client.close()
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        # Without a bus no plugin is connected: nothing it said may stay on the page.
        with self.extensions_lock:
            self.issues.clear()
            self.cards.clear()
            self.extensions = {'issues': [], 'cards': []}
            try:
                (self.runtime/'extensions.json').unlink(missing_ok=True)
            except OSError:
                pass

    # -- published data -------------------------------------------------
    def _profile_directories(self):
        directories = {}
        try:
            base = load_settings(self.config_path)
        except (OSError, ValueError):
            base = self.settings
        directories['default'] = Path(base.telemetry.directory)
        try:
            from modules.runtime.fleet_config import Configuration
            configuration = Configuration.load(self.config_path)
        except (OSError, ValueError):
            return directories
        for identifier in configuration.profiles:
            try:
                directories[identifier] = Path(load_settings(self.config_path, profile=identifier)
                                               .telemetry.directory)
            except (OSError, ValueError):
                continue
        return directories

    def modems(self):
        records = []
        root = self.runtime/'modems'
        try:
            paths = sorted(root.glob('*/status.json'))
        except OSError:
            return records
        for path in paths[:32]:
            try:
                raw = path.read_bytes()
                if len(raw) > 512 * 1024:
                    continue
                value = json.loads(raw)
            except (OSError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            identifier = path.parent.name
            record = {key: value.get(key) for key in
                      ('state', 'supervisor_version', 'device', 'network', 'diagnosis',
                       'features', 'sim', 'registration', 'log_label')}
            record['id'] = identifier
            records.append(record)
        return records

    def _broadcast_status(self, force=False):
        records = self.modems()
        if not records:
            return
        fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True, default=str).encode()).hexdigest()
        changed = fingerprint != self._status_fingerprint
        self._status_fingerprint = fingerprint
        now = self.clock()
        for client in self._snapshot_clients():
            if 'status' not in client.topics:
                continue
            if changed or now - client.last_status >= client.interval:
                client.last_status = now
                client.send({'op': 'status', 'data': {'modems': records}})

    def _snapshot_clients(self):
        with self.clients_lock:
            return list(self.clients)

    def _tail_events(self):
        new = []
        for directory in set(self._profile_directories().values()):
            path = directory/'events.jsonl'
            try:
                metadata = path.stat()
            except OSError:
                continue
            key = str(path)
            state = self.tail.get(key)
            if state is None:
                self.tail[key] = (metadata.st_ino, metadata.st_size)
                continue
            offset = 0 if (state[0] != metadata.st_ino or metadata.st_size < state[1]) else state[1]
            if metadata.st_size == offset:
                self.tail[key] = (metadata.st_ino, offset)
                continue
            try:
                with path.open('rb') as stream:
                    stream.seek(offset)
                    content = stream.read(min(metadata.st_size - offset, 1024 * 1024))
            except OSError:
                continue
            self.tail[key] = (metadata.st_ino, offset + len(content))
            for raw in content.splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(event, dict) and type(event.get('seq')) is int
                        and isinstance(event.get('type'), str)):
                    new.append(event)
        return new

    def _broadcast_events(self, events):
        if not events:
            return
        for client in self._snapshot_clients():
            if 'events' in client.topics:
                for event in events:
                    client.send({'op': 'event', 'data': event})

    def _ping_clients(self, now):
        if now < self._next_ping:
            return
        self._next_ping = now + PING_SECONDS
        for client in self._snapshot_clients():
            if client.connected and now - client.last_activity > UNHEALTHY_SECONDS:
                client.send({'op': 'error', 'code': 'unhealthy'})
            else:
                client.send({'op': 'ping'})

    def poll(self):
        now = self.clock()
        with self.extensions_lock:
            expired = any(record['expires'] <= now for records in self.cards.values()
                          for record in records.values())
        if expired:
            self._publish_extensions()
        self._broadcast_status()
        self._broadcast_events(self._tail_events())
        self._ping_clients(now)

    def status(self):
        with self.clients_lock:
            clients = list(self.clients)
        return {'clients': [{'plugin': client.name, 'connected': client.connected,
                             'topics': sorted(client.topics), 'interval': client.interval,
                             'drops': client.drops_total,
                             'last_activity_utc': _now_iso() if client.last_activity else None}
                            for client in clients if client.connected]}


def new_secret():
    return random_secrets.token_urlsafe(32)


def bus_worker(config_path, runtime, worker_id, settings, stop):
    """Worker role: serve the plugin bus and publish its heartbeat."""
    from modules.runtime.workers import Heartbeat
    runtime = Path(runtime)
    heartbeat = Heartbeat(runtime/'bus.json', worker_id, settings)
    server = BusServer(config_path, runtime, settings, stop)
    thread = threading.Thread(target=server.serve, daemon=True, name='bus-server')
    thread.start()
    try:
        while not stop.is_set():
            server.poll()
            heartbeat.beat(phase='RUNNING', clients=server.status()['clients'])
            stop.wait(0.5)
    finally:
        server.close()
        heartbeat.beat(force=True, phase='STOPPED')
        heartbeat.close()
    return 0
