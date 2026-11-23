"""Bounded, codable updater failures (stdlib only, never imports GI).

Every step of a transaction can fail with an `UpdateError`: the step that was
running, one code of the published history table and an optional detail that
never carries a URL, a token or a password hash.
"""


class UpdateError(Exception):
    """A step failure with its code and a short, safe detail."""

    def __init__(self, step, code, detail=None):
        self.step = step
        self.code = code
        self.detail = detail
        message = f'{step}: {code}'
        if detail:
            message += f': {detail}'
        super().__init__(message)


class Interrupted(Exception):
    """The transaction must stop at `step` (SIGTERM, a stop flag)."""

    def __init__(self, step):
        self.step = step
        super().__init__(f'interrupted during {step}')


class LockBusy(RuntimeError):
    """Another update operation holds the single update lock."""
