"""Automatic installation policy: newer stable, signed releases inside the window.

The policy is deliberately conservative and never loops: only the stable
channel, only when signatures are required, only when a package of the
installed version is kept so the update can be undone, and never a version
that already failed automatically. The decision is stored in `state.auto` for
the web interface and the journal, and only `started` starts a unit.
"""
from datetime import datetime
import functools
import logging

from modules.runtime import version as runtime_version
from modules.update import history
from modules.update import state as state_module
from modules.update.commands import dpkg_status
from modules.update.install import cached_package, find_release, resolve, release_version
from modules.update import units as units_module

LOG = logging.getLogger(__name__)

REASON_STARTED = 'started'
FAILED_RESULTS = ('failed', 'rolled_back', 'interrupted')


def window_bounds(update):
    """`(start, end)` as `datetime.time` from the validated `HH:MM` settings."""
    start = datetime.strptime(update.window_start, '%H:%M').time()
    end = datetime.strptime(update.window_end, '%H:%M').time()
    return start, end


def _time_of(value):
    if isinstance(value, datetime):
        return value.time()
    return value


def in_window(now, start, end):
    """Whether a local time falls in the window; a window may cross midnight."""
    moment = _time_of(now)
    if start < end:
        return start <= moment < end
    if start > end:
        return moment >= start or moment < end
    return False


def _candidates(state_value, installed):
    """Eligible releases newer than `installed`, highest first."""
    if not installed:
        return []
    found = []
    for release in state_value.get('releases') or []:
        if not isinstance(release, dict):
            continue
        if release.get('prerelease') or not release.get('installable'):
            continue
        signature = release.get('signature') or {}
        if not signature.get('url'):
            continue
        version = release_version(release)
        if not version:
            continue
        try:
            if runtime_version.compare_versions(version, installed) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        found.append(version)
    found.sort(key=functools.cmp_to_key(runtime_version.compare_versions), reverse=True)
    return found


def _failed_before(version, history_lines):
    return any(isinstance(line, dict) and line.get('reason') == 'auto'
               and line.get('result') in FAILED_RESULTS and line.get('to') == version
               for line in history_lines or [])


def auto_decision(*, settings, state, installed, history_lines, rollback_ok, in_progress, now):
    """The first automatic-mode reason that applies and the candidate version."""
    update = getattr(settings, 'update', settings)
    if update.mode != 'auto':
        return 'mode_not_auto', None
    if update.channel != 'stable':
        return 'channel_not_stable', None
    if not update.require_signature:
        return 'signature_not_required', None
    if in_progress:
        return 'in_progress', None
    candidates = _candidates(state, installed)
    if not candidates:
        return 'no_newer_release', None
    highest = candidates[0]
    if _failed_before(highest, history_lines):
        return 'failed_before', highest
    if not rollback_ok:
        return 'no_rollback_package', highest
    start, end = window_bounds(update)
    if not in_window(now, start, end):
        return 'outside_window', highest
    return 'started', highest


def rollback_package_kept(paths, state_value, installed_version):
    """Whether the installed version can be reinstalled if the update is unhealthy."""
    if not installed_version:
        return False
    if cached_package(paths, installed_version) is not None:
        return True
    release = find_release(state_value, installed_version)
    return bool(isinstance(release, dict) and release.get('installable'))


def run_auto(paths, settings, state_value, *, runner, now_local, utcnow, log):
    """Record `state.auto` and, when the policy says `started`, start the unit."""
    update = getattr(settings, 'update', settings)
    installed = dpkg_status(runner) if update.mode == 'auto' else None
    installed_version = installed[1] if installed else None
    lines = []
    if update.mode == 'auto':
        try:
            lines = history.read(paths, limit=50)
        except OSError:
            lines = []
    reason, version = auto_decision(
        settings=settings, state=state_value, installed=installed_version,
        history_lines=lines,
        rollback_ok=rollback_package_kept(paths, state_value, installed_version),
        in_progress=bool(state_value.get('in_progress')), now=now_local)
    window = f'{update.window_start}–{update.window_end}'
    previous = state_value.get('auto')
    previous = previous if isinstance(previous, dict) else {}
    state_value['auto'] = {
        'checked_at_utc': state_module.stamp(utcnow()),
        'reason': reason, 'version': version, 'window': window,
    }
    try:
        state_module.write_state(paths.state, state_value, now=utcnow())
    except OSError as error:
        log.error('Cannot write the update state: %s', error)

    if reason != REASON_STARTED:
        if (previous.get('reason'), previous.get('version')) != (reason, version):
            log.info('Automatic update: %s%s', reason,
                     f' ({version})' if version else '')
        else:
            log.debug('Automatic update: %s%s', reason,
                      f' ({version})' if version else '')
        return reason, version

    try:
        resolve(paths, settings, state_value, version, reason='auto', installed=installed)
    except Exception as error:  # noqa: BLE001 - a refused candidate must not start a unit
        code = getattr(error, 'code', type(error).__name__)
        log.warning('Cannot start the automatic update to %s: %s', version, code)
        return _not_started(paths, state_value, version, code, utcnow, log), version
    try:
        units_module.write_request(paths, version, reason='auto', requested_by='timer',
                                   utcnow=utcnow)
        units_module.start_unit(units_module.install_unit(version), runner=runner)
    except Exception as error:  # noqa: BLE001 - requester errors are logged, never raised
        code = getattr(error, 'code', type(error).__name__)
        try:
            units_module.request_path(paths, version).unlink(missing_ok=True)
        except OSError:
            pass
        log.warning('Cannot start the automatic update to %s: %s', version, code)
        return _not_started(paths, state_value, version, code, utcnow, log), version
    log.info('Automatic update to %s started', version)
    return reason, version


def _not_started(paths, state_value, version, reason, utcnow, log):
    """Record why an automatic update that the policy accepted did not start."""
    auto = dict(state_value.get('auto') or {})
    auto.update({'reason': reason, 'version': version})
    state_value['auto'] = auto
    try:
        state_module.write_state(paths.state, state_value, now=utcnow())
    except OSError as error:
        log.error('Cannot write the update state: %s', error)
    return reason
