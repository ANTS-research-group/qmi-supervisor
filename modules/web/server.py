"""Optional HTTP configuration UI. Reads snapshots; never queries QMI or runs commands."""
from configparser import ConfigParser
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookies import SimpleCookie, CookieError
import hmac
import ipaddress
import json
import logging
import math
import os
import re
from pathlib import Path
import socket
import selectors
from socketserver import ThreadingMixIn
import tempfile
import threading
import time
from urllib.parse import urlsplit, parse_qs

from modules.runtime.settings import load_settings, validate_settings
from modules.runtime.configuration import configuration_lock
from modules.runtime.ipc import revision
from modules.runtime.fleet_config import Configuration, valid_profile
from modules.runtime.fleet_status import modem_snapshot
from modules.web.bind import management_interface_address
from modules.web.auth import AuthManager, AuthError, hash_password

MAX_BODY = 64 * 1024
MAX_SNAPSHOT = 2 * 1024 * 1024
STATUS_PATH = "/run/qmi-supervisor/status.json"
SESSION_COOKIE = "qmi_session"
STATIC_ROUTES = ('', 'dashboard', 'modem', 'metrics/charts', 'metrics/data', 'metrics/signal', 'metrics/performance',
                 'configuration/modem', 'configuration/telemetry', 'configuration/web', 'profile')
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
                '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif',
                '.ico': 'image/x-icon', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf'}


def validate_static_export(root):
    """Validate a complete, symlink-free export without running Node or a build."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.absolute() != root.resolve():
        raise ValueError('Missing frontend export: run npm ci and npm run build in frontend/ on the build PC')
    for route in STATIC_ROUTES:
        target = root / route / 'index.html'
        relative = target.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('Frontend export must not contain symlinks')
        if not target.is_file():
            raise ValueError('Incomplete frontend export: run npm ci and npm run build in frontend/ on the build PC')
    # Reject symlinks anywhere in the shipped tree, including unused assets.
    if any(path.is_symlink() or not (path.is_dir() or path.is_file()) for path in root.rglob('*')):
        raise ValueError('Frontend export must contain only regular files and directories, without symlinks')
    return root.resolve()


def static_file(root, request_path):
    """Map only public export files. Never decode URL paths or follow symlinks."""
    try:
        parsed = urlsplit(request_path)
        path = parsed.path
        if (parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith('/')
                or '%' in path or '\\' in path or '\x00' in path or path.startswith('/api/')
                or any(part in ('.', '..') or part.startswith('.') for part in path.split('/') if part)):
            return None
        parts = path.strip('/').split('/') if path.strip('/') else []
        if '//' in path:
            return None
        target = root.joinpath(*parts)
        if not parts or path.endswith('/') or not target.suffix:
            target = target / 'index.html'
        relative = target.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        if target.suffix.lower() not in STATIC_TYPES or not target.is_file():
            return None
        target.resolve().relative_to(root)
        return target, STATIC_TYPES[target.suffix.lower()], (
            'public, max-age=31536000, immutable'
            # Next versions chunks by content hash and manifests by build ID.
            # Turbopack hashes are not limited to hexadecimal characters.
            if relative.parts[:2] == ('_next', 'static') and target.suffix not in ('.html', '.txt')
            else 'no-store')
    except (OSError, ValueError):
        return None


def snapshot_freshness(snapshot, boot_id, now=None):
    """Age observations on this boot, independent of browser and wall clocks.

    Never infer liveness from an HTTP response or the snapshot file's mtime:
    resources may continue to publish while the supervisor loop is blocked.
    """
    now = time.monotonic() if now is None else now
    same_boot = bool(boot_id and snapshot.get('boot_id') == boot_id)

    def age(value):
        if (not same_boot or type(value) not in (int, float)
                or value < 0 or value > now or not math.isfinite(value)):
            return None
        return (now-value)*1000

    heartbeat = age(snapshot.get('heartbeat_monotonic'))
    sample = snapshot.get('sample')
    timing = sample.get('timing') if isinstance(sample, dict) else None
    return {'heartbeat_age_ms': heartbeat,
            'snapshot_age_ms': age(snapshot.get('published_monotonic')),
            'sample_age_ms': age(timing.get('acquired_end_monotonic')) if isinstance(timing, dict) else None,
            'state_fresh': heartbeat <= 15000 if heartbeat is not None else None}


def _loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public_config(settings):
    values = asdict(settings)
    values.get("web", {}).pop("password_hash", None)
    return values


def _merge_settings(settings, changes):
    """Reject JSON coercions (notably bool-as-int) before dataclass validation."""
    if not isinstance(changes, dict):
        raise ValueError("config must be a JSON object")
    sections = {}
    for section, options in changes.items():
        if section not in settings.__dataclass_fields__ or not isinstance(options, dict):
            raise ValueError(f"Unknown or invalid configuration section: {section}")
        original = getattr(settings, section)
        values = {}
        for key, value in options.items():
            if section == "web" and key == "password_hash":
                raise ValueError("Use web.password to set a new password")
            if section == "web" and key == "password":
                if type(value) is not str:
                    raise ValueError("Invalid type for web.password")
                if value:
                    values["password_hash"] = hash_password(value)
                continue
            if key not in original.__dataclass_fields__:
                raise ValueError(f"Unknown option: {section}.{key}")
            expected = original.__dataclass_fields__[key].type
            if expected is float:
                if type(value) not in (int, float):
                    raise ValueError(f"{section}.{key} must be a number")
                value = float(value)
            elif type(value) is not expected:
                raise ValueError(f"Invalid type for {section}.{key}")
            values[key] = value
        sections[section] = replace(original, **values)
    return validate_settings(replace(settings, **sections))


def _write_config(path, settings):
    configuration = settings if isinstance(settings, Configuration) else (
        Configuration.load(path).update('default', settings) if path.exists() else Configuration(settings, {}))
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(configuration.render())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _strict_json(content):
    def pairs(items):
        values = {}
        for key, value in items:
            if key in values:
                raise ValueError("Duplicate JSON key: " + key)
            values[key] = value
        return values

    def constant(value):
        raise ValueError("Invalid JSON number: " + value)

    return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16
    allow_reuse_address = True

    def __init__(self, address, handler, max_workers):
        self.workers = threading.BoundedSemaphore(max_workers)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address):
        if not self.workers.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.workers.release()


class _Handler(BaseHTTPRequestHandler):
    server_version = "QMI-Supervisor"
    sys_version = ""

    def log_message(self, format, *args):
        # Avoid logging credentials or arbitrary request/query strings.
        return

    def _send(self, status, value, content_type="application/json; charset=utf-8", headers=None):
        body = (json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if content_type.startswith("application/json") and not isinstance(value, bytes) else value)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", (headers or {}).get('Cache-Control', 'no-store'))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for key, value in (headers or {}).items():
            if key.lower() != 'cache-control':
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, detail):
        code = {400: 'INVALID_CONFIGURATION', 401: 'AUTHENTICATION_REQUIRED', 403: 'FORBIDDEN',
                404: 'NOT_FOUND', 408: 'REQUEST_TIMEOUT', 412: 'CONFIGURATION_CONFLICT',
                429: 'RATE_LIMITED', 503: 'UNAVAILABLE'}.get(status, 'REQUEST_FAILED')
        if status == 401 and 'credentials' in detail.lower():
            code = 'INVALID_CREDENTIALS'
        match = re.search(r'\b(modem|network|recovery|telemetry|web)\.([a-z_]+)\b', detail)
        payload = {"error": detail, "code": code}
        if match:
            payload['field'] = match.group(0)
        self._send(status, payload)

    def _host_allowed(self):
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return False
        raw = hosts[0]
        try:
            parsed = urlsplit("//" + raw)
            if (parsed.username is not None or parsed.password is not None or parsed.path
                    or parsed.query or parsed.fragment or not parsed.hostname):
                return False
            if (parsed.port or 80) != self.server.server_port:
                return False
            host = parsed.hostname.lower()
            if host in self.server.allowed_hosts:
                return True
            # A wildcard bind accepts IP literals, never attacker-controlled DNS names.
            if self.server.wildcard:
                ipaddress.ip_address(host)
                return True
            return False
        except ValueError:
            return False

    def _session_id(self):
        values = self.headers.get_all("Cookie", [])
        if len(values) != 1 or len(values[0]) > 4096:
            return ""
        try:
            cookies = SimpleCookie()
            cookies.load(values[0])
            return cookies[SESSION_COOKIE].value if SESSION_COOKIE in cookies else ""
        except CookieError:
            return ""

    def _authentication(self, settings):
        self.server.auth.reconfigure(settings.web)
        session = self.server.auth.authenticate(self._session_id())
        return dict(session) if session else None

    def _origin_allowed(self):
        origin = self.headers.get_all("Origin", [])
        if origin and (len(origin) != 1 or origin[0] != "http://" + self.headers["Host"]):
            self._error(403, "Origin is not allowed")
            return False
        return True

    def _guard(self, write=False):
        if not self._host_allowed():
            self._error(403, "Host header is not allowed")
            return False
        try:
            with self.server.config_lock:
                settings = self._settings()
                auth = self._authentication(settings)
        except (OSError, ValueError, AuthError):
            self._error(503, "Authentication is temporarily unavailable")
            return False
        if not auth:
            self._error(401, "Authentication required")
            return False
        self.authentication = auth
        if write:
            if not self._origin_allowed():
                return False
            if not hmac.compare_digest(
                    self.headers.get("X-CSRF-Token", "").encode(), auth["csrf_token"].encode()):
                self._error(403, "Invalid session request")
                return False
        return True

    def _settings(self):
        return load_settings(self.server.config_path) if self.server.config_path.exists() else self.server.initial_settings

    def _configuration(self):
        return (Configuration.load(self.server.config_path) if self.server.config_path.exists()
                else Configuration(self.server.initial_settings, {}))

    def _api_scope(self):
        parsed = urlsplit(self.path)
        parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True) if parsed.query else {}
        if len(parameters.get('modem', [])) > 1:
            raise ValueError('Only one modem profile can be selected')
        if (set(parameters)-{'modem'}
                or (parameters and parsed.path not in {'/api/config', '/api/status'})):
            raise LookupError('Unsupported API query parameters')
        identifier = parameters.get('modem', ['default'])[0]
        if not valid_profile(identifier):
            raise ValueError('Invalid modem profile identifier')
        return parsed.path, identifier

    def _inventory(self):
        try:
            content = self.server.status_path.read_bytes()
            snapshot = _strict_json(content) if len(content) <= MAX_SNAPSHOT else {}
            return snapshot.get('modems', []) if isinstance(snapshot, dict) else []
        except (OSError, ValueError):
            return []

    def _profile_settings(self, configuration, identifier, *, create=False):
        if identifier == 'default' or identifier in configuration.profiles:
            return configuration.select(identifier)
        entry = next((entry for entry in self._inventory() if entry.get('id') == identifier), None)
        if entry:
            return replace(configuration.base, modem=replace(configuration.base.modem, **entry['selector']))
        if create:
            return configuration.base
        raise ValueError(f'Unknown modem profile: {identifier}')

    def _session_response(self, settings, auth):
        return {"authenticated": bool(auth), "username": auth["username"] if auth else None,
                "csrf_token": auth["csrf_token"] if auth else None,
                "expires_in": auth["expires_in"] if auth else None,
                "methods": {"password": bool(settings.web.password_hash)},
                "readonly": False}

    def _session_cookie(self, sid="", expires_in=0):
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE] = sid
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["samesite"] = "Strict"
        cookie[SESSION_COOKIE]["max-age"] = max(0, int(expires_in))
        return cookie.output(header="").strip()

    def _read_payload(self):
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "Transfer encoding is not supported")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._error(411, "Content-Length is required")
            return None
        try:
            if len(lengths) != 1:
                raise ValueError()
            length = int(lengths[0])
            if length < 0:
                raise ValueError()
        except ValueError:
            self._error(400, "Invalid Content-Length")
            return None
        if length > MAX_BODY:
            self._error(413, "Request exceeds 64 KiB")
            return None
        if self.headers.get_content_type() != "application/json":
            self._error(415, "Content-Type must be application/json")
            return None
        try:
            content = self.rfile.read(length)
            if len(content) != length:
                raise ValueError("Incomplete request body")
            payload = _strict_json(content)
            if not isinstance(payload, dict):
                raise ValueError("Body must be a JSON object")
            return payload
        except (ValueError, UnicodeError) as error:
            self._error(400, str(error))
        except (socket.timeout, TimeoutError):
            self._error(408, "Request body timed out")
        return None

    def do_GET(self):
        if not self._host_allowed():
            self._error(403, "Host header is not allowed")
            return
        asset = static_file(self.server.static_root, self.path)
        if asset is not None:
            filename, content_type, cache = asset
            try:
                self._send(200, filename.read_bytes(), content_type, {'Cache-Control': cache})
            except OSError:
                self._error(503, 'Web UI asset is unavailable')
            return
        if self.path == "/api/session":
            try:
                with self.server.config_lock:
                    settings = self._settings()
                    self._send(200, self._session_response(settings, self._authentication(settings)))
            except (OSError, ValueError, AuthError):
                self._error(503, "Authentication is temporarily unavailable")
            return
        if not self._guard():
            return
        try:
            resource, identifier = self._api_scope()
        except (ValueError, LookupError) as error:
            self._error(404 if isinstance(error, LookupError) else 400, str(error))
            return
        if resource == "/api/status":
            try:
                with self.server.status_path.open("rb") as stream:
                    content = stream.read(MAX_SNAPSHOT + 1)
                if len(content) > MAX_SNAPSHOT:
                    raise ValueError("Snapshot exceeds size limit")
                snapshot = _strict_json(content)
                if not isinstance(snapshot, dict):
                    raise ValueError("Snapshot must be an object")
                if urlsplit(self.path).query:
                    entries = snapshot.get('modems', [])
                    entry = next((value for value in entries if value.get('id') == identifier), None)
                    if entry is None and identifier != 'default' and identifier not in self._configuration().profiles:
                        self._error(404, 'Unknown modem profile')
                        return
                    target = self.server.status_path.parent/'modems'/identifier/'status.json'
                    if target.exists() and entry and entry.get('detected'):
                        content = target.read_bytes()
                        if len(content) > MAX_SNAPSHOT:
                            raise ValueError('Snapshot exceeds size limit')
                        scoped = _strict_json(content)
                        if not isinstance(scoped, dict):
                            raise ValueError('Snapshot must be an object')
                    elif snapshot.get('fleet') or identifier != 'default':
                        scoped = {}
                    else:
                        scoped = dict(snapshot)
                    if snapshot.get('fleet') or identifier != 'default':
                        scoped = modem_snapshot(scoped, entry or {},
                            manager_heartbeat=snapshot.get('manager_heartbeat_monotonic'),
                            boot_id=snapshot.get('boot_id'))
                    resources = dict(scoped.get('resources') or {})
                    processes = dict(scoped.get('processes') or {})
                    if 'web' in (snapshot.get('processes') or {}):
                        processes['web'] = snapshot['processes']['web']
                    for key in ('web', 'manager'):
                        resources[key] = (snapshot.get('resources') or {}).get(key)
                    snapshot = dict(scoped, resources=resources, processes=processes, fleet=snapshot.get('fleet', False),
                        modems=entries, selected_modem=identifier, config_revision=snapshot.get('config_revision'),
                        ownership_cleanup=snapshot.get('ownership_cleanup', []),
                        configuration_error=snapshot.get('configuration_error'))
                self._send(200, {"snapshot": snapshot,
                                 "freshness": snapshot_freshness(snapshot, self.server.boot_id)})
            except FileNotFoundError:
                self._send(200, {"snapshot": None})
            except (OSError, ValueError):
                self._send(200, {"snapshot": None, "warning": "Status snapshot is temporarily unavailable"})
        elif resource == "/api/config":
            try:
                with self.server.config_lock:
                    configuration = self._configuration()
                    settings = self._profile_settings(configuration, identifier)
                    self._send(200, {"config": _public_config(settings), "revision": configuration.revision,
                                     'modem_id': identifier, 'profiles': ['default', *configuration.profiles],
                                     "editable": bool(self.authentication),
                                     'listen_urls': self.server.listen_urls})
            except (OSError, ValueError):
                self._error(503, "Cannot read project configuration")
        else:
            self._error(404, "Not found")

    def do_POST(self):
        if not self._host_allowed():
            self._error(403, "Host header is not allowed")
            return
        if self.path == "/api/login":
            if not self._origin_allowed():
                return
            payload = self._read_payload()
            if payload is None:
                return
            if set(payload) != {"username", "password"} or any(type(value) is not str for value in payload.values()):
                self._error(400, "Provide username and password")
                return
            try:
                with self.server.config_lock:
                    settings = self._settings()
                    self.server.auth.reconfigure(settings.web)
                session = self.server.auth.login(payload["username"], payload["password"], self.client_address[0])
                previous = self._session_id()
                if previous and previous != session["session_id"]:
                    self.server.auth.logout(previous)
                self._send(200, self._session_response(settings, session), headers={
                    "Set-Cookie": self._session_cookie(session["session_id"], session["expires_in"])})
            except AuthError as error:
                headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
                self._send(error.status, {"error": error.message}, headers=headers)
            except (OSError, ValueError):
                self._error(503, "Authentication is temporarily unavailable")
            return
        if not self._guard(write=True):
            return
        if self.path == "/api/logout":
            payload = self._read_payload()
            if payload is None:
                return
            if payload:
                self._error(400, "Body must be empty")
                return
            try:
                self.server.auth.logout(self._session_id())
                self._send(200, {"authenticated": False}, headers={"Set-Cookie": self._session_cookie()})
            except AuthError:
                self._error(503, "Cannot close session")
            return
        try:
            resource, identifier = self._api_scope()
        except (ValueError, LookupError) as error:
            self._error(404 if isinstance(error, LookupError) else 400, str(error))
            return
        if resource != "/api/config":
            self._error(404, "Not found")
            return
        payload = self._read_payload()
        if payload is None:
            return
        try:
            if set(payload) != {"config"}:
                raise ValueError("Body must contain only a config object")
            with self.server.config_lock, configuration_lock(self.server.config_path):
                configuration = self._configuration()
                current = self._profile_settings(configuration, identifier, create=True)
                expected = self.headers.get('If-Match')
                if expected and expected != configuration.revision:
                    self._error(412, 'Configuration changed elsewhere. Refresh and review your changes.')
                    return
                # A reset/logout can occur while a slow request body arrives.
                # Recheck in the same critical section as the eventual write.
                auth = self._authentication(current)
                if not auth:
                    self._error(401, "Authentication required")
                    return
                if not hmac.compare_digest(
                        self.headers.get("X-CSRF-Token", "").encode(), auth["csrf_token"].encode()):
                    self._error(403, "Invalid session request")
                    return
                candidate = _merge_settings(current, payload["config"])
                updated = configuration.update(identifier, candidate)
                _write_config(self.server.config_path, updated)
                self.server.auth.reconfigure(candidate.web)
            effects = [name for name, changed in (
                ('connection', (current.modem, current.network, current.recovery) != (candidate.modem, candidate.network, candidate.recovery)),
                ('telemetry', current.telemetry != candidate.telemetry),
                ('web', current.web != candidate.web)) if changed]
            self._send(200, {"config": _public_config(candidate), "revision": updated.revision, 'modem_id': identifier,
                             "effects": effects, "restart_required": False, "auto_apply": True})
        except (ValueError, UnicodeError) as error:
            self._error(400, str(error))
        except (socket.timeout, TimeoutError):
            self._error(408, "Request body timed out")
        except OSError:
            self._error(503, "Cannot save project configuration")
        except AuthError:
            self._error(503, "Configuration saved; please sign in again after the service restarts")


def build_server(settings, config_path, status_path=STATUS_PATH, *, server_address=None, max_workers=8,
                 auth_manager=None, session_state_path=None, static_root=None):
    """Create an unstarted server; injectable bind address supports isolated tests."""
    if not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be between 1 and 32")
    validate_settings(settings)
    host = settings.web.host
    if host == 'auto':
        raise ValueError('Use serve() for automatic localhost and primary interface listeners')
    loopback = _loopback(host)
    if not loopback and not settings.web.password_hash:
        raise ValueError("A web password is required outside loopback")
    public_root = validate_static_export(static_root if static_root is not None else
                                         Path(__file__).resolve().parents[2] / 'frontend/out')
    server = _Server(server_address or (host, settings.web.port), _Handler, max_workers)
    server.initial_settings = settings
    server.loopback = loopback
    server.config_path = Path(config_path).resolve()
    server.status_path = Path(status_path)
    try:
        server.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        server.boot_id = None
    server.auth = auth_manager or AuthManager(settings.web,
        state_path=session_state_path or server.status_path.with_name('web-sessions.json'), boot_id=server.boot_id)
    server.static_root = public_root
    server.config_lock = threading.Lock()
    server.wildcard = host in {"0.0.0.0", "::"}
    server.allowed_hosts = {host.lower(), str(server.server_address[0]).lower()}
    if loopback:
        server.allowed_hosts.update({"localhost", "127.0.0.1", "::1"})
    rendered_host = '[' + host + ']' if ':' in host else host
    server.listen_url = f'http://{rendered_host}:{server.server_port}'
    server.listen_urls = [server.listen_url]
    return server


create_server = build_server


def serve(settings, config_path, status_path=STATUS_PATH, stop=None, *,
          address_resolver=management_interface_address, refresh_interval=None, static_root=None, progress=None):
    """One HTTP process, localhost plus a management LAN IPv4 when host=auto.

    Refreshing the selected address never queries the modem or restarts its
    connection. Both listeners share the same request limit and config lock.
    """
    validate_settings(settings)
    interval = settings.web.address_refresh if refresh_interval is None else refresh_interval
    stop = stop if stop is not None else threading.Event()
    selector = selectors.DefaultSelector()
    servers, errors = {}, {}
    config_lock = threading.Lock()
    workers = threading.BoundedSemaphore(settings.web.max_workers)
    auth_manager = AuthManager(settings.web, state_path=Path(status_path).with_name('web-sessions.json'))
    next_refresh = 0
    try:
        while not stop.is_set():
            if progress:
                progress()
            if time.monotonic() >= next_refresh:
                hosts = [settings.web.host]
                if settings.web.host == 'auto':
                    current = address_resolver()
                    hosts = ['127.0.0.1'] + ([current[1]] if current else [])
                wanted = set(hosts)
                for host in list(servers):
                    if host not in wanted:
                        server = servers.pop(host)
                        selector.unregister(server)
                        server.server_close()
                for host in dict.fromkeys(hosts):
                    if host in servers:
                        continue
                    candidate = replace(settings, web=replace(settings.web, host=host))
                    try:
                        server = build_server(candidate, config_path, status_path, auth_manager=auth_manager,
                                              max_workers=settings.web.max_workers, static_root=static_root)
                    except OSError as error:
                        if settings.web.host != 'auto' or host == '127.0.0.1':
                            raise
                        if time.monotonic()-errors.get(host, float('-inf')) >= 60:
                            logging.getLogger(__name__).warning('Cannot listen on %s: %s', host, error)
                            errors[host] = time.monotonic()
                        continue
                    server.timeout = 0.1
                    server.config_lock, server.workers = config_lock, workers
                    servers[host] = server
                    selector.register(server, selectors.EVENT_READ)
                    logging.getLogger(__name__).info('Web listening on %s', server.listen_url)
                urls = [server.listen_url for server in servers.values()]
                for server in servers.values():
                    server.listen_urls = urls
                # A missing or changed default interface is checked without
                # creating extra threads, processes or IP traffic.
                errors = {host: when for host, when in errors.items() if host in wanted}
                next_refresh = time.monotonic() + interval if settings.web.host == 'auto' else float('inf')
            for key, _ in selector.select(timeout=min(0.5, max(0, next_refresh-time.monotonic()))):
                key.fileobj.handle_request()
    finally:
        for server in servers.values():
            server.server_close()
        selector.close()
