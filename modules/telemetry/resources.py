"""Small Linux process sampler. Call from an existing observer worker, not QMI."""
from datetime import datetime, timezone
import os
from pathlib import Path
import time


def read_boot_id(proc_root="/proc"):
    """A missing identity must never be presented as a confirmed current boot."""
    try:
        return (Path(proc_root) / "sys/kernel/random/boot_id").read_text().strip() or None
    except (OSError, UnicodeError):
        return None


class ProcessSampler:
    """CPU averages use one core = 100%; RSS is the kernel's inexpensive estimate.

    Targets are explicit PIDs or owned Popen objects; there is no process scan.
    No worker is created here. All state belongs to the caller's observer worker.
    """
    def __init__(self, *, proc_root="/proc", clock=time.monotonic, utcnow=None,
                 clock_ticks=None, page_size=None):
        self.proc_root, self.clock = Path(proc_root), clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.clock_ticks = clock_ticks if clock_ticks is not None else os.sysconf("SC_CLK_TCK")
        self.page_size = page_size if page_size is not None else os.sysconf("SC_PAGE_SIZE")
        if self.clock_ticks <= 0 or self.page_size <= 0:
            raise ValueError("Process accounting units must be positive")
        self.boot_id = read_boot_id(proc_root)
        self._previous = {}
        self._owned = {}
        self._last_sample = None

    def _read(self, pid):
        content = (self.proc_root / str(pid) / "stat").read_text()
        prefix, separator, tail = content.rpartition(")")
        # comm can contain spaces, ')' and newlines. Numeric fields follow its
        # final closing parenthesis; splitting the whole line misindexes them.
        if not separator or "(" not in prefix or int(prefix.split("(", 1)[0]) != pid:
            raise ValueError("Malformed process accounting record")
        fields = tail.split()
        if len(fields) < 22:
            raise ValueError("Incomplete process accounting record")
        ticks = int(fields[11]) + int(fields[12])  # fields 14/15, excluding child CPU.
        starttime, rss_pages = int(fields[19]), int(fields[21])
        if ticks < 0 or starttime < 0 or rss_pages < 0:
            raise ValueError("Invalid process accounting counters")
        return fields[0], ticks, starttime, rss_pages * self.page_size

    def _process(self, role, target):
        result = dict(pid=None, starttime_ticks=None, state="disabled", cpu_percent=None,
                      rss_bytes=None, cpu_interval_seconds=None)
        if target is None:
            self._previous.pop(role, None)
            self._owned.pop(role, None)
            return result
        owned = not isinstance(target, int) or isinstance(target, bool)
        try:
            pid = target.pid if owned else target
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise ValueError("Invalid process PID")
            result["pid"] = pid
            if owned and target.poll() is not None:
                result["state"] = "exited"
                self._previous.pop(role, None)
                return result
            before = self.clock()
            process_state, ticks, starttime, rss = self._read(pid)
            observed = (before + self.clock()) / 2
            identity = self.boot_id, pid, starttime
            if owned:
                previous_owner = self._owned.get(role)
                if previous_owner and previous_owner[0] is target and previous_owner[1] != identity:
                    raise ValueError("Owned process PID was reused")
                if target.poll() is not None:
                    result["state"] = "exited"
                    self._previous.pop(role, None)
                    return result
                self._owned[role] = target, identity
            else:
                self._owned.pop(role, None)
            result.update(starttime_ticks=starttime, rss_bytes=rss, state="warming_up")
            if process_state in {"Z", "X", "x"}:
                result.update(state="exited", rss_bytes=None)
                self._previous.pop(role, None)
                return result
            previous = self._previous.get(role)
            if previous and previous[0] == identity:
                elapsed, delta = observed - previous[2], ticks - previous[1]
                if elapsed > 0 and delta >= 0:
                    result.update(state="ok", cpu_percent=100 * delta / self.clock_ticks / elapsed,
                                  cpu_interval_seconds=elapsed)
            self._previous[role] = identity, ticks, observed
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError) as error:
            self._previous.pop(role, None)
            result.update(state="unavailable", cpu_percent=None, rss_bytes=None, error=str(error))
        return result

    def sample(self, targets):
        now = self.clock()
        interval = now - self._last_sample if self._last_sample is not None else None
        self._last_sample = now
        roles = ('supervisor', 'web') + tuple(role for role in ('manager', 'modem', 'telemetry') if role in targets)
        records = {role: self._process(role, targets.get(role)) for role in roles}
        return dict(sampled_at_utc=self.utcnow().isoformat(), sampled_monotonic=now,
                    interval_seconds=interval, **records)
