"""The single update lock shared by every writer of the updater files.

Installs wait up to 120 s; checks do not wait at all. Closing the descriptor
releases the flock even when the process dies.
"""
from contextlib import contextmanager
import fcntl
import os
import time

from modules.update.errors import LockBusy


@contextmanager
def update_lock(path, *, wait_seconds=0, clock=time.monotonic, sleep=time.sleep):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = clock() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise LockBusy('Another update operation holds ' + str(path)) from None
                sleep(1)
        yield
    finally:
        os.close(descriptor)   # closing releases the lock
