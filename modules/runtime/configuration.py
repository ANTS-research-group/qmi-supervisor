"""Cross-process configuration transactions shared by the API and installer."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import tempfile


@contextmanager
def configuration_lock(path):
    path = Path(path).resolve()
    if path == Path('/etc/qmi-supervisor.ini'):
        lock = Path('/run/qmi-supervisor/config.lock')
    else:
        directory = Path(tempfile.gettempdir()) / f'qmi-supervisor-locks-{os.getuid()}'
        directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise ValueError('Configuration lock directory is not private')
        lock = directory / (hashlib.sha256(str(path).encode()).hexdigest() + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
