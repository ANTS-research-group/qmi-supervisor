"""The optional access token for a private release repository.

The token lives outside every configuration and the state file, in
/etc/qmi-supervisor-update-token (root, mode 0600). It is never printed,
logged or stored in state.json; error messages never include its content.
"""
import os
from pathlib import Path
import re
import stat
import tempfile

TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_]{20,255}\Z')


class TokenFileError(Exception):
    """The token file is missing in a way that is not plain absence, or unsafe to use."""


def read_token(path):
    """The stored token, None when no file exists; TokenFileError when it is unsafe or invalid."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TokenFileError(f'Cannot read the token file: {error.strerror or error}') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TokenFileError('The token file is not a regular file')
        if info.st_uid != os.geteuid():
            raise TokenFileError('The token file is not owned by the current user')
        if info.st_mode & 0o077:
            raise TokenFileError('The token file is readable or writable by other users')
        if info.st_size > 1024:
            raise TokenFileError('The token file is too large')
        content = os.read(descriptor, 1024)
    except OSError as error:
        raise TokenFileError(f'Cannot read the token file: {error.strerror or error}') from None
    finally:
        os.close(descriptor)
    try:
        token = content.decode('utf-8').strip()
    except UnicodeDecodeError:
        raise TokenFileError('The token file is not valid UTF-8') from None
    if not TOKEN_PATTERN.fullmatch(token):
        raise TokenFileError('The token file does not contain a valid access token')
    return token


def write_token(path, token):
    """Atomically write a validated token, root 0600 (mkstemp's default)."""
    path = Path(path)
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError('invalid access token')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(token + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def remove_token(path):
    """Unlink the token file; True when one was removed."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True
