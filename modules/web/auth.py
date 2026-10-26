"""Bounded local web authentication using only the Python standard library.

PBKDF2-HMAC-SHA256 uses OWASP's documented 600,000 iteration work factor:
https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html#pbkdf2
Session identifiers are random bearer secrets; only their SHA256 digests are
persisted. All timestamps use this boot's monotonic clock, never wall time.
The HTTP layer owns cookie attributes, origin checks, CSRF checks and TLS.
"""
from collections import OrderedDict, deque
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
import unicodedata


PBKDF2_ITERATIONS = 600_000
MAX_ITERATIONS = 2_000_000
MIN_PASSWORD_CHARACTERS = 10
MAX_PASSWORD_BYTES = 256
MAX_SESSIONS = 64
MAX_CLIENTS = 128
FAILED_ATTEMPTS = 5
RATE_WINDOW = 60
DEFAULT_STATE_PATH = '/run/qmi-supervisor/web-sessions.json'
MAX_STATE_BYTES = 64 * 1024
_LOCK = threading.RLock()
_HASH_SLOTS = threading.BoundedSemaphore(2)
_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
_DIGEST = re.compile(r'[0-9a-f]{64}')


class AuthError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


def _password_bytes(password):
    if (not isinstance(password, str) or len(password) < MIN_PASSWORD_CHARACTERS
            or any(unicodedata.category(character) == 'Cc' for character in password)):
        raise ValueError('The password must contain at least 10 characters and no control characters.')
    try:
        encoded = password.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('The password must be valid UTF-8 text.') from None
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError('The password must not exceed 256 UTF-8 bytes.')
    return encoded


def _hash_parts(encoded):
    if not isinstance(encoded, str) or len(encoded) > 160:
        return None
    parts = encoded.split('$')
    if len(parts) != 4 or parts[0] != 'pbkdf2_sha256' or not re.fullmatch(r'[0-9]{6,7}', parts[1]):
        return None
    iterations = int(parts[1])
    if not PBKDF2_ITERATIONS <= iterations <= MAX_ITERATIONS:
        return None
    if not re.fullmatch(r'[0-9a-f]{32}', parts[2]) or not _DIGEST.fullmatch(parts[3]):
        return None
    return iterations, bytes.fromhex(parts[2]), bytes.fromhex(parts[3])


def is_password_hash(encoded):
    return _hash_parts(encoded) is not None


def hash_password(password, *, iterations=PBKDF2_ITERATIONS):
    data = _password_bytes(password)
    if type(iterations) is not int or not PBKDF2_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError('Invalid PBKDF2 work factor')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', data, salt, iterations, dklen=32)
    return f'pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    parts = _hash_parts(encoded)
    if parts is None:
        return False
    try:
        data = _password_bytes(password)
    except ValueError:
        return False
    iterations, salt, expected = parts
    actual = hashlib.pbkdf2_hmac('sha256', data, salt, iterations, dklen=32)
    return hmac.compare_digest(actual, expected)


def _boot_id():
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if value and len(value) <= 128:
            return value
    except OSError:
        pass
    # Without a verifiable boot identity, reject state from earlier processes.
    return secrets.token_hex(16)


def _finite(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class AuthManager:
    """One shared manager serves all listeners; authentication never writes disk.

    ``authenticate`` and ``logout`` take the opaque cookie VALUE, not a Cookie
    header. Login alone returns ``session_id`` so the HTTP layer can set it as
    HttpOnly; authenticated session info contains username, CSRF and lifetime.
    """
    def __init__(self, settings_web, state_path=DEFAULT_STATE_PATH, *, clock=time.monotonic, boot_id=None):
        self.username = getattr(settings_web, 'username', 'admin')
        self.password_hash = getattr(settings_web, 'password_hash', '')
        self.timeout = getattr(settings_web, 'session_timeout', 28800)
        if not _finite(self.timeout) or self.timeout <= 0 or self.timeout > 31 * 86400:
            raise ValueError('Invalid web session lifetime')
        self.clock, self.path = clock, Path(state_path)
        self.boot_id = boot_id if isinstance(boot_id, str) and boot_id else _boot_id()
        self.configured = (isinstance(self.username, str) and bool(self.username)
                           and is_password_hash(self.password_hash))
        self._fingerprint = hashlib.sha256(json.dumps(
            [self.username, self.password_hash], separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        self._sessions = OrderedDict()
        self._clients = OrderedDict()
        self._storage_failed = False
        self._load()

    def reconfigure(self, settings_web):
        """Revoke rotated credentials once; unchanged request checks do no I/O."""
        username = getattr(settings_web, 'username', 'admin')
        password_hash = getattr(settings_web, 'password_hash', '')
        timeout = getattr(settings_web, 'session_timeout', 28800)
        if not _finite(timeout) or not 0 < timeout <= 31 * 86400:
            raise ValueError('Invalid web session lifetime')
        fingerprint = hashlib.sha256(json.dumps(
            [username, password_hash], separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
        with _LOCK:
            changed = fingerprint != self._fingerprint
            if not changed and timeout == self.timeout:
                return False
            self.username, self.password_hash = username, password_hash
            self.configured = isinstance(username, str) and bool(username) and is_password_hash(password_hash)
            self._fingerprint, self.timeout = fingerprint, timeout
            had_sessions = bool(self._sessions)
            if changed:
                self._sessions.clear()
            else:
                for session in self._sessions.values():
                    session['expires_monotonic'] = min(session['expires_monotonic'],
                                                       session['issued_monotonic']+timeout)
                self._prune(self.clock())
            try:
                # A read-only access change must not create a session store just
                # because its lifetime changed. Existing stores still need
                # durable revocation, including records not loaded here.
                if had_sessions or self.path.exists():
                    self._save()
            except OSError:
                self._sessions.clear()
                self._storage_failed = True
                raise AuthError(503, 'Could not save the access change; existing sessions were revoked.') from None
            self._storage_failed = False
            return True

    @staticmethod
    def _sid_digest(session_id):
        if not isinstance(session_id, str) or not _TOKEN.fullmatch(session_id):
            return None
        return hashlib.sha256(session_id.encode('ascii')).hexdigest()

    def _prune(self, now):
        for digest, session in list(self._sessions.items()):
            if not session['issued_monotonic'] <= now < session['expires_monotonic']:
                del self._sessions[digest]
        for key, attempts in list(self._clients.items()):
            while attempts['failures'] and now-attempts['failures'][0] >= RATE_WINDOW:
                attempts['failures'].popleft()
            if not attempts['failures'] and not attempts['pending']:
                del self._clients[key]

    def _load(self):
        if not self.configured:
            return
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or metadata.st_mode & 0o077 or metadata.st_size > MAX_STATE_BYTES):
                    return
                data = json.load(stream)
            if (not isinstance(data, dict) or data.get('version') != 1
                    or data.get('boot_id') != self.boot_id
                    or data.get('fingerprint') != self._fingerprint
                    or not isinstance(data.get('sessions'), dict) or len(data['sessions']) > MAX_SESSIONS):
                return
            now = self.clock()
            loaded = []
            for digest, session in data['sessions'].items():
                if (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                        or not isinstance(session, dict) or session.get('username') != self.username
                        or not isinstance(session.get('csrf_token'), str)
                        or not _TOKEN.fullmatch(session['csrf_token'])):
                    continue
                issued, expiry = session.get('issued_monotonic'), session.get('expires_monotonic')
                if (not _finite(issued) or not _finite(expiry) or not 0 <= issued <= now < expiry
                        or expiry-issued > 31 * 86400):
                    continue
                expiry = min(expiry, issued+self.timeout)
                if now >= expiry:
                    continue
                loaded.append((digest, dict(username=self.username, csrf_token=session['csrf_token'],
                                            issued_monotonic=issued, expires_monotonic=expiry)))
            self._sessions.update(sorted(loaded, key=lambda item: item[1]['issued_monotonic']))
        except (OSError, ValueError, TypeError, UnicodeError):
            # Missing/corrupt/untrusted state never grants a session. A new
            # password-authenticated login may atomically replace it.
            return

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.'+self.path.name+'.', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(dict(version=1, boot_id=self.boot_id, fingerprint=self._fingerprint,
                               sessions=self._sessions), stream, separators=(',', ':'), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _info(self, session, now):
        return dict(username=session['username'], csrf_token=session['csrf_token'],
                    expires_in=max(0, math.ceil(session['expires_monotonic']-now)))

    def login(self, username, password, client_key):
        key = hashlib.sha256(str(client_key)[:256].encode('utf-8', errors='replace')).hexdigest()
        with _LOCK:
            if not self.configured:
                raise AuthError(503, 'Username and password access is not configured.')
            expected_user, encoded, fingerprint = self.username, self.password_hash, self._fingerprint
            now = self.clock()
            self._prune(now)
            attempts = self._clients.get(key)
            if attempts is None:
                if len(self._clients) >= MAX_CLIENTS:
                    raise AuthError(429, 'Too many sign-in attempts. Try again later.', RATE_WINDOW)
                attempts = {'failures': deque(maxlen=FAILED_ATTEMPTS), 'pending': 0}
                self._clients[key] = attempts
            if len(attempts['failures'])+attempts['pending'] >= FAILED_ATTEMPTS:
                wait = math.ceil(RATE_WINDOW-(now-attempts['failures'][0])) if attempts['failures'] else RATE_WINDOW
                raise AuthError(429, 'Too many sign-in attempts. Try again later.', max(1, wait))
            if not _HASH_SLOTS.acquire(blocking=False):
                if not attempts['failures'] and not attempts['pending']:
                    self._clients.pop(key, None)
                raise AuthError(429, 'Other sign-in requests are in progress. Try again.', 1)
            attempts['pending'] += 1
        valid = False
        try:
            # Hash even an unknown username, avoiding account-dependent work.
            matches_password = verify_password(password, encoded)
            valid = (isinstance(username, str) and hmac.compare_digest(
                username.encode('utf-8', errors='replace'), expected_user.encode('utf-8')) and matches_password)
        finally:
            _HASH_SLOTS.release()
            with _LOCK:
                attempts['pending'] -= 1
                if not valid:
                    attempts['failures'].append(self.clock())
        if not valid:
            raise AuthError(401, 'Incorrect username or password.')
        with _LOCK:
            if fingerprint != self._fingerprint:
                raise AuthError(401, 'Credentials changed. Sign in again.')
            now = self.clock()
            self._prune(now)
            attempts['failures'].clear()
            for _ in range(8):
                session_id = secrets.token_urlsafe(32)
                digest = self._sid_digest(session_id)
                if digest is not None and digest not in self._sessions:
                    break
            else:
                raise AuthError(503, 'Could not create a secure session.')
            session = dict(username=self.username, csrf_token=secrets.token_urlsafe(32),
                           issued_monotonic=now, expires_monotonic=now+self.timeout)
            previous = self._sessions.copy()
            self._sessions[digest] = session
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
            try:
                self._save()
            except OSError:
                self._sessions = previous
                raise AuthError(503, 'Could not save the session. Check device storage.') from None
            self._storage_failed = False
            return dict(self._info(session, now), session_id=session_id)

    def authenticate(self, session_id):
        digest = self._sid_digest(session_id)
        if digest is None:
            return None
        with _LOCK:
            if self._storage_failed:
                return None
            now = self.clock()
            self._prune(now)
            session = self._sessions.get(digest)
            return self._info(session, now) if session else None

    def logout(self, session_id):
        digest = self._sid_digest(session_id)
        if digest is None:
            return False
        with _LOCK:
            self._prune(self.clock())
            if digest not in self._sessions:
                return False
            del self._sessions[digest]
            try:
                self._save()
            except OSError:
                self._storage_failed = True
                self._sessions.clear()
                raise AuthError(503, 'Could not confirm sign-out in session storage.') from None
            return True
