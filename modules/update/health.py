"""Health signals and waits of an update transaction.

Every freshness comparison uses `time.monotonic()`: Linux CLOCK_MONOTONIC is
shared by every process of a boot, and NTP may step the wall clock when a
modem reconnects. `modem.json` carries identity data, so only `phase`,
`worker_id` and `heartbeat_monotonic` are read from it.
"""
import time

from modules.runtime.fleet_config import valid_profile
from modules.runtime.ipc import read_json
from modules.update.errors import Interrupted

FRESH_SECONDS = 15
SERVICE_SECONDS = 180
STABLE_SECONDS = 30
MODEMS_SECONDS = 600
POLL_SECONDS = 5
SERVICE = 'qmi-supervisor.service'


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def heartbeat(path, now, *, after=None):
    """Phase, worker id and monotonic beat of a fresh heartbeat file, else None."""
    value = read_json(path)
    if not isinstance(value, dict):
        return None
    beat = value.get('heartbeat_monotonic')
    if not _number(beat) or not 0 <= now - beat <= FRESH_SECONDS:
        return None
    if after is not None and not beat > after:
        return None
    return {'phase': value.get('phase'), 'worker_id': value.get('worker_id'), 'monotonic': beat}


def online_profiles(runtime, now):
    """`{profile: worker_id}` for every modem whose worker is fresh and ONLINE."""
    found = {}
    modems = runtime / 'modems'
    try:
        entries = list(modems.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not valid_profile(entry.name):
            continue
        value = heartbeat(entry / 'modem.json', now)
        if value is not None and value['phase'] == 'ONLINE':
            found[entry.name] = value['worker_id']
    return found


def web_running(runtime, now, *, after=None):
    value = heartbeat(runtime / 'web.json', now, after=after)
    return value is not None and value['phase'] == 'RUNNING'


def status_published(runtime, after):
    value = read_json(runtime / 'status.json')
    if not isinstance(value, dict):
        return False
    published = value.get('published_monotonic')
    return _number(published) and published > after


def running_identity(runtime):
    value = read_json(runtime / 'version.json')
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        return None
    return value


def service_show(runner):
    """`{ActiveState, SubState, MainPID, NRestarts}` from systemd."""
    result = runner(['systemctl', 'show', SERVICE, '-p', 'ActiveState', '-p', 'SubState',
                     '-p', 'MainPID', '-p', 'NRestarts'], timeout=30)
    values = {}
    for line in (result.stdout or '').splitlines():
        name, separator, value = line.partition('=')
        if separator:
            values[name.strip()] = value.strip()
    return values


def evaluate(runtime, runner, now, *, target, after, web_required):
    """`(None, None)` when healthy now, else the first failing stage and its detail."""
    show = service_show(runner)
    active = show.get('ActiveState')
    if active != 'active':
        return 'restart', f"service {active}/{show.get('SubState')}"
    identity = running_identity(runtime)
    version = identity.get('supervisor_version') if identity else None
    started = identity.get('started_monotonic') if identity else None
    if (identity is None or version != target or str(identity.get('pid')) != show.get('MainPID')
            or not _number(started) or started <= after):
        return 'restart', f'running version {version or "unknown"}'
    if web_required and not (web_running(runtime, now, after=after)
                             and status_published(runtime, after)):
        return 'health', 'web worker not running'
    return None, None


def wait_service(runtime, runner, *, target, after, web_required, clock=time.monotonic,
                 sleep=time.sleep, stop=None, on_stage=None, deadline_from=None):
    """Wait until the restarted service is healthy and stays healthy for 30 s.

    `after` is when the transaction started; a service started later counts as
    the new one. The time budget starts at `deadline_from` (when apt-get
    finished), because the package's postinst restarts the service inside apt.
    Conditions must first hold within 180 s of that base; then MainPID and
    NRestarts must not change and every condition must stay true for 30 s.
    """
    base = after if deadline_from is None else deadline_from
    first_deadline = base + SERVICE_SECONDS
    hard_deadline = base + SERVICE_SECONDS + STABLE_SECONDS
    seen_healthy_at = None
    stable_since = marker = None
    stage, last_detail, moved = 'restart', None, False
    while True:
        if stop is not None and stop.is_set():
            raise Interrupted(stage)
        now = clock()
        found, detail = evaluate(runtime, runner, now, target=target, after=after,
                                 web_required=web_required)
        if found is None:
            if not moved:
                # The service part passed: the remaining work is the health window.
                moved = True
                if on_stage is not None:
                    on_stage('health')
            if seen_healthy_at is None:
                if now > first_deadline:
                    return 'health', 'health_failed', 'the new version did not become healthy in time'
                seen_healthy_at = now
            show = service_show(runner)
            current = (show.get('MainPID'), show.get('NRestarts'))
            if stable_since is None or current != marker:
                stable_since, marker = now, current
        else:
            stage, last_detail = found, detail
            stable_since = None
            if found != 'restart' and not moved:
                moved = True
                if on_stage is not None:
                    on_stage('health')
            if seen_healthy_at is None and now > first_deadline:
                return found, 'health_failed', detail
        if seen_healthy_at is not None and stable_since is not None and now - stable_since >= STABLE_SECONDS:
            return None
        if now > hard_deadline:
            if seen_healthy_at is None:
                return 'health', 'health_failed', last_detail or 'the new version did not become healthy in time'
            return 'restart', 'health_failed', last_detail or 'the service did not stay healthy'
        sleep(POLL_SECONDS)
        if stop is not None and stop.is_set():
            raise Interrupted(stage)


def wait_modems(runtime, before, *, deadline, clock=time.monotonic, sleep=time.sleep, stop=None):
    """Profiles still missing: a fresh, ONLINE heartbeat with a new worker id."""
    before = dict(before)
    while True:
        if stop is not None and stop.is_set():
            raise Interrupted('health')
        now = clock()
        current = online_profiles(runtime, now)
        missing = [profile for profile in sorted(before)
                   if profile not in current or current[profile] == before[profile]]
        if not missing:
            return []
        if now >= deadline:
            return missing
        sleep(POLL_SECONDS)
        if stop is not None and stop.is_set():
            raise Interrupted('health')
