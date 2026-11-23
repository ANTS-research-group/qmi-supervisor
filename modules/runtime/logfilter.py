"""Fold repeated identical journal lines while a failure streak is active."""
import logging

from modules.runtime.diagnosis import LOG_FIRST_ATTEMPTS, STREAK_ACTIVE


class RepeatLimiter(logging.Filter):
    """Let the first lines of each template through, then fold the rest."""

    def __init__(self, first=LOG_FIRST_ATTEMPTS):
        super().__init__()
        self.first = first
        self._active = False
        self._counts = {}
        self._suppressed = {}

    def filter(self, record):
        if not STREAK_ACTIVE.is_set():
            if self._active:
                self.reset()
            return True
        if not self._active:
            self._counts = {}
            self._suppressed = {}
            self._active = True
        if getattr(record, 'qmi_diagnosis', False) or record.levelno >= logging.ERROR:
            return True
        key = self._key(record)
        if key is None:
            return True
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count <= self.first:
            return True
        self._suppressed[key] = self._suppressed.get(key, 0) + 1
        return False

    def _key(self, record):
        # The template, not the rendered text, so counters inside arguments
        # cannot defeat folding.
        try:
            key = (record.name, record.levelno, record.msg)
            hash(key)
        except TypeError:
            return None
        return key

    def reset(self):
        self._active = False
        self._counts = {}
        self._suppressed = {}

    def drain(self):
        result = {str(key[2]): count for key, count in self._suppressed.items() if count}
        self.reset()
        return result


def install(first=LOG_FIRST_ATTEMPTS, logger=None):
    """Attach one limiter to every root handler and return it."""
    limiter = RepeatLimiter(first=first)
    for handler in (logger or logging.getLogger()).handlers:
        handler.addFilter(limiter)
    return limiter
