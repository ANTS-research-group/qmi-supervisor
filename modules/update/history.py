"""One append-only line per update transaction (public contract).

`history.jsonl` is a bounded log: one line is at most 2048 bytes, the file is
rotated to `history.jsonl.1` at 256 KiB, and no line ever carries a URL, a
password hash, a token or a long digit run.
"""
from pathlib import Path
import json
import os
import re

SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
LINE_MAX_BYTES = 2048
DETAIL_MAX_BYTES = 256
RESULTS = ('success', 'failed', 'rolled_back', 'interrupted')
REASONS = ('manual', 'auto', 'rollback', 'repair')
STEPS = ('resolve', 'download', 'verify', 'prepare', 'backup', 'snapshot',
         'install', 'restart', 'health', 'rollback')
CODES = (
    'config_unreadable', 'invalid_version', 'not_package_install', 'package_state_broken',
    'already_installed', 'unknown_version', 'no_package', 'no_checksums', 'unsigned',
    'prerelease_channel', 'downgrade_disabled', 'no_cached_package', 'no_rollback',
    'cache_unsafe', 'download_failed', 'download_too_large', 'download_size_mismatch',
    'keyring_missing', 'no_keyring', 'bad_signature', 'sums_invalid', 'checksum_missing',
    'checksum_mismatch', 'package_mismatch', 'package_incomplete', 'config_incompatible',
    'backup_failed', 'dpkg_busy', 'install_failed', 'install_timeout', 'health_failed',
    'modems_not_back', 'rollback_failed', 'interrupted', 'repair_failed', 'internal_error',
)
WITHHELD = 'detail withheld: it may contain a secret'
URL = re.compile(r'https?://\S+', re.IGNORECASE)
LONG_NUMBER = re.compile(r'[0-9]{14,}')
SECRET = re.compile(r'password|token|authorization', re.IGNORECASE)


def clean_detail(text):
    """Bounded, secret-free history detail; None stays None."""
    if text is None:
        return None
    cleaned = re.sub(r'\s+', ' ', str(text)).strip()
    cleaned = URL.sub('<url>', cleaned)
    cleaned = LONG_NUMBER.sub('<number>', cleaned)
    if '$' in cleaned or SECRET.search(cleaned):
        return WITHHELD
    return cleaned.encode('utf-8')[:DETAIL_MAX_BYTES].decode('utf-8', 'ignore')


def make_record(**fields):
    """A history record with the schema version, validated enums and a clean detail."""
    reason = fields.get('reason')
    if reason not in REASONS:
        raise ValueError(f'unknown history reason: {reason!r}')
    result = fields.get('result')
    if result not in RESULTS:
        raise ValueError(f'unknown history result: {result!r}')
    step_failed = fields.get('step_failed')
    if step_failed is not None and step_failed not in STEPS:
        raise ValueError(f'unknown history step: {step_failed!r}')
    code = fields.get('code')
    if code is not None and code not in CODES:
        raise ValueError(f'unknown history code: {code!r}')
    record = {'schema_version': SCHEMA_VERSION, **fields}
    record['detail'] = clean_detail(record.get('detail'))
    return record


def _dump(record):
    return (json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
            + '\n').encode('utf-8')


def _rotated(path):
    return path.with_name(path.name + '.1')


def append(paths, record):
    """Append one line: 0644 even under a restrictive umask, rotated before 256 KiB."""
    path = Path(paths.history)
    line = _dump(record)
    if len(line) > LINE_MAX_BYTES:
        line = _dump(dict(record, detail=None))
    try:
        current = path.stat().st_size
    except OSError:
        current = 0
    if current + len(line) > MAX_BYTES:
        try:
            os.replace(path, _rotated(path))
        except OSError:
            pass
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        os.fchmod(descriptor, 0o644)
        view = memoryview(line)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return line


def read(paths, limit=50):
    """The last `limit` history records, oldest first, across the rotated file."""
    path = Path(paths.history)
    records = []
    for candidate in (_rotated(path), path):
        try:
            with candidate.open('r', encoding='utf-8', errors='replace') as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            continue
    return records[-limit:]
