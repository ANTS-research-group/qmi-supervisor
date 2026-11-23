"""Private updater directories and files: 0700 directories, 0600 files.

Every path is opened with O_NOFOLLOW; a symlink planted in the cache, the
backups or the request directory is refused instead of followed.
"""
import os
from pathlib import Path
import stat
import tempfile

from modules.update.errors import UpdateError

_STEP_FOR_CODE = {'cache_unsafe': 'download', 'backup_failed': 'backup'}


def _not_private(path):
    return f'{Path(path).name} is not a private directory'


def ensure_private_dir(path, *, code, step=None):
    """Create `path` 0700 (parents 0755) and require a private, real directory."""
    path = Path(path)
    step = step or _STEP_FOR_CODE.get(code, 'download')
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            path.mkdir(mode=0o700)
        metadata = os.lstat(path)
    except OSError as error:
        raise UpdateError(step, code, _not_private(path)) from error
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise UpdateError(step, code, _not_private(path))
    return path


def write_private(path, data, mode=0o600):
    """Atomically replace `path` with `data` (bytes) and the given mode."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_nofollow(path, limit):
    """Read at most `limit` bytes of a regular file without following a symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('not a regular file')
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b''.join(chunks)
    if len(data) > limit:
        raise ValueError(f'file is larger than {limit} bytes')
    return data
