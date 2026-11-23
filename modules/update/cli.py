"""The qmi-supervisor-update command line (stdlib only, metadata never printed).

Commands: check [--auto], list [--json], status [--json], set-token,
clear-token, install VERSION and rollback. `check --auto` is what the systemd
timer runs: it stays quiet when it skips and exits 0 after recording a network
failure, so the unit does not flap to `failed` while a host is offline. A manual
`check` always prints and exits 1 on failure. `install` and `rollback` are
requesters unless `--from-unit` is given: then they run inside their own
systemd unit, hold the update lock and perform the transaction.
"""
import argparse
from dataclasses import asdict, replace
from datetime import timedelta
import getpass
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from modules.runtime import configuration, ipc  # noqa: F401 - eager imports, see below
from modules.runtime import version as runtime_version
from modules.runtime.settings import Settings, load_settings
from modules.telemetry import resources
from modules.update import (  # noqa: F401 - every module is imported before apt replaces them
    auto, commands, files, health, history, install, lock, releases, state, transport, units,
    verify,
)
from modules.update.credentials import TokenFileError, read_token, remove_token, write_token
from modules.update.errors import LockBusy, UpdateError
from modules.update.state import UpdatePaths

LOG = logging.getLogger(__name__)

FETCH_TIMEOUT = 10.0
CHECK_MAX_SECONDS = 70 * 60
WAIT_POLL_SECONDS = 2
WAIT_STOP_SECONDS = 10

# English texts of the history codes (the web interface translates the same IDs).
CODE_TEXT = {
    'config_unreadable': 'The current configuration cannot be read; fix it first.',
    'invalid_version': 'The requested version is not valid.',
    'not_package_install': 'This installation was not made with the Debian package.',
    'package_state_broken': 'The installed package is not completely installed.',
    'already_installed': 'This version is already installed.',
    'unknown_version': 'This version is not in the list of releases. Check again.',
    'no_package': 'The release has no package.',
    'no_checksums': 'The release has no checksum file.',
    'unsigned': 'The release is not signed.',
    'prerelease_channel': 'Pre-releases are not enabled',
    'downgrade_disabled': 'Installing older versions is turned off.',
    'no_cached_package': 'The package of that version is no longer stored on this device.',
    'no_rollback': 'No previous version is kept.',
    'cache_unsafe': 'The download folder is not private.',
    'download_failed': 'The package could not be downloaded.',
    'download_too_large': 'The download is larger than allowed.',
    'download_size_mismatch': 'The downloaded file does not have the published size.',
    'keyring_missing': 'The release signing key is not installed.',
    'no_keyring': 'This device has no release signing key, so it cannot verify releases. Install a package that includes /opt/qmi-supervisor/keys/release-signing.gpg.',
    'bad_signature': 'The release signature is not valid.',
    'sums_invalid': 'The checksum file is not valid.',
    'checksum_missing': 'The checksum file does not list this package.',
    'checksum_mismatch': 'The downloaded package does not match its checksum.',
    'package_mismatch': 'The downloaded package is not the expected version.',
    'package_incomplete': 'That version does not include the updater and cannot be installed from here.',
    'config_incompatible': 'The current configuration is not valid for that version.',
    'backup_failed': 'The configuration could not be saved.',
    'dpkg_busy': 'Another package installation is running; try again later.',
    'install_failed': 'The package could not be installed.',
    'install_timeout': 'The package installation took too long.',
    'health_failed': 'The new version did not start correctly in time.',
    'modems_not_back': 'A modem that was online did not come back online in time.',
    'rollback_failed': 'The previous version could not be restored.',
    'interrupted': 'The updater stopped before finishing.',
    'repair_failed': 'The package could not be repaired automatically.',
    'internal_error': 'Unexpected updater error.',
    'in_progress': 'An update is already running.',
    'request_pending': 'Another update request is starting.',
    'unit_start_failed': 'The updater is not available on this device.',
    'downgrade_not_confirmed': 'Confirm the installation of an older version.',
    'request_failed': 'The update request could not be written.',
}



def emit(text, *, warning=False):
    """Print one line; journal priority warning when running under systemd."""
    if warning and os.environ.get('JOURNAL_STREAM'):
        text = '<4>' + text
    print(text, file=sys.stderr if warning else sys.stdout, flush=True)


def _is_root():
    """Tests patch this function, never os.geteuid (which uninstall tests also patch)."""
    return os.geteuid() == 0


def read_update_settings(path):
    """(UpdateSettings, None) or (None, error) without ever printing the INI."""
    path = Path(path)
    try:
        if not path.exists():
            return Settings().update, None
        return load_settings(path).update, None
    except ValueError as error:
        return None, str(error)


def due(state_value, update, now):
    """Whether a periodic check is due now."""
    last = state.parse_stamp(state_value.get('last_success_utc'))
    if last is None:
        return True
    if state_value.get('source') != update.source:
        return True
    if last > now + timedelta(minutes=10):
        return True
    return now >= last + timedelta(hours=update.check_interval_hours) - timedelta(minutes=10)


def _reason_for(releases_list, newest_release):
    for release in releases_list:
        deb = release.get('deb') or {}
        if release.get('tag') == newest_release.get('tag') and deb.get('version') == newest_release.get('version'):
            return release.get('reason') or 'unknown'
    return 'unknown'


def _summary(current_state, current):
    newest_available = current_state.get('newest_available')
    newest_installable = current_state.get('newest_installable')
    if newest_installable is not None and releases.is_newer(newest_installable['version'], current):
        return (f'Update check: installed {current}; update available: '
                f'{newest_installable["tag"]} ({newest_installable["version"]}).')
    if newest_available is not None and releases.is_newer(newest_available['version'], current):
        reason = _reason_for(current_state.get('releases', []), newest_available)
        return (f'Update check: installed {current}; {newest_available["tag"]} '
                f'({newest_available["version"]}) is newer but not installable ({reason}).')
    channel = current_state.get('channel') or 'stable'
    label = 'stable or pre-release' if channel == 'prerelease' else 'stable'
    return f'Update check: installed {current}; no newer {label} release.'


def _fail(paths, current_state, update, current, now, auto, code, detail, status):
    previous = current_state.get('check_error')
    same = isinstance(previous, dict) and previous.get('code') == code
    if same:
        check_error = dict(previous)
        check_error['attempts'] = check_error.get('attempts', 1) + 1
        check_error['detail'] = transport.bounded(detail)
        check_error['status'] = status
    else:
        check_error = {
            'code': code,
            'detail': transport.bounded(detail),
            'status': status,
            'since_utc': state.stamp(now),
            'attempts': 1,
        }
    current_state['check_error'] = check_error
    state.refresh_derived(current_state, current_version=current, channel=update.channel,
                          require_signature=update.require_signature)
    state.write_state(paths.state, current_state, now=now)
    if not (auto and same):
        emit(f'Update check failed ({code}): {transport.bounded(detail)}', warning=True)
    return 0 if auto else 1


def _check_locked(paths, update, *, auto, fetch, now):
    current = runtime_version.installed_version()
    current_state = state.load_state(paths.state) or state.new_state(current, now)

    if auto and not due(current_state, update, now):
        if (current_state.get('current_version'), current_state.get('channel'),
                current_state.get('require_signature')) != (current, update.channel, update.require_signature):
            state.refresh_derived(current_state, current_version=current, channel=update.channel,
                                  require_signature=update.require_signature)
            state.write_state(paths.state, current_state, now=now)
        return 0

    etag = None
    if current_state.get('source') == update.source and current_state.get('releases'):
        etag = current_state.get('etag')
    else:
        current_state['releases'] = []
        current_state['etag'] = None
    current_state['source'] = update.source
    current_state['checked_utc'] = state.stamp(now)

    try:
        token = read_token(paths.token)
    except TokenFileError as error:
        return _fail(paths, current_state, update, current, now, auto, 'bad_token_file', str(error), None)

    try:
        result = fetch(update.source, token=token, etag=etag, timeout=FETCH_TIMEOUT)
    except transport.UpdateCheckError as error:
        return _fail(paths, current_state, update, current, now, auto, error.code, error.detail, error.status)

    if not result.not_modified:
        current_state['releases'] = releases.parse_releases(result.items,
                                                            require_signature=update.require_signature)
        current_state['etag'] = result.etag
    current_state['last_success_utc'] = state.stamp(now)
    current_state['check_error'] = None
    state.refresh_derived(current_state, current_version=current, channel=update.channel,
                          require_signature=update.require_signature)
    state.write_state(paths.state, current_state, now=now)
    emit(_summary(current_state, current))
    return 0


def run_check(paths, *, automatic, fetch, now, runner=None, monotonic=None, sleep=None,
              now_local=None):
    update, error = read_update_settings(paths.config)
    if update is None:
        emit(f'Cannot use the configuration: {error}; run with sudo if it is a permission problem.', warning=True)
        return 1
    runner = runner or commands.run_command
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    now_value = now() if callable(now) else now
    utcnow = now if callable(now) else (lambda: now_value)
    try:
        # The lock now lives in /run; validate both directories before any network work.
        state.ensure_state_dir(paths.state_dir)
        Path(paths.lock).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with lock.update_lock(paths.lock, wait_seconds=0, clock=monotonic, sleep=sleep):
            if automatic and update.mode == 'off':
                # No periodic network work, but a dead transaction must still be
                # recorded and repaired, as the plan promises at the next check.
                document = state.load_state(paths.state)
                if document is None:
                    return 0
                install.reconcile_interrupted(paths, document, runner=runner, clock=monotonic,
                                              utcnow=utcnow, log=LOG)
                document = state.load_state(paths.state) or document
                install.start_repair_if_needed(paths, document, runner=runner, utcnow=utcnow,
                                               log=LOG)
                return 0
            document = state.load_state(paths.state) or state.new_state(
                runtime_version.installed_version(), now_value)
            install.reconcile_interrupted(paths, document, runner=runner, clock=monotonic,
                                          utcnow=utcnow, log=LOG)
            result = _check_locked(paths, update, auto=automatic, fetch=fetch, now=now_value)
            document = state.load_state(paths.state) or document
            installed = runtime_version.installed_version()
            document['rollback_ready'] = install.rollback_ready(paths, document, installed)
            try:
                state.write_state(paths.state, document, now=now_value)
            except OSError as write_error:
                LOG.error('Cannot write the update state: %s', write_error)
            install.start_repair_if_needed(paths, document, runner=runner, utcnow=utcnow,
                                           log=LOG)
            if automatic:
                auto.run_auto(paths, update, document, runner=runner,
                              now_local=now_local or now_value.astimezone(),
                              utcnow=utcnow, log=LOG)
            return result
    except LockBusy:
        LOG.info('An update operation is running; check skipped')
        return 0
    except (OSError, ValueError) as error:
        # A symlinked state directory raises ValueError from ensure_state_dir.
        detail = getattr(error, 'strerror', None) or str(error)
        emit(f'Cannot write {paths.state_dir}: {detail}; run with sudo.', warning=True)
        return 1


def _reason_text(reason):
    return releases.REASON_TEXT.get(reason, str(reason))


def run_list(paths, *, json_output):
    current = runtime_version.installed_version()
    document = state.load_state(paths.state)
    items = document.get('releases', []) if document else []
    if json_output:
        emit(json.dumps(items, ensure_ascii=False))
        return 0
    if document is None:
        emit('No check has been made yet.')
        return 0
    newest_installable = document.get('newest_installable') or {}
    rows = []
    for release in items:
        deb = release.get('deb') or {}
        version = deb.get('version') or '-'
        published = (release.get('published_utc') or '')[:10] or '-'
        kind = 'pre-release' if release.get('prerelease') else 'stable'
        if release.get('installable'):
            status_text = 'installable'
        else:
            status_text = 'not installable: ' + _reason_text(release.get('reason'))
        if deb.get('version') == current:
            status_text += ' (installed)'
        if (newest_installable and release.get('tag') == newest_installable.get('tag')
                and deb.get('version') == newest_installable.get('version')):
            status_text += ' (newest)'
        rows.append((release.get('tag', ''), version, published, kind, status_text))
    headers = ('TAG', 'PACKAGE VERSION', 'PUBLISHED', 'KIND', 'STATUS')
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    emit('  '.join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip())
    for row in rows:
        emit('  '.join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())
    return 0


def _check_error_text(check_error):
    code = check_error.get('code')
    text = transport.ERROR_TEXT.get(code, str(code))
    if '{status}' in text:
        text = text.replace('{status}', str(check_error.get('status')))
    return (f'{code}: {text}, {check_error.get("attempts")} attempts '
            f'since {check_error.get("since_utc")}')


def run_status(paths, *, json_output, clock):
    current = runtime_version.installed_version()
    update, error = read_update_settings(paths.config)
    document = state.load_state(paths.state)
    now = clock()
    next_check = None
    if update is not None and update.mode != 'off':
        last = state.parse_stamp(document.get('last_success_utc')) if document else None
        next_check = state.stamp(now if last is None else last + timedelta(hours=update.check_interval_hours))
    token_configured = paths.token.exists()
    if json_output:
        emit(json.dumps({
            'installed_version': current,
            'settings': asdict(update) if update is not None else None,
            'settings_error': error,
            'token_configured': token_configured,
            'next_check_utc': next_check,
            'state': document,
        }, ensure_ascii=False))
        return 0
    emit(f'Installed version: {current}')
    if update is None:
        emit('Settings: unreadable (run with sudo)')
        emit(f'Source: unknown ({error})')
    else:
        signatures = 'required' if update.require_signature else 'not required'
        emit(f'Update mode: {update.mode}, channel {update.channel}, signatures {signatures}')
        emit(f'Source: {update.source}')
    emit('Token: configured' if token_configured else 'Token: not configured')
    check_error = document.get('check_error') if document else None
    # The last attempt is checked_utc; last_success_utc only feeds "Next periodic check".
    checked = document.get('checked_utc') if document else None
    if checked:
        emit(f'Last check: {checked}, failed ({_check_error_text(check_error)})' if check_error
             else f'Last check: {checked}, succeeded')
    elif check_error:
        emit(f'Last check: failed ({_check_error_text(check_error)})')
    else:
        emit('Last check: never')
    channel = (document or {}).get('channel') or (update.channel if update else 'stable')
    newest_available = (document or {}).get('newest_available')
    if newest_available:
        line = f'Newest {channel} release: {newest_available["tag"]} ({newest_available["version"]})'
        if not (document or {}).get('newest_installable'):
            reason = _reason_for((document or {}).get('releases', []), newest_available)
            line += f', not installable: {_reason_text(reason)}'
        emit(line)
    else:
        emit(f'Newest {channel} release: none')
    emit('Update available: ' + ('yes' if (document or {}).get('update_available') else 'no'))
    in_progress = (document or {}).get('in_progress')
    if isinstance(in_progress, dict):
        steps = in_progress.get('steps')
        done = (sum(1 for step in steps if isinstance(step, dict) and step.get('state') == 'done')
                if isinstance(steps, list) else 0)
        total = len(steps) if isinstance(steps, list) else 0
        suffix = f' ({done}/{total} steps done)' if total else ''
        emit(f'Update in progress: {in_progress.get("to")} at step '
             f'{in_progress.get("step")}{suffix}')
    last_result = (document or {}).get('last_result')
    if isinstance(last_result, dict):
        suffix = f', {last_result.get("code")}' if last_result.get('code') else ''
        emit(f'Last update: {last_result.get("result")} ({last_result.get("reason")}){suffix}')
    rollback = (document or {}).get('rollback') or {}
    emit(f'Rollback package: {rollback.get("version")}' if rollback.get('available')
         else 'Rollback package: none')
    repair = (document or {}).get('repair')
    if isinstance(repair, dict) and repair.get('needed'):
        emit(f'Repair needed: {repair.get("status")}')
    auto = (document or {}).get('auto')
    if isinstance(auto, dict):
        emit(f'Automatic updates: {auto.get("reason")}')
    if update is not None and update.mode == 'off':
        emit('Next periodic check: off')
    elif next_check:
        emit(f'Next periodic check: after {next_check}')
    else:
        emit('Next periodic check: unknown')
    return 0


def run_set_token(paths, stdin):
    if not _is_root():
        emit('Run set-token with sudo.', warning=True)
        return 1
    try:
        if stdin.isatty():
            token = getpass.getpass('Access token (input hidden): ')
        else:
            token = stdin.readline(1025).rstrip('\r\n')
    except (EOFError, KeyboardInterrupt):
        emit('Token entry cancelled; nothing was saved.', warning=True)
        return 1
    try:
        write_token(paths.token, token)
    except ValueError:
        emit('The token must be 20 to 255 letters, digits or underscores; nothing was saved.', warning=True)
        return 1
    emit(f'Token saved in {paths.token} (root only, mode 0600).')
    return 0


def run_clear_token(paths):
    if not _is_root():
        emit('Run clear-token with sudo.', warning=True)
        return 1
    try:
        removed = remove_token(paths.token)
    except OSError as error:
        emit(f'Cannot remove {paths.token}: {error.strerror or error}; run with sudo.', warning=True)
        return 1
    emit('Token removed.' if removed else 'No token was configured.')
    return 0


def _requester_error(code):
    emit(f'{code}: {CODE_TEXT.get(code, code)}', warning=True)
    return 2


def _load_settings(path):
    """(Settings, None) or (None, error) without printing the INI."""
    path = Path(path)
    try:
        if not path.exists():
            return Settings(), None
        return load_settings(path), None
    except ValueError as error:
        return None, str(error)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _cgroup_text():
    """Tests patch this reader; production reads the process's own cgroup."""
    return Path('/proc/self/cgroup').read_text(encoding='utf-8', errors='replace')


def _asset_opener(paths):
    """The streaming opener downloads use; the token only reaches the configured source host.

    A release list can name asset URLs on any https host (a mirror or a hostile
    source), so the bearer token is attached only when the URL host is the
    source's host. Redirects never receive it (transport's unredirected header).
    """
    try:
        token = read_token(paths.token)
    except TokenFileError:
        token = None
    update, _ = read_update_settings(paths.config)
    source = urlsplit(update.source).hostname if update is not None else None

    def opener(url, *, accept, timeout):
        host = urlsplit(url).hostname
        permitted = token if (token and source and host == source) else None
        return transport.open_url(url, accept=accept, timeout=timeout, token=permitted)
    return opener


def _install_signals(stop):
    def handler(signum, frame):
        stop.set()
    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


def _sleep_for(sleep, stop, *, from_unit):
    if sleep is not None:
        return sleep
    if from_unit and stop is not None:
        return stop.wait
    return time.sleep


def _record_failure(paths, version, reason, *, step, code, detail, utcnow, runner,
                    request_id=None, unit=None):
    """A history line and last_result for a request refused before the transaction."""
    now = utcnow()
    try:
        installed = commands.dpkg_status(runner)
    except Exception:  # noqa: BLE001 - recording must not raise
        installed = None
    installed_after = installed[1] if installed else None
    record = history.make_record(**{
        'id': uuid.uuid4().hex, 'request_id': request_id,
        'started_at_utc': state.stamp(now), 'at_utc': state.stamp(now), 'duration_s': 0,
        'from': installed_after, 'to': version, 'reason': reason, 'result': 'failed',
        'step_failed': step, 'code': code, 'detail': detail, 'rollback_available': False,
        'installed_after': installed_after, 'config_restored': False, 'signed_by': None,
    })
    try:
        history.append(paths, record)
    except OSError:
        pass
    document = state.load_state(paths.state) or state.new_state(
        runtime_version.installed_version(), now)
    document['in_progress'] = None
    document['last_result'] = {**record,
                               'steps': [{'id': name, 'state': 'skipped'} for name in history.STEPS]}
    try:
        document['history'] = list(reversed(history.read(paths, limit=10)))
    except Exception:  # noqa: BLE001
        pass
    try:
        state.write_state(paths.state, document, now=now)
    except OSError:
        pass
    return record


def _result_matches(result, request_id, baseline_id):
    if request_id is not None:
        return result.get('request_id') == request_id
    return result.get('id') != baseline_id


def _wait_for_result(paths, unit, *, request_id, baseline_id, runner, utcnow, sleep):
    """Poll the state file while the unit runs; 0 when it ended in `success`."""
    started = utcnow()
    seen = None
    inactive_since = None
    while True:
        document = state.load_state(paths.state) or {}
        in_progress = document.get('in_progress')
        if isinstance(in_progress, dict):
            step = in_progress.get('step')
            if step != seen:
                emit(f'Step: {step}')
                seen = step
        result = document.get('last_result')
        if isinstance(result, dict) and _result_matches(result, request_id, baseline_id):
            return 0 if result.get('result') == 'success' else 1
        try:
            # A Type=oneshot unit stays "activating" for its whole runtime, and
            # `systemctl is-active` exits non-zero for that; read ActiveState.
            probe = runner(['systemctl', 'show', '-p', 'ActiveState', '--value', unit], timeout=30)
            state_name = (getattr(probe, 'stdout', '') or '').strip()
            active = probe.returncode == 0 and state_name in {
                'active', 'activating', 'reloading', 'deactivating'}
        except Exception:  # noqa: BLE001 - a missing systemctl must not hang the wait
            active = False
        if active:
            inactive_since = None
        else:
            inactive_since = inactive_since or utcnow()
            if (utcnow() - inactive_since).total_seconds() >= WAIT_STOP_SECONDS:
                emit(f'The updater unit stopped without a result; read its journal with: '
                     f'journalctl -fu {unit}', warning=True)
                return 1
        if (utcnow() - started).total_seconds() >= CHECK_MAX_SECONDS:
            emit(f'Gave up waiting for the update; read its journal with: journalctl -fu {unit}',
                 warning=True)
            return 1
        sleep(WAIT_POLL_SECONDS)


def _requester_install(paths, args, *, runner, utcnow, sleep):
    version = args.version
    if not isinstance(version, str) or verify.VERSION_PATTERN.fullmatch(version) is None:
        return _requester_error('invalid_version')
    settings, _ = _load_settings(paths.config)
    if settings is None:
        return _requester_error('config_unreadable')
    document = state.load_state(paths.state) or state.new_state(
        runtime_version.installed_version(), utcnow())
    try:
        installed = commands.dpkg_status(runner)
        plan = install.resolve(paths, settings, document, version, reason=args.reason,
                               installed=installed)
    except UpdateError as failure:
        return _requester_error(failure.code)
    if plan.downgrade and not args.allow_downgrade:
        return _requester_error('downgrade_not_confirmed')
    if units.transaction_live(document.get('in_progress'), boot_id=resources.read_boot_id(),
                              pid_alive=_pid_alive):
        return _requester_error('in_progress')
    if units.pending_request(paths, utcnow=utcnow):
        return _requester_error('request_pending')
    unit = units.install_unit(version)
    try:
        request_id = units.write_request(paths, version, reason=args.reason,
                                         requested_by='cli', utcnow=utcnow)
        units.start_unit(unit, runner=runner)
    except UpdateError as failure:
        return _requester_error(failure.code)
    emit(f'Started {unit}.')
    emit(f'Follow it with: journalctl -fu {unit}')
    if args.wait:
        return _wait_for_result(paths, unit, request_id=request_id, baseline_id=None,
                                runner=runner, utcnow=utcnow, sleep=sleep)
    return 0


def _unit_install(paths, args, *, runner, open_url, monotonic, utcnow, sleep, stop):
    version = args.version
    unit = units.install_unit(version)
    if (not isinstance(version, str) or verify.VERSION_PATTERN.fullmatch(version) is None
            or not units.running_in_unit(unit, _cgroup_text())):
        emit('--from-unit runs only inside its unit; '
             f'use "qmi-supervisor-update install <version>"', warning=True)
        return 2
    _install_signals(stop)
    try:
        with lock.update_lock(paths.lock, wait_seconds=120, clock=monotonic, sleep=sleep):
            return _unit_install_locked(paths, version, unit=unit, runner=runner,
                                        open_url=open_url, monotonic=monotonic,
                                        utcnow=utcnow, sleep=sleep, stop=stop)
    except LockBusy:
        emit('Another update operation holds the lock; nothing was recorded.', warning=True)
        return 75


def _unit_install_locked(paths, version, *, unit, runner, open_url, monotonic, utcnow, sleep, stop):
    document = state.load_state(paths.state) or state.new_state(
        runtime_version.installed_version(), utcnow())
    install.reconcile_interrupted(paths, document, runner=runner, clock=monotonic,
                                  utcnow=utcnow, log=LOG)
    request = units.take_request(paths, version, utcnow=utcnow)
    reason = request.get('reason') if request else 'manual'
    if reason not in history.REASONS:
        reason = 'manual'
    request_id = request.get('request_id') if request else None
    settings, error = _load_settings(paths.config)
    if settings is None:
        _record_failure(paths, version, reason, step='resolve', code='config_unreadable',
                        detail=error, utcnow=utcnow, runner=runner, request_id=request_id,
                        unit=unit)
        emit(f'config_unreadable: {CODE_TEXT["config_unreadable"]}', warning=True)
        return 1
    installer = install.Installer(paths, settings, runner=runner, open_url=open_url,
                                  keyring=verify.keyring_path(paths), clock=monotonic,
                                  utcnow=utcnow, sleep=sleep, stop=stop)
    record = installer.run(version, reason=reason, request_id=request_id, unit=unit)
    return 0 if record['result'] == 'success' else 1


def _requester_rollback(paths, args, *, runner, utcnow, sleep):
    settings, _ = _load_settings(paths.config)
    if settings is None:
        return _requester_error('config_unreadable')
    document = state.load_state(paths.state) or state.new_state(
        runtime_version.installed_version(), utcnow())
    rollback = document.get('rollback') or {}
    version = rollback.get('version')
    if not rollback.get('available') or not version:
        return _requester_error('no_rollback')
    try:
        installed = commands.dpkg_status(runner)
        install.resolve(paths, settings, document, version, reason='rollback', installed=installed)
    except UpdateError as failure:
        return _requester_error(failure.code)
    if units.transaction_live(document.get('in_progress'), boot_id=resources.read_boot_id(),
                              pid_alive=_pid_alive):
        return _requester_error('in_progress')
    if units.pending_request(paths, utcnow=utcnow):
        return _requester_error('request_pending')
    baseline_id = (document.get('last_result') or {}).get('id')
    try:
        units.start_unit(units.ROLLBACK_UNIT, runner=runner)
    except UpdateError as failure:
        return _requester_error(failure.code)
    emit(f'Started {units.ROLLBACK_UNIT}.')
    emit(f'Follow it with: journalctl -fu {units.ROLLBACK_UNIT}')
    if args.wait:
        return _wait_for_result(paths, units.ROLLBACK_UNIT, request_id=None,
                                baseline_id=baseline_id, runner=runner, utcnow=utcnow,
                                sleep=sleep)
    return 0


def _unit_rollback(paths, *, runner, open_url, monotonic, utcnow, sleep, stop):
    if not units.running_in_unit(units.ROLLBACK_UNIT, _cgroup_text()):
        emit('--from-unit runs only inside its unit; '
             'use "qmi-supervisor-update rollback"', warning=True)
        return 2
    _install_signals(stop)
    try:
        with lock.update_lock(paths.lock, wait_seconds=120, clock=monotonic, sleep=sleep):
            return _unit_rollback_locked(paths, runner=runner, open_url=open_url,
                                         monotonic=monotonic, utcnow=utcnow, sleep=sleep,
                                         stop=stop)
    except LockBusy:
        emit('Another update operation holds the lock; nothing was recorded.', warning=True)
        return 75


def _unit_rollback_locked(paths, *, runner, open_url, monotonic, utcnow, sleep, stop):
    document = state.load_state(paths.state) or state.new_state(
        runtime_version.installed_version(), utcnow())
    install.reconcile_interrupted(paths, document, runner=runner, clock=monotonic,
                                  utcnow=utcnow, log=LOG)
    settings, error = _load_settings(paths.config)
    rollback = document.get('rollback') or {}
    version = rollback.get('version')
    if settings is None:
        _record_failure(paths, version or 'unknown', 'rollback', step='resolve',
                        code='config_unreadable', detail=error, utcnow=utcnow, runner=runner,
                        unit=units.ROLLBACK_UNIT)
        emit(f'config_unreadable: {CODE_TEXT["config_unreadable"]}', warning=True)
        return 1
    if not rollback.get('available') or not version:
        _record_failure(paths, version or 'unknown', 'rollback', step='resolve',
                        code='no_rollback', detail=None, utcnow=utcnow, runner=runner,
                        unit=units.ROLLBACK_UNIT)
        emit(f'no_rollback: {CODE_TEXT["no_rollback"]}', warning=True)
        return 1
    installer = install.Installer(paths, settings, runner=runner, open_url=open_url,
                                  keyring=verify.keyring_path(paths), clock=monotonic,
                                  utcnow=utcnow, sleep=sleep, stop=stop)
    record = installer.run(version, reason='rollback', unit=units.ROLLBACK_UNIT)
    return 0 if record['result'] == 'success' else 1


def _parser():
    parser = argparse.ArgumentParser(prog='qmi-supervisor-update',
                                     description='Check for and manage qmi-supervisor releases.')
    parser.add_argument('--config', default=None, metavar='PATH',
                        help='configuration file (default: /etc/qmi-supervisor.ini)')
    parser.add_argument('--state-dir', default=None, metavar='DIR',
                        help='state directory (default: /var/lib/qmi-supervisor/update)')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check', help='read the release list now and update the state file')
    check.add_argument('--auto', action='store_true', help='periodic run: quiet, no failure exit status')
    listing = commands.add_parser('list', help='show the releases known from the last check')
    listing.add_argument('--json', action='store_true')
    status = commands.add_parser('status', help='show installed, policy and last check')
    status.add_argument('--json', action='store_true')
    commands.add_parser('set-token', help='save an access token for a private repository')
    commands.add_parser('clear-token', help='remove the stored access token')
    install_parser = commands.add_parser(
        'install', help='install a released version; it runs in its own systemd unit')
    install_parser.add_argument('version', help='Debian package version, for example 1.4.0-1')
    install_parser.add_argument('--allow-downgrade', action='store_true',
                                help='confirm the installation of an older version')
    install_parser.add_argument('--reason', choices=('manual', 'auto'), default='manual')
    install_parser.add_argument('--from-unit', action='store_true', help=argparse.SUPPRESS)
    install_parser.add_argument('--wait', action='store_true',
                                help='follow the transaction until it ends')
    rollback_parser = commands.add_parser(
        'rollback', help='restore the kept previous version in its own systemd unit')
    rollback_parser.add_argument('--from-unit', action='store_true', help=argparse.SUPPRESS)
    rollback_parser.add_argument('--wait', action='store_true',
                                 help='follow the transaction until it ends')
    return parser


def main(argv=None, *, paths=None, fetch=None, clock=None, stdin=None, runner=None,
         open_url=None, monotonic=None, sleep=None, stop=None, now_local=None):
    args = _parser().parse_args(argv)
    base = paths if paths is not None else UpdatePaths()
    overrides = {}
    if args.config is not None:
        overrides['config'] = Path(args.config)
    if args.state_dir is not None:
        overrides['state_dir'] = Path(args.state_dir)
    resolved = replace(base, **overrides) if overrides else base
    fetch = fetch or releases.fetch_releases
    clock = clock or state.utc_now
    now = clock if callable(clock) else getattr(clock, 'utcnow', state.utc_now)
    stdin = stdin if stdin is not None else sys.stdin
    runner = runner or commands.run_command
    if monotonic is None:
        monotonic = getattr(clock, 'monotonic', None) or time.monotonic
    if sleep is None:
        sleep = getattr(clock, 'sleep', None)
    if args.command == 'check':
        local_now = now_local
        if local_now is None:
            local_now = getattr(clock, 'now_local', None)
        if local_now is None:
            local_now = now().astimezone()
        elif callable(local_now):
            local_now = local_now()
        return run_check(resolved, automatic=args.auto, fetch=fetch, now=now, runner=runner,
                         monotonic=monotonic, sleep=sleep, now_local=local_now)
    if args.command == 'list':
        return run_list(resolved, json_output=args.json)
    if args.command == 'status':
        return run_status(resolved, json_output=args.json, clock=now)
    if args.command == 'set-token':
        return run_set_token(resolved, stdin)
    if args.command == 'clear-token':
        return run_clear_token(resolved)
    if args.command == 'install':
        if args.from_unit:
            stop = stop if stop is not None else threading.Event()
            return _unit_install(resolved, args, runner=runner,
                                 open_url=open_url or _asset_opener(resolved),
                                 monotonic=monotonic, utcnow=now,
                                 sleep=_sleep_for(sleep, stop, from_unit=True), stop=stop)
        return _requester_install(resolved, args, runner=runner, utcnow=now,
                                  sleep=_sleep_for(sleep, stop, from_unit=False))
    if args.command == 'rollback':
        if args.from_unit:
            stop = stop if stop is not None else threading.Event()
            return _unit_rollback(resolved, runner=runner,
                                  open_url=open_url or _asset_opener(resolved),
                                  monotonic=monotonic, utcnow=now,
                                  sleep=_sleep_for(sleep, stop, from_unit=True), stop=stop)
        return _requester_rollback(resolved, args, runner=runner, utcnow=now,
                                   sleep=_sleep_for(sleep, stop, from_unit=False))
    return 2
