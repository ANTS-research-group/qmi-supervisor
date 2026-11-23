"""Download, checksum, signature and package checks of the updater.

Nothing here imports GI. Every subprocess goes through the injected `runner`
and `command_env()`. Details never carry a URL: GitHub redirects asset
downloads to signed URLs that are temporary credentials.
"""
import hashlib
import http.client
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.error

from modules.update.commands import command_env
from modules.update.errors import UpdateError
from modules.update.transport import UpdateCheckError, test_mode

LOG = logging.getLogger(__name__)

PACKAGE = 'qmi-supervisor'
SUMS = 'SHA256SUMS'
SIGNATURE = 'SHA256SUMS.asc'
SUMS_MAX_BYTES = 64 * 1024
DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
DOWNLOAD_SECONDS = 600
VERSION_PATTERN = re.compile(r'[0-9][A-Za-z0-9.+~-]{0,63}')
REQUIRED_FILES = (
    'opt/qmi-supervisor/qmi_supervisor.py',
    'usr/local/sbin/qmi-supervisor-update',
    'etc/systemd/system/qmi-supervisor-update-install@.service',
    'etc/systemd/system/qmi-supervisor-update-rollback.service',
)
SUMS_LINE = re.compile(r'([0-9a-fA-F]{64}) [ *](\S+)')
FINGERPRINT = re.compile(r'[0-9A-F]{40}|[0-9A-F]{64}')
FAILURE_STATUS = {'BADSIG', 'ERRSIG', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG', 'NO_PUBKEY', 'NODATA'}
TEST_KEYRING_ENV = 'QMI_SUPERVISOR_UPDATE_TEST_KEYRING'

_warned_test_keyring = False


def deb_name(version):
    return f'qmi-supervisor_{version}_all.deb'


def sha256_file(path):
    """The lowercase hex SHA-256 of a file opened without following a symlink."""
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def parse_sums(text):
    """`{name: sha256}` from a SHA256SUMS file; every non-empty line must be valid."""
    if isinstance(text, bytes):
        if len(text) > SUMS_MAX_BYTES:
            raise UpdateError('verify', 'sums_invalid', 'the checksum file is too large')
        try:
            text = text.decode('utf-8')
        except UnicodeDecodeError:
            raise UpdateError('verify', 'sums_invalid', 'the checksum file is not text') from None
    if len(text.encode('utf-8')) > SUMS_MAX_BYTES:
        raise UpdateError('verify', 'sums_invalid', 'the checksum file is too large')
    entries = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = SUMS_LINE.fullmatch(line)
        if match is None:
            raise UpdateError('verify', 'sums_invalid', 'a line of the checksum file is malformed')
        digest, name = match.group(1).lower(), match.group(2)
        if name in entries:
            raise UpdateError('verify', 'sums_invalid', f'{name} appears twice in the checksum file')
        entries[name] = digest
    return entries


def _remove(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def download(open_url, url, target, *, step, name, max_bytes, expected_size, clock):
    """Stream `url` into `target` atomically; return `(sha256, size)`.

    A leftover part file is removed first. Reading stops at the first chunk
    that makes the file larger than `max_bytes` or than the published size.
    """
    target = Path(target)
    part = target.with_name('.' + target.name + '.part')
    _remove(part)
    digest = hashlib.sha256()
    total = 0
    started = clock()
    try:
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            with open_url(url, accept='application/octet-stream', timeout=30) as response:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes or (expected_size is not None and total > expected_size):
                        raise UpdateError(step, 'download_too_large',
                                          f'{name} is larger than allowed')
                    digest.update(chunk)
                    stream.write(chunk)
                    if clock() - started > DOWNLOAD_SECONDS:
                        raise UpdateError(step, 'download_failed',
                                          f'{name}: the download took longer than {DOWNLOAD_SECONDS} s')
            stream.flush()
            os.fsync(stream.fileno())
        if expected_size is not None and total != expected_size:
            raise UpdateError(step, 'download_size_mismatch',
                              f'{name}: expected {expected_size} bytes, got {total}')
        os.replace(part, target)
        return digest.hexdigest(), total
    except UpdateError:
        _remove(part)
        raise
    except UpdateCheckError as error:
        _remove(part)
        raise UpdateError(step, 'download_failed', f'{name}: {error.code}') from None
    except urllib.error.HTTPError as error:
        _remove(part)
        raise UpdateError(step, 'download_failed', f'{name}: HTTP {error.code}') from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError,
            ValueError) as error:
        _remove(part)
        raise UpdateError(step, 'download_failed', f'{name}: {type(error).__name__}') from None


def signature_fingerprint(returncode, stdout):
    """The primary-key fingerprint of a valid gpgv run, else None (stdout status lines only)."""
    words = [line.split()[1:] for line in stdout.splitlines() if line.startswith('[GNUPG:] ')]
    keywords = {word[0] for word in words if word}
    valid = [word for word in words if word and word[0] == 'VALIDSIG']
    if returncode != 0 or 'GOODSIG' not in keywords or not valid or keywords & FAILURE_STATUS:
        return None
    fingerprint = valid[0][-1].upper()          # last VALIDSIG field: primary key fingerprint
    return fingerprint if FINGERPRINT.fullmatch(fingerprint) else None


def verify_signature(sums, signature, keyring, *, runner, workdir):
    """Verify `signature` over `sums` with the shipped keyring; return the fingerprint."""
    keyring = Path(keyring)
    if not keyring.is_file():
        raise UpdateError('verify', 'keyring_missing',
                          f'the signing keyring {keyring.name} is missing')
    home = tempfile.mkdtemp(dir=workdir)
    try:
        result = runner(
            ['gpgv', '--homedir', home, '--status-fd', '1', '--keyring', str(keyring.resolve()),
             str(signature), str(sums)],
            timeout=60, env=command_env())
    finally:
        shutil.rmtree(home, ignore_errors=True)
    fingerprint = signature_fingerprint(result.returncode, result.stdout or '')
    if fingerprint is None:
        raise UpdateError('verify', 'bad_signature', 'the release signature is not valid')
    return fingerprint


def keyring_path(paths):
    """The shipped keyring, or the test keyring only under U2's test switch."""
    global _warned_test_keyring
    value = os.environ.get(TEST_KEYRING_ENV)
    if test_mode() and value and Path(value).is_absolute():
        if not _warned_test_keyring:
            LOG.warning('Using the TEST signing keyring %s (test mode)', value)
            _warned_test_keyring = True
        return Path(value)
    return Path(paths.keyring)


def package_fields(deb, *, runner):
    """`{Package, Version, Architecture}` from dpkg-deb; refuses an unreadable package."""
    result = runner(['dpkg-deb', '-f', str(deb), 'Package', 'Version', 'Architecture'],
                    timeout=60, env=command_env())
    if result.returncode != 0:
        raise UpdateError('verify', 'package_mismatch', 'the package could not be read')
    fields = {}
    for line in (result.stdout or '').splitlines():
        name, separator, value = line.partition(':')
        if separator:
            fields[name.strip()] = value.strip()
    return fields


def check_package(deb, version, *, runner, workdir, python, config, preflight=True):
    """Package identity, the updater files and the target's own configuration preflight."""
    fields = package_fields(deb, runner=runner)
    if (fields.get('Package') != PACKAGE or fields.get('Version') != version
            or fields.get('Architecture') != 'all'):
        found = ' '.join(str(fields.get(key)) for key in ('Package', 'Version', 'Architecture'))
        raise UpdateError('verify', 'package_mismatch', f'found {found}')
    tree = Path(tempfile.mkdtemp(dir=workdir))
    try:
        result = runner(['dpkg-deb', '-x', str(deb), str(tree)], timeout=120, env=command_env())
        if result.returncode != 0:
            raise UpdateError('verify', 'package_mismatch', 'the package could not be extracted')
        for name in REQUIRED_FILES:
            if not (tree / name).exists():
                raise UpdateError('verify', 'package_incomplete', f'{name} is missing')
        if preflight and Path(config).exists():
            script = tree / 'opt/qmi-supervisor/qmi_supervisor.py'
            result = runner([str(python), '-B', '-E', '-s', str(script), '--check-config',
                             '--config', str(config)],
                            timeout=60, cwd=tree / 'opt/qmi-supervisor', env=command_env())
            if result.returncode != 0:
                lines = [line for line in (result.stderr or '').splitlines() if line.strip()]
                detail = lines[-1] if lines else 'the configuration check failed'
                prefix = 'qmi_supervisor.py: '
                if detail.startswith(prefix):
                    detail = detail[len(prefix):]
                raise UpdateError('verify', 'config_incompatible', detail)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
