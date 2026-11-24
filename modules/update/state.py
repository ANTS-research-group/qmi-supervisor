"""Paths, the updater state file and its lock.

The state file is written only by qmi-supervisor-update, replaced atomically,
root 0644 so `status`/`list` work without sudo. It is bounded to 2 MiB and
never contains an access token or a device identifier.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile

from modules.update.releases import apply_signature_policy, is_newer, newest

SCHEMA_VERSION = 1
MAX_STATE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class UpdatePaths:
    """Every path the updater touches; tests replace each one with a temp path.

    `state_dir` is the updater's name for the `root` (the state directory); `root`
    is kept as an alias so both spellings read the same directory.
    """
    config: Path = Path('/etc/qmi-supervisor.ini')
    state_dir: Path = Path('/var/lib/qmi-supervisor/update')
    token: Path = Path('/etc/qmi-supervisor-update-token')
    lock: Path = Path('/run/qmi-supervisor-update.lock')
    requests: Path = Path('/run/qmi-supervisor-update')
    runtime: Path = Path('/run/qmi-supervisor')
    keyring: Path = Path('/opt/qmi-supervisor/keys/release-signing.gpg')
    python: Path = Path('/usr/bin/python3')

    @property
    def root(self):
        return self.state_dir

    @property
    def state(self):
        return self.state_dir / 'state.json'

    @property
    def history(self):
        return self.state_dir / 'history.jsonl'

    @property
    def history_rotated(self):
        return self.state_dir / 'history.jsonl.1'

    @property
    def cache(self):
        return self.state_dir / 'cache'

    @property
    def backups(self):
        return self.state_dir / 'backups'


def utc_now():
    return datetime.now(timezone.utc)


def stamp(moment):
    """A UTC timestamp with '+00:00' (parseable by fromisoformat on Python 3.10)."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_stamp(value):
    """An aware datetime from a stored stamp, else None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def new_state(current_version, now):
    """Every schema key with its empty value."""
    return {
        'schema_version': SCHEMA_VERSION,
        'updated_utc': stamp(now),
        'current_version': current_version,
        'source': None,
        'channel': None,
        'require_signature': None,
        'checked_utc': None,
        'last_success_utc': None,
        'check_error': None,
        'etag': None,
        'releases': [],
        'newest_available': None,
        'newest_installable': None,
        'update_available': False,
        'in_progress': None,
        'last_result': None,
    }


def load_state(path):
    """The stored state, or None when it is missing, too large or not schema version 1."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
        with path.open('rb') as stream:
            document = json.loads(stream.read().decode('utf-8'))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if (not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION
            or not isinstance(document.get('releases'), list)):
        return None
    return document


def ensure_state_dir(directory):
    """Create the state directory 0755; refuse a symlink or a non-directory."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError(f'Updater state directory is a symlink: {directory}')
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError(f'Updater state path is not a directory: {directory}')
    os.chmod(directory, 0o755)


def _text_safe(value):
    """Replace lone surrogates so an untrusted string can always be written as UTF-8."""
    if isinstance(value, str):
        return value.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    if isinstance(value, dict):
        return {_text_safe(key): _text_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_text_safe(item) for item in value]
    return value


def write_state(path, state, *, now):
    """Stamp and atomically replace the state file (0644)."""
    path = Path(path)
    state['updated_utc'] = stamp(now)
    ensure_state_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix='.state.json.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(_text_safe(state), ensure_ascii=False, indent=1, sort_keys=True))
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def refresh_derived(state, *, current_version, channel, require_signature):
    """Recompute channel/installability-dependent fields in place and return the state."""
    releases = [apply_signature_policy(release, require_signature) for release in state.get('releases', [])]
    state['releases'] = releases
    state['current_version'] = current_version
    state['channel'] = channel
    state['require_signature'] = require_signature
    state['newest_available'] = newest(releases, channel, installable_only=False)
    state['newest_installable'] = newest(releases, channel, installable_only=True)
    state['update_available'] = (state['newest_installable'] is not None
                                 and is_newer(state['newest_installable']['version'], current_version))
    return state


@contextmanager
def state_lock(path):
    """Exclusive non-blocking lock; yields True when acquired, False when held elsewhere."""
    path = Path(path)
    ensure_state_dir(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)
