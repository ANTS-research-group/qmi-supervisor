"""Scoped API tokens for the machine-facing HTTP interface (E1.1).

Only the SHA-256 digest of a token secret is stored. The store is root-only
and lives in the application's configuration directory, never in the INI.
All comparisons of secrets are constant time.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time

DEFAULT_TOKEN_PATH = '/etc/qmi-supervisor/api-tokens.json'
SCOPES = ('read:status', 'read:events', 'read:metrics')
MAX_TOKENS = 64
MAX_STATE_BYTES = 64 * 1024
NAME_MAX = 64
TOKEN_ID = re.compile(r'[0-9a-f]{8}')
TOKEN = re.compile(r'qms_([0-9a-f]{8})_([A-Za-z0-9_-]{43})')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
_LOCK = threading.RLock()


class TokenError(ValueError):
    """Invalid token input or storage; the message is safe to show to root."""


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(moment):
    try:
        return moment.isoformat()
    except AttributeError:
        return str(moment)


def _parse_iso(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _valid_name(name):
    return (isinstance(name, str) and 0 < len(name) <= NAME_MAX
            and not any(ord(character) < 32 for character in name))


def _valid_scopes(scopes):
    if isinstance(scopes, str):
        scopes = [item.strip() for item in scopes.split(',') if item.strip()]
    if not isinstance(scopes, (list, tuple)) or not scopes:
        raise TokenError('At least one scope is required')
    cleaned = []
    for scope in scopes:
        if not isinstance(scope, str) or scope not in SCOPES:
            raise TokenError('Unknown scope; allowed: ' + ', '.join(SCOPES))
        if scope not in cleaned:
            cleaned.append(scope)
    return cleaned


class TokenStore:
    """Read/write the token file; verify bearer tokens on every API request."""

    def __init__(self, path=None, *, clock=time.time, utcnow=_utcnow):
        self.path = Path(path or DEFAULT_TOKEN_PATH)
        self.clock, self.utcnow = clock, utcnow
        self._last_flush = 0.0
        self._pending = {}

    def _load(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError:
            return []
        try:
            with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or metadata.st_mode & 0o077 or metadata.st_size > MAX_STATE_BYTES):
                    return []
                data = json.load(stream)
        except (OSError, ValueError, UnicodeError):
            return []
        if not isinstance(data, dict) or data.get('version') != 1:
            return []
        tokens, seen = [], set()
        for record in data.get('tokens') or []:
            if not isinstance(record, dict):
                continue
            identifier = record.get('id')
            if (not isinstance(identifier, str) or not TOKEN_ID.fullmatch(identifier)
                    or identifier in seen or not _valid_name(record.get('name'))
                    or not isinstance(record.get('digest'), str)
                    or not DIGEST.fullmatch(record['digest'])):
                continue
            try:
                scopes = _valid_scopes(record.get('scopes'))
            except TokenError:
                continue
            seen.add(identifier)
            tokens.append({'id': identifier, 'name': record['name'], 'scopes': scopes,
                           'digest': record['digest'],
                           'created_utc': _iso(record.get('created_utc') or ''),
                           'expires_utc': record.get('expires_utc'),
                           'last_used_utc': record.get('last_used_utc')})
            if len(tokens) >= MAX_TOKENS:
                break
        return tokens

    def _save(self, tokens):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.' + self.path.name + '.',
                                                 dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'tokens': tokens}, stream,
                          separators=(',', ':'), allow_nan=False)
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
        self._last_flush = self.clock()

    def create(self, name, scopes, expires_days=None):
        """Create a token and return its secret once; only the digest is stored."""
        if not _valid_name(name):
            raise TokenError('The token name must be 1..64 printable characters')
        scopes = _valid_scopes(scopes)
        expires_days = self._days(expires_days)
        with _LOCK:
            tokens = self._load()
            if len(tokens) >= MAX_TOKENS:
                raise TokenError(f'At most {MAX_TOKENS} tokens are allowed')
            identifier = secrets.token_hex(4)
            while any(record['id'] == identifier for record in tokens):
                identifier = secrets.token_hex(4)
            secret = secrets.token_urlsafe(32)
            now = self.utcnow()
            expires = None
            if expires_days is not None:
                from datetime import timedelta
                expires = _iso(now + timedelta(days=expires_days))
            tokens.append({'id': identifier, 'name': name, 'scopes': scopes,
                           'digest': 'sha256:' + hashlib.sha256(secret.encode()).hexdigest(),
                           'created_utc': _iso(now), 'expires_utc': expires,
                           'last_used_utc': None})
            self._save(tokens)
        return f'qms_{identifier}_{secret}'

    @staticmethod
    def _days(value):
        if value in (None, ''):
            return None
        try:
            days = int(value)
        except (TypeError, ValueError):
            raise TokenError('The expiry must be a whole number of days') from None
        if days < 1 or days > 3650:
            raise TokenError('The expiry must be between 1 and 3650 days')
        return days

    def list(self):
        with _LOCK:
            return [{'id': record['id'], 'name': record['name'], 'scopes': list(record['scopes']),
                     'created_utc': record['created_utc'], 'expires_utc': record['expires_utc'],
                     'last_used_utc': record['last_used_utc']} for record in self._load()]

    def revoke(self, identifier):
        with _LOCK:
            tokens = self._load()
            remaining = [record for record in tokens if record['id'] != identifier]
            if len(remaining) == len(tokens):
                return False
            self._save(remaining)
            return True

    def verify(self, token):
        """The public record of a valid, unexpired token with any scope, else None."""
        if not isinstance(token, str) or len(token) > 128:
            return None
        match = TOKEN.fullmatch(token)
        if match is None:
            return None
        identifier, secret = match.groups()
        digest = 'sha256:' + hashlib.sha256(secret.encode()).hexdigest()
        with _LOCK:
            tokens = self._load()
            record = next((item for item in tokens if item['id'] == identifier), None)
            if record is None or not hmac.compare_digest(record['digest'], digest):
                return None
            expires = _parse_iso(record.get('expires_utc'))
            if expires is not None and self.utcnow() >= expires:
                return None
            self._touch(record)
            return {'id': record['id'], 'name': record['name'],
                    'scopes': list(record['scopes']), 'expires_utc': record['expires_utc'],
                    'last_used_utc': record['last_used_utc']}

    def _touch(self, record):
        """Update last_used at most once per minute per token, batched per minute."""
        now = self.clock()
        value = self._pending.get(record['id'])
        if value is not None:
            record['last_used_utc'] = value
        previous = _parse_iso(record.get('last_used_utc'))
        if previous is not None and (self.utcnow() - previous).total_seconds() < 60:
            return
        record['last_used_utc'] = _iso(self.utcnow())
        self._pending[record['id']] = record['last_used_utc']
        # The first use of a token is persisted at once; later uses are batched.
        if previous is not None and now - self._last_flush < 60:
            return
        tokens = self._load()
        for item in tokens:
            item['last_used_utc'] = self._pending.get(item['id'], item['last_used_utc'])
        try:
            self._save(tokens)
        except OSError:
            pass
        self._pending.clear()
