"""Adapter between the web UI and the updater.

The web never downloads, verifies or installs anything: this module only reads
the updater's state file and asks systemd to start the updater's own units,
which run outside `qmi-supervisor.service`. Every updater name the web needs is
imported here, so the HTTP contract of `modules/web/server.py` does not depend
on the updater's internal spellings.

Nothing here imports GI, opens a network connection or logs a password, a
session identifier or a CSRF token.
"""
from functools import lru_cache, cmp_to_key
import json
import logging
import math
import os
from pathlib import Path
import stat
import subprocess
import threading
import time

from modules.runtime.version import compare_versions, installed_version
from modules.update.state import UpdatePaths
from modules.update.units import INSTALL_PREFIX, ROLLBACK_UNIT, escape_instance
from modules.update.verify import VERSION_PATTERN

LOG = logging.getLogger(__name__)

CHECK_UNIT = 'qmi-supervisor-update-check.service'
# The timer runs `check --auto` (it honours mode and the periodic interval). A
# web request is an explicit check, so it starts the unit that runs plain `check`.
CHECK_MANUAL_UNIT = 'qmi-supervisor-update-check-manual.service'
# The updater keeps the state path in `UpdatePaths.state`; derive it instead of copying it.
STATE_PATH = UpdatePaths().state
BUILD_INFO_PATH = Path(__file__).resolve().parents[2] / 'BUILD_INFO.json'

MAX_STATE_BYTES = 512 * 1024
MAX_BUILD_INFO_BYTES = 4 * 1024
MAX_RELEASES = 20
MAX_NOTES = 4000
MAX_HISTORY = 10
MAX_TEXT = 256
MAX_STEPS = 20
CHECK_INTERVAL_SECONDS = 60
INSTALL_GRACE_SECONDS = 30

_RUNNING_ACTIVE = frozenset({'activating', 'active', 'deactivating', 'reloading'})
_ACTIONS = {'manual': 'install', 'auto': 'automatic', 'rollback': 'rollback', 'repair': 'repair'}
_RESULTS = {'success': 'succeeded', 'failed': 'failed', 'rolled_back': 'rolled_back',
            'interrupted': 'interrupted'}
_STEP_STATES = frozenset({'pending', 'running', 'done', 'failed', 'skipped'})
_UNAVAILABLE_MESSAGE = 'The updater is not available on this device.'


class UpdateError(Exception):
    """A refused or failed web update request with its HTTP status and code."""

    def __init__(self, status, code, message, reason=None, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.reason, self.retry_after = reason, retry_after


def valid_version(value):
    """Whether `value` is a package version the updater may install."""
    return type(value) is str and VERSION_PATTERN.fullmatch(value) is not None


def unit_instance(version):
    """The escaped unit instance, exactly as `systemd-escape -- VALUE` prints it."""
    if not valid_version(version):
        raise ValueError('invalid package version')
    return escape_instance(version)


def install_unit(version):
    return INSTALL_PREFIX + unit_instance(version) + '.service'


def _bounded(value):
    """A bounded string or None; never a non-text value from the state file."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value[:MAX_TEXT]


def _relation(version, installed):
    if not valid_version(version) or not isinstance(installed, str) or installed == 'unknown':
        return 'unknown'
    try:
        order = compare_versions(version, installed)
    except (TypeError, ValueError):
        return 'unknown'
    if order == 0:
        return 'installed'
    return 'newer' if order > 0 else 'older'


def _action(reason):
    return _ACTIONS.get(reason, 'install')


def _result(result):
    return _RESULTS.get(result, 'failed')


def _steps(value):
    steps = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        identifier = item.get('id')
        if not isinstance(identifier, str) or not identifier:
            continue
        state = item.get('state')
        steps.append({'id': identifier[:MAX_TEXT],
                      'state': state if state in _STEP_STATES else 'pending'})
        if len(steps) >= MAX_STEPS:
            break
    return steps


def _normalise_release(release):
    """One stored release in the web's names, or None when it cannot be installed.

    The updater stores the upstream version in `version` (no Debian revision) and the
    installable package version in `deb.version`; installs and comparisons must
    use the latter, exactly like the updater's `find_release()`.
    """
    if not isinstance(release, dict):
        return None
    deb = release.get('deb') if isinstance(release.get('deb'), dict) else {}
    version = deb.get('version')
    if not valid_version(version):
        return None
    notes = release.get('notes')
    notes = notes if isinstance(notes, str) else ''
    truncated = bool(release.get('notes_truncated')) or len(notes) > MAX_NOTES
    published = release.get('published_utc')
    reason = release.get('reason')
    return {
        'tag': _bounded(release.get('tag')),
        'version': version,
        'published_at_utc': published[:MAX_TEXT] if isinstance(published, str) else None,
        'prerelease': bool(release.get('prerelease')),
        'notes': notes[:MAX_NOTES],
        'notes_truncated': truncated,
        'installable': bool(release.get('installable')),
        'reason': reason[:MAX_TEXT] if isinstance(reason, str) else None,
    }


def _normalise_install(document):
    """the updater's `in_progress`/`last_result` in the web's `install` shape."""
    tx = document.get('in_progress')
    if isinstance(tx, dict):
        return {
            'action': _action(tx.get('reason')),
            'state': 'running',
            'version': _bounded(tx.get('to')),
            'from_version': _bounded(tx.get('from')),
            'step': _bounded(tx.get('step')),
            'steps': _steps(tx.get('steps')),
            'started_at_utc': _bounded(tx.get('started_at_utc')),
            'finished_at_utc': None,
            'error_code': None,
            'error': None,
        }
    result = document.get('last_result')
    if isinstance(result, dict):
        return {
            'action': _action(result.get('reason')),
            'state': _result(result.get('result')),
            'version': _bounded(result.get('to')),
            'from_version': _bounded(result.get('from')),
            'step': _bounded(result.get('step_failed')),
            'steps': _steps(result.get('steps')),
            'started_at_utc': _bounded(result.get('started_at_utc')),
            'finished_at_utc': _bounded(result.get('at_utc')),
            'error_code': _bounded(result.get('code')),
            'error': _bounded(result.get('detail')),
        }
    return None


def _normalise_history(document):
    history = []
    for record in document.get('history') if isinstance(document.get('history'), list) else []:
        if not isinstance(record, dict):
            continue
        history.append({
            'action': _action(record.get('reason')),
            'version': _bounded(record.get('to')),
            'from_version': _bounded(record.get('from')),
            'result': _result(record.get('result')),
            'started_at_utc': _bounded(record.get('started_at_utc')),
            'finished_at_utc': _bounded(record.get('at_utc')),
            'error_code': _bounded(record.get('code')),
            'error': _bounded(record.get('detail')),
        })
        if len(history) >= MAX_HISTORY:
            break
    return history


def _normalise_rollback(document):
    rollback = document.get('rollback')
    if not isinstance(rollback, dict) or rollback.get('available') is not True:
        return {'available': False, 'version': None}
    version = rollback.get('version')
    if not valid_version(version):
        return {'available': False, 'version': None}
    return {'available': True, 'version': version}


def _normalise_state(document):
    """the updater's state file in the adapter's normalised view (newest releases first)."""
    releases = [release for release in
                (_normalise_release(item) for item in document.get('releases') or [])
                if release is not None]
    releases.sort(key=cmp_to_key(lambda left, right: compare_versions(right['version'], left['version'])))
    releases = releases[:MAX_RELEASES]
    error = document.get('check_error')
    error = error if isinstance(error, dict) else None
    return {
        'checked_at_utc': _bounded(document.get('checked_utc')),
        'check': {
            'state': 'failed' if error is not None else 'ok',
            'error_code': _bounded(error.get('code')) if error else None,
            'error': _bounded(error.get('detail')) if error else None,
        },
        'releases': releases,
        'install': _normalise_install(document),
        'history': _normalise_history(document),
        'rollback': _normalise_rollback(document),
    }


def read_state(path):
    """The normalised state and an error marker; never raises.

    Missing file → `(None, None)`. Any other problem (a symlink, another owner,
    a world-writable file, more than 512 KiB, invalid JSON or a non-object)
    → `(None, 'unreadable')`.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, 'unreadable'
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o002 or metadata.st_size > MAX_STATE_BYTES):
            return None, 'unreadable'
        content = b''
        while len(content) <= MAX_STATE_BYTES:
            chunk = os.read(descriptor, MAX_STATE_BYTES + 1 - len(content))
            if not chunk:
                break
            content += chunk
    except OSError:
        return None, 'unreadable'
    finally:
        os.close(descriptor)
    if len(content) > MAX_STATE_BYTES:
        return None, 'unreadable'
    try:
        document = json.loads(content.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, 'unreadable'
    if not isinstance(document, dict):
        return None, 'unreadable'
    try:
        return _normalise_state(document), None
    except Exception:  # noqa: BLE001 - an untrusted file must never crash the web
        return None, 'unreadable'


def unit_states(run):
    """Which updater units systemd reports running; None when it cannot be asked."""
    command = ['systemctl', 'list-units', '--all', '--output=json', '--no-pager',
               CHECK_UNIT, CHECK_MANUAL_UNIT, INSTALL_PREFIX + '*.service', ROLLBACK_UNIT]
    try:
        result = run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        units = json.loads(result.stdout)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(units, list):
        return None
    states = {'check_running': False, 'install_running': False,
              'install_unit': None, 'rollback_running': False}
    for entry in units:
        if not isinstance(entry, dict):
            continue
        name, active = entry.get('unit'), entry.get('active')
        if not isinstance(name, str) or active not in _RUNNING_ACTIVE:
            continue
        if name in (CHECK_UNIT, CHECK_MANUAL_UNIT):
            states['check_running'] = True
        elif name == ROLLBACK_UNIT:
            states['rollback_running'] = True
        elif name.startswith(INSTALL_PREFIX) and name.endswith('.service'):
            states['install_running'] = True
            states['install_unit'] = name
    return states


def start_unit(run, unit):
    """Ask systemd to start `unit` without blocking; its failure is a 503."""
    try:
        result = run(['systemctl', 'start', '--no-block', unit],
                     capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        LOG.warning('Cannot start %s: %s', unit, str(error)[:300])
        raise UpdateError(503, 'UPDATER_UNAVAILABLE', _UNAVAILABLE_MESSAGE) from None
    if result.returncode != 0:
        LOG.warning('Cannot start %s: %s', unit, str(result.stderr or '')[:300])
        raise UpdateError(503, 'UPDATER_UNAVAILABLE', _UNAVAILABLE_MESSAGE)


@lru_cache(maxsize=None)
def build_info():
    """`BUILD_INFO.json` next to `qmi_supervisor.py`, cached for the process.

    Missing, oversized or invalid files answer `{'commit': None,
    'built_utc': None}`; no other key is ever read.
    """
    missing = {'commit': None, 'built_utc': None}
    try:
        descriptor = os.open(BUILD_INFO_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return missing
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BUILD_INFO_BYTES:
            return missing
        raw = os.read(descriptor, MAX_BUILD_INFO_BYTES + 1)
    except OSError:
        return missing
    finally:
        os.close(descriptor)
    if len(raw) > MAX_BUILD_INFO_BYTES:
        return missing
    try:
        document = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return missing
    if not isinstance(document, dict):
        return missing
    commit = _bounded(document.get('commit'))
    built = _bounded(document.get('built_utc'))
    return {'commit': commit, 'built_utc': built}


class UpdateControl:
    """One per web process, shared by every listener, so limits are host-wide."""

    def __init__(self, state_path=STATE_PATH, *, run=subprocess.run, clock=time.monotonic):
        self.state_path = Path(state_path)
        self.run = run
        self.clock = clock
        self.lock = threading.Lock()
        self._last_check = None
        self._last_start = None

    # -- helpers -----------------------------------------------------------
    def _check_retry_after(self, now):
        if self._last_check is None:
            return 0
        remaining = CHECK_INTERVAL_SECONDS - (now - self._last_check)
        return max(0, math.ceil(remaining)) if remaining > 0 else 0

    def _within_grace(self, now):
        return (self._last_start is not None
                and now - self._last_start < INSTALL_GRACE_SECONDS)

    def _annotate(self, release, *, installed, channel, allow_downgrade):
        relation = _relation(release['version'], installed)
        installable = release.get('installable') is True
        reason = release.get('reason')
        if relation == 'installed':
            installable, reason = False, 'installed'
        elif relation == 'unknown':
            installable, reason = False, 'installed_unknown'
        elif release.get('prerelease') and channel == 'stable':
            installable, reason = False, 'prerelease_channel'
        elif relation == 'older' and not allow_downgrade:
            installable, reason = False, 'downgrade_disabled'
        return dict(release, relation=relation, installable=installable, reason=reason)

    def _settings_view(self, settings):
        update = settings.update
        return {'mode': update.mode, 'channel': update.channel,
                'window_start': update.window_start, 'window_end': update.window_end,
                'check_interval_hours': update.check_interval_hours,
                'require_signature': update.require_signature,
                'auto_rollback': update.auto_rollback,
                'allow_downgrade': update.allow_downgrade,
                'source': update.source}

    def _usable_units(self):
        states = unit_states(self.run)
        if states is None:
            raise UpdateError(503, 'UPDATER_UNAVAILABLE', _UNAVAILABLE_MESSAGE)
        return states

    # -- POST /api/update/check -------------------------------------------
    def start_check(self):
        with self.lock:
            now = self.clock()
            retry = self._check_retry_after(now)
            if retry:
                raise UpdateError(429, 'RATE_LIMITED', 'Wait a minute before checking again.',
                                  retry_after=retry)
            states = self._usable_units()
            if states['install_running'] or states['rollback_running'] or self._within_grace(now):
                raise UpdateError(409, 'UPDATE_IN_PROGRESS', 'An update is already running.')
            if states['check_running']:
                return {'started': False, 'unit': CHECK_MANUAL_UNIT,
                        'retry_after': CHECK_INTERVAL_SECONDS}
            start_unit(self.run, CHECK_MANUAL_UNIT)
            self._last_check = now
            LOG.info('Web request: update check')
            return {'started': True, 'unit': CHECK_MANUAL_UNIT,
                    'retry_after': CHECK_INTERVAL_SECONDS}

    # -- POST /api/update/install -----------------------------------------
    def prepare_install(self, settings, version, allow_downgrade):
        """Validate an install request; return its annotated release or raise."""
        if not valid_version(version):
            raise UpdateError(400, 'INVALID_VERSION', 'The version is not valid.')
        installed = installed_version()
        if not isinstance(installed, str) or installed == 'unknown':
            raise UpdateError(409, 'VERSION_UNKNOWN',
                              'The installed version is unknown. Update from the device.')
        state, _error = read_state(self.state_path)
        if state is None:
            raise UpdateError(409, 'NO_RELEASE_LIST', 'Check for updates first.')
        release = next((item for item in state['releases'] if item['version'] == version), None)
        if release is None:
            raise UpdateError(409, 'UNKNOWN_VERSION',
                              'This version is not in the list of releases. Check again.')
        update = settings.update
        release = self._annotate(release, installed=installed, channel=update.channel,
                                 allow_downgrade=update.allow_downgrade)
        if release['relation'] == 'installed':
            raise UpdateError(409, 'ALREADY_INSTALLED', 'This version is already installed.')
        older = release['relation'] == 'older'
        if older and not update.allow_downgrade:
            raise UpdateError(403, 'DOWNGRADE_DISABLED',
                              'Installing older versions is turned off.')
        if older and not allow_downgrade:
            raise UpdateError(400, 'DOWNGRADE_NOT_CONFIRMED',
                              'Confirm the installation of an older version.')
        if not release['installable']:
            raise UpdateError(409, 'NOT_INSTALLABLE', 'This version cannot be installed.',
                              reason=release.get('reason'))
        now = self.clock()
        states = self._usable_units()
        if states['install_running'] or states['rollback_running'] or self._within_grace(now):
            raise UpdateError(409, 'UPDATE_IN_PROGRESS', 'An update is already running.')
        return release

    def start_install(self, settings, version, allow_downgrade):
        with self.lock:
            self.prepare_install(settings, version, allow_downgrade)
            unit = install_unit(version)
            from_version = installed_version()
            start_unit(self.run, unit)
            self._last_start = self.clock()
            LOG.info('Web request: install %s (installed %s)', version, from_version)
            return {'started': True, 'unit': unit, 'version': version,
                    'from_version': from_version}

    # -- POST /api/update/rollback ----------------------------------------
    def prepare_rollback(self):
        state, _error = read_state(self.state_path)
        rollback = state['rollback'] if state is not None else {'available': False, 'version': None}
        if not rollback['available'] or not rollback['version']:
            raise UpdateError(409, 'NO_ROLLBACK',
                              'No previous version is kept for a rollback.')
        now = self.clock()
        states = self._usable_units()
        if states['install_running'] or states['rollback_running'] or self._within_grace(now):
            raise UpdateError(409, 'UPDATE_IN_PROGRESS', 'An update is already running.')
        return rollback

    def start_rollback(self):
        with self.lock:
            rollback = self.prepare_rollback()
            from_version = installed_version()
            start_unit(self.run, ROLLBACK_UNIT)
            self._last_start = self.clock()
            LOG.info('Web request: rollback to %s', rollback['version'])
            return {'started': True, 'unit': ROLLBACK_UNIT, 'version': rollback['version'],
                    'from_version': from_version}

    # -- GET /api/update ---------------------------------------------------
    def status(self, settings):
        installed = installed_version()
        state, state_error = read_state(self.state_path)
        units = unit_states(self.run)
        info = build_info()
        now = self.clock()
        update = settings.update

        releases = []
        if state is not None:
            releases = [self._annotate(release, installed=installed, channel=update.channel,
                                       allow_downgrade=update.allow_downgrade)
                        for release in state['releases']]

        check = {'state': 'never', 'checked_at_utc': None, 'error_code': None,
                 'error': None, 'running': bool(units and units['check_running']),
                 'retry_after': self._check_retry_after(now)}
        install = None
        history = []
        rollback = {'available': False, 'version': None, 'running': False}
        if state is not None:
            check.update(state['check'])
            check['checked_at_utc'] = state['checked_at_utc']
            install = dict(state['install']) if state['install'] is not None else None
            history = list(state['history'])
            rollback.update(state['rollback'])
        rollback['running'] = bool(units and units['rollback_running'])

        if install is not None:
            running = bool(units and units['install_running'])
            if (install['state'] == 'running' and units is not None and not running
                    and not self._within_grace(now)):
                install['state'] = 'interrupted'
            install['running'] = running and install['state'] == 'running'

        return {
            'installed': {'version': installed, 'commit': info['commit'],
                          'built_utc': info['built_utc']},
            'settings': self._settings_view(settings),
            'check': check,
            'releases': releases,
            'install': install,
            'history': history,
            'rollback': rollback,
            'state_error': state_error,
            'units_error': 'unavailable' if units is None else None,
        }
