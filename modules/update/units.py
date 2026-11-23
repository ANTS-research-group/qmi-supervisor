"""Systemd unit names, request files and the start of an updater unit.

`%I` hands the unit's unescaped instance to the CLI, so every instance is
built with `escape_instance()` (exactly `systemd-escape -- VALUE`) and the CLI
re-validates the result. Request files live in `/run` and are consumed by the
unit; a stale or mismatching one is ignored and deleted.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid

from modules.update.errors import UpdateError
from modules.update.files import ensure_private_dir, read_nofollow, write_private
from modules.update.state import parse_stamp, stamp

INSTALL_PREFIX = 'qmi-supervisor-update-install@'
ROLLBACK_UNIT = 'qmi-supervisor-update-rollback.service'

REQUEST_MAX_BYTES = 4 * 1024
REQUEST_PENDING_SECONDS = 120
REQUEST_MAX_AGE_SECONDS = 600
REQUEST_SCHEMA = 1


def escape_instance(value):
    """Exactly what `systemd-escape -- VALUE` prints (verified against systemd 249)."""
    out = []
    for index, byte in enumerate(value.encode('utf-8')):
        char = chr(byte)
        if char == '/':
            out.append('-')
        elif char.isascii() and (char.isalnum() or char in ':_.') and not (index == 0 and char == '.'):
            out.append(char)
        else:
            out.append('\\x%02x' % byte)
    return ''.join(out)


def install_unit(version):
    return INSTALL_PREFIX + escape_instance(version) + '.service'


def running_in_unit(unit, cgroup_text):
    """True when /proc/self/cgroup puts this process in `unit` (cgroup v2, or v1 name=systemd)."""
    for line in cgroup_text.splitlines():
        hierarchy, _, rest = line.partition(':')
        controllers, _, path = rest.partition(':')
        if (hierarchy == '0' and controllers == '') or controllers == 'name=systemd':
            if path.rstrip('/').rsplit('/', 1)[-1] == unit:
                return True
    return False


def request_path(paths, version):
    digest = hashlib.sha256(version.encode('utf-8')).hexdigest()[:16]
    return Path(paths.requests) / f'request-{digest}.json'


def _request_id():
    return uuid.uuid4().hex


def write_request(paths, version, *, reason, requested_by, utcnow):
    """Write the request file for `version` and return its request id."""
    directory = ensure_private_dir(Path(paths.requests), code='request_failed', step='resolve')
    request_id = _request_id()
    document = {
        'schema_version': REQUEST_SCHEMA,
        'request_id': request_id,
        'version': version,
        'reason': reason,
        'requested_at_utc': stamp(utcnow()),
        'requested_by': requested_by,
    }
    write_private(request_path(paths, version), json.dumps(document, sort_keys=True).encode('utf-8'))
    return request_id


def _read_request(path):
    try:
        raw = read_nofollow(path, REQUEST_MAX_BYTES)
        document = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _delete(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _age_seconds(document, utcnow):
    moment = parse_stamp(document.get('requested_at_utc')) if isinstance(document, dict) else None
    if moment is None:
        return None
    try:
        return (utcnow() - moment).total_seconds()
    except (TypeError, ValueError):
        return None


def take_request(paths, version, *, utcnow):
    """Consume the request for `version`, or None when it is missing, stale or mismatching."""
    path = request_path(paths, version)
    if not path.exists() and not path.is_symlink():
        return None
    document = _read_request(path)
    _delete(path)
    if document is None:
        return None
    if document.get('schema_version') != REQUEST_SCHEMA or document.get('version') != version:
        return None
    request_id = document.get('request_id')
    if not isinstance(request_id, str) or len(request_id) != 32:
        return None
    age = _age_seconds(document, utcnow)
    if age is None or age > REQUEST_MAX_AGE_SECONDS:
        return None
    return document


def pending_request(paths, *, utcnow):
    """True when a request file younger than 120 s exists (a unit is about to start)."""
    directory = Path(paths.requests)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.startswith('request-') or not entry.name.endswith('.json'):
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        document = _read_request(entry)
        age = _age_seconds(document, utcnow)
        if age is not None and age <= REQUEST_PENDING_SECONDS:
            return True
    return False


def _last_line(text):
    lines = [line for line in str(text or '').splitlines() if line.strip()]
    return lines[-1] if lines else None


def start_unit(unit, *, runner):
    """Start `unit` without blocking; a failure is the requester's error."""
    result = runner(['systemctl', 'start', '--no-block', unit], timeout=30)
    if result.returncode != 0:
        raise UpdateError('resolve', 'unit_start_failed', _last_line(result.stderr))


def transaction_live(in_progress, *, boot_id, pid_alive):
    """Whether the recorded transaction is still running in this boot."""
    if not isinstance(in_progress, dict):
        return False
    if in_progress.get('boot_id') != boot_id:
        return False
    pid = in_progress.get('pid')
    if type(pid) is not int or pid <= 0:
        return False
    try:
        return bool(pid_alive(pid))
    except OSError:
        return False
