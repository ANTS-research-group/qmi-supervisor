"""Resolve, install and roll back one version in a single locked transaction.

`Installer.run()` walks the ten published steps, saving `in_progress.step` to
`state.json` before each one, decides from dpkg's state (never from apt-get's
exit code) and, when a changed package fails its health check, restores the
previous configuration and package. It repairs only the named package.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from modules.runtime import version as runtime_version
from modules.runtime.configuration import configuration_lock
from modules.telemetry.resources import read_boot_id
from modules.update import health, history
from modules.update import state as state_module
from modules.update import units as units_module
from modules.update.commands import APT_ENV, command_env, dpkg_status
from modules.update.errors import Interrupted, UpdateError
from modules.update.files import ensure_private_dir, read_nofollow, write_private
from modules.update.verify import (
    DOWNLOAD_MAX_BYTES, SIGNATURE, SUMS, SUMS_MAX_BYTES, VERSION_PATTERN,
    check_package, deb_name, download, parse_sums, sha256_file,
    verify_signature,
)

LOG = logging.getLogger(__name__)

VERIFIED = 'verified.json'
VERIFIED_MAX_BYTES = 16 * 1024
BACKUP_PATTERN = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{8}')
KEEP_BACKUPS = 5
APT_TIMEOUT = 600
SYSTEMCTL_TIMEOUT = 120
LOCK_MARKERS = ('Could not get lock', 'Unable to acquire the dpkg frontend lock')
HEX64 = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class Plan:
    """What a resolved request will do."""
    version: str
    reason: str
    downgrade: bool
    release: object
    cached: object


def release_version(release):
    """The Debian version a stored release installs."""
    deb = release.get('deb') if isinstance(release, dict) else None
    if isinstance(deb, dict) and deb.get('version'):
        return deb['version']
    return release.get('version') if isinstance(release, dict) else None


def find_release(state_value, version):
    for release in state_value.get('releases') or []:
        if isinstance(release, dict) and release_version(release) == version:
            return release
    return None


def _size(asset):
    value = asset.get('size') if isinstance(asset, dict) else None
    return value if type(value) is int and value > 0 else None


def _last_line(text):
    lines = [line for line in str(text or '').splitlines() if line.strip()]
    return lines[-1] if lines else None


def cached_package(paths, version):
    """The verified cache record for `version`, only when the package still matches it."""
    directory = Path(paths.cache) / version
    try:
        raw = read_nofollow(directory / VERIFIED, VERIFIED_MAX_BYTES)
        record = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    expected = record.get('sha256')
    if not isinstance(expected, str) or HEX64.fullmatch(expected) is None:
        return None
    try:
        actual = sha256_file(directory / deb_name(version))
    except OSError:
        return None
    return record if actual == expected else None


def is_installable_release(state_value, version):
    """Whether `version` is one of the stored, installable releases."""
    return any(isinstance(release, dict) and release.get('installable')
               and release_version(release) == version
               for release in state_value.get('releases') or [])


def rollback_ready(paths, state_value, installed_version):
    """Whether a package of the installed version lets the next update be undone."""
    if not installed_version:
        return False
    if cached_package(paths, installed_version) is not None:
        return True
    return is_installable_release(state_value, installed_version)


def checksum_name(entries, version, published_name=None):
    """The SHA256SUMS key for a package version, or None.

    GitHub rewrites ~ and + in asset names to '.', and stage-release.sh writes
    SHA256SUMS with the uploaded name. Accept the published name, the local one
    and both dotted spellings; the cached file keeps its local name.
    """
    candidates = []
    if isinstance(published_name, str) and published_name:
        candidates.append(published_name)
    candidates.append(deb_name(version))
    dotted = 'qmi-supervisor_' + version.replace('~', '.').replace('+', '.') + '_all.deb'
    if dotted not in candidates:
        candidates.append(dotted)
    return next((candidate for candidate in candidates if candidate in entries), None)


def resolve(paths, settings, state_value, version, *, reason, installed):
    """Validate a request without network access or writes; return a `Plan`."""
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise UpdateError('resolve', 'invalid_version', 'the requested version is not valid')
    update = getattr(settings, 'update', settings)
    if installed is None and reason != 'repair':
        raise UpdateError('resolve', 'not_package_install',
                          'this installation was not made with the Debian package')
    if installed is not None and installed[0] != 'ii' and reason != 'repair':
        raise UpdateError('resolve', 'package_state_broken',
                          'the installed package is not completely installed')
    if installed is not None and installed == ('ii', version):
        raise UpdateError('resolve', 'already_installed', 'this version is already installed')

    release = cached = None
    if reason in ('manual', 'auto'):
        release = find_release(state_value, version)
        if release is None:
            raise UpdateError('resolve', 'unknown_version',
                              'this version is not in the list of releases')
        deb = release.get('deb') or {}
        if not deb or not deb.get('url'):
            raise UpdateError('resolve', 'no_package', 'the release has no package')
        sums = release.get('sums') or {}
        if not sums or not sums.get('url'):
            raise UpdateError('resolve', 'no_checksums', 'the release has no checksum file')
        signature = release.get('signature') or {}
        if update.require_signature and not signature.get('url'):
            raise UpdateError('resolve', 'unsigned', 'the release is not signed')
        if release.get('prerelease') and update.channel == 'stable':
            raise UpdateError('resolve', 'prerelease_channel', 'pre-releases are not enabled')
    else:
        cached = cached_package(paths, version)
        if cached is None:
            raise UpdateError('resolve', 'no_cached_package',
                              'the package of that version is no longer stored on this device')
        if reason == 'rollback':
            rollback = state_value.get('rollback') or {}
            if rollback.get('version') != version:
                raise UpdateError('resolve', 'no_rollback', 'no previous version is kept')

    downgrade = bool(installed) and runtime_version.compare_versions(version, installed[1]) < 0
    if downgrade and reason in ('manual', 'auto') and not update.allow_downgrade:
        raise UpdateError('resolve', 'downgrade_disabled', 'installing older versions is turned off')
    return Plan(version, reason, downgrade, release, cached)


def _interrupted_detail(step, after):
    if after is None:
        return f'stopped during {step}; package state unknown'
    return f'stopped during {step}; package state {after[0]} {after[1]}'


def _duration(tx, now):
    started = state_module.parse_stamp((tx or {}).get('started_at_utc'))
    if started is None:
        return 0
    try:
        return max(0, int((now - started).total_seconds()))
    except (TypeError, ValueError):
        return 0


def reconcile_interrupted(paths, state_value, *, runner, clock, utcnow, log=LOG):
    """Record a transaction whose process is gone; prepare a repair when needed.

    Called by every update-lock holder before anything else. Holding the lock
    proves the recorded process is gone (it held the lock for its whole life
    and `/run` is a tmpfs). Repair is prepared only for `qmi-supervisor`, only
    from a cached verified package; nothing here configures every package.
    """
    tx = state_value.get('in_progress')
    if not isinstance(tx, dict):
        return None
    step = tx.get('step')
    if step is not None and step not in history.STEPS:
        step = None
    after = dpkg_status(runner)
    now = utcnow()
    record = history.make_record(**{
        'id': tx.get('id') or uuid.uuid4().hex,
        'request_id': tx.get('request_id'),
        'started_at_utc': tx.get('started_at_utc'),
        'at_utc': state_module.stamp(now),
        'duration_s': _duration(tx, now),
        'from': tx.get('from'), 'to': tx.get('to'),
        'reason': tx.get('reason') if tx.get('reason') in history.REASONS else 'manual',
        'result': 'interrupted', 'step_failed': step, 'code': 'interrupted',
        'detail': _interrupted_detail(step, after),
        'rollback_available': bool(tx.get('rollback_available')),
        'installed_after': after[1] if after else None,
        'config_restored': False, 'signed_by': tx.get('signed_by'),
    })
    try:
        history.append(paths, record)
    except OSError as error:
        log.error('Cannot append to the update history: %s', error)

    state_value['in_progress'] = None
    state_value['last_result'] = {**record,
                                  'steps': [dict(item) for item in tx.get('steps') or []
                                            if isinstance(item, dict)]}
    try:
        state_value['history'] = list(reversed(history.read(paths, limit=10)))
    except Exception as error:  # noqa: BLE001 - recording must not raise here
        log.error('Cannot read the update history: %s', error)

    finish_rollback = step == 'rollback' and after == ('ii', tx.get('to'))
    needed = after is None or after[0] != 'ii' or finish_rollback
    target = None
    if needed:
        if tx.get('from') and cached_package(paths, tx['from']) is not None:
            target = tx['from']
        elif (not finish_rollback and tx.get('to')
              and cached_package(paths, tx['to']) is not None):
            target = tx['to']
        state_value['repair'] = {
            'needed': True, 'status': after[0] if after else None, 'target': target,
            'attempted': False, 'detected_at_utc': state_module.stamp(now),
            'manual_command': None,
        }
    else:
        state_value['repair'] = None
    if after is not None:
        log.warning('Update %s interrupted during %s; package state %s %s',
                    tx.get('to'), step, after[0], after[1])
    else:
        log.warning('Update %s interrupted during %s; the package is not installed',
                    tx.get('to'), step)
    try:
        state_module.write_state(paths.state, state_value, now=now)
    except OSError as error:
        log.error('Cannot write the update state: %s', error)
    return record


def start_repair_if_needed(paths, state_value, *, runner, utcnow, log=LOG):
    """Start the repair unit once for a detected interrupted package, or warn once."""
    repair = state_value.get('repair')
    if not isinstance(repair, dict) or not repair.get('needed') or repair.get('attempted'):
        return False
    repair = dict(repair)
    repair['attempted'] = True
    state_value['repair'] = repair
    try:
        state_module.write_state(paths.state, state_value, now=utcnow())
    except OSError as error:
        log.error('Cannot write the update state: %s', error)
    target = repair.get('target')
    if not target:
        log.warning('Package qmi-supervisor is in state %s and no verified package is cached; '
                    'repair it by hand', repair.get('status'))
        return False
    try:
        units_module.write_request(paths, target, reason='repair', requested_by='check',
                                   utcnow=utcnow)
        units_module.start_unit(units_module.install_unit(target), runner=runner)
    except UpdateError as error:
        log.warning('Cannot start the updater to repair the package: %s', error.code)
        return False
    log.info('Repair of qmi-supervisor with %s started', target)
    return True


class Installer:
    """One transaction: resolve, download, verify, prepare, backup, snapshot,
    install, restart, health, then success or the automatic rollback."""

    def __init__(self, paths, settings, *, runner, open_url, keyring, clock=time.monotonic,
                 utcnow=state_module.utc_now, sleep=None, stop=None, log=LOG):
        self.paths = paths
        self.settings = settings
        self.update = getattr(settings, 'update', settings)
        self.runner = runner
        self.open_url = open_url
        self.keyring = keyring
        self.clock = clock
        self.utcnow = utcnow
        self.stop = stop
        self.sleep = sleep if sleep is not None else (stop.wait if stop is not None else time.sleep)
        self.log = log
        self.state = None
        self.tx = None
        self.plan = None
        self.current_step = 'resolve'
        self.started_at = None
        self.from_version = None
        self.to_version = None
        self.reason = 'manual'
        self.installed_before = None
        self.changed = False
        self.rollback_available = False
        self.signed_by = None
        self.online_before = {}
        self.web_before = False
        self.apt_end = None
        self.apt_started = None
        self.backup_dir = None
        self.backup_sha = None
        self.config_restored = False

    # -- state bookkeeping ------------------------------------------------
    def _mark(self, step_id, value):
        for step in self.tx['steps']:
            if step['id'] == step_id:
                step['state'] = value
                return

    def _save(self):
        self.tx['updated_at_utc'] = state_module.stamp(self.utcnow())
        try:
            state_module.write_state(self.paths.state, self.state, now=self.utcnow())
        except OSError as error:
            self.log.error('Cannot write the update state: %s', error)

    def _enter(self, step):
        self._check_stop(step)
        self.current_step = step
        self.tx['step'] = step
        self._mark(step, 'running')
        self._save()

    def _done(self, step):
        self._mark(step, 'done')
        self._save()

    def _check_stop(self, step):
        if self.stop is not None and self.stop.is_set():
            raise Interrupted(step)

    # -- commands ---------------------------------------------------------
    def _run(self, args, *, timeout, env=None):
        """(CompletedProcess, timed_out); a timeout never kills apt-get with SIGKILL."""
        try:
            return self.runner(args, timeout=timeout, env=env), False
        except subprocess.TimeoutExpired as error:
            stdout = error.output.decode('utf-8', 'replace') if isinstance(error.output, bytes) else (error.output or '')
            stderr = error.stderr.decode('utf-8', 'replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
            return subprocess.CompletedProcess(args, 124, stdout, stderr), True

    def _apt_detail(self, result, timed_out):
        if timed_out:
            return _last_line(result.stderr) or _last_line(result.stdout) or 'the package installation took too long'
        return _last_line(result.stderr) or _last_line(result.stdout)

    # -- downloads and verification --------------------------------------
    def _cache_dir(self, version, *, step):
        return ensure_private_dir(Path(self.paths.cache) / version, code='cache_unsafe', step=step)

    def _download_assets(self, release, version, *, step):
        directory = self._cache_dir(version, step=step)
        sums = release.get('sums') or {}
        signature = release.get('signature') or {}
        deb = release.get('deb') or {}
        download(self.open_url, sums['url'], directory / SUMS, step=step, name=SUMS,
                 max_bytes=SUMS_MAX_BYTES, expected_size=_size(sums), clock=self.clock)
        if signature.get('url'):
            download(self.open_url, signature['url'], directory / SIGNATURE, step=step,
                     name=SIGNATURE, max_bytes=SUMS_MAX_BYTES,
                     expected_size=_size(signature), clock=self.clock)
        download(self.open_url, deb['url'], directory / deb_name(version), step=step,
                 name=deb.get('name') or deb_name(version), max_bytes=DOWNLOAD_MAX_BYTES,
                 expected_size=_size(deb), clock=self.clock)

    def _at_step(self, error, step):
        if error.step == 'verify':
            return UpdateError(step, error.code, error.detail)
        return error

    def _verify_package(self, version, *, step, preflight):
        directory = Path(self.paths.cache) / version
        deb = directory / deb_name(version)
        sums_path = directory / SUMS
        signature_path = directory / SIGNATURE
        signed_by = None
        # A repair uses the cache as it is: the cached signature is checked, but
        # require_signature never asks a half-installed package for what it lacks.
        if signature_path.exists() or (self.update.require_signature and self.reason != 'repair'):
            try:
                signed_by = verify_signature(sums_path, signature_path, self.keyring,
                                             runner=self.runner, workdir=directory)
            except UpdateError as error:
                raise self._at_step(error, step) from None
        try:
            entries = parse_sums(read_nofollow(sums_path, SUMS_MAX_BYTES))
        except UpdateError as error:
            raise self._at_step(error, step) from None
        except (OSError, ValueError):
            raise UpdateError(step, 'sums_invalid', 'the checksum file could not be read') from None
        published = ((self.plan.release if self.plan else None) or {}).get('deb') or {}
        name = checksum_name(entries, version, published.get('name'))
        if name is None:
            raise UpdateError(step, 'checksum_missing',
                              f'{deb_name(version)} is not in the checksum file')
        try:
            actual = sha256_file(deb)
        except OSError:
            raise UpdateError(step, 'download_failed', f'{name}: the package is missing') from None
        if actual != entries[name]:
            try:
                deb.unlink()
            except OSError:
                pass
            raise UpdateError(step, 'checksum_mismatch', f'{name} does not match its checksum')
        try:
            check_package(deb, version, runner=self.runner, workdir=directory,
                          python=self.paths.python, config=self.paths.config, preflight=preflight)
        except UpdateError as error:
            raise self._at_step(error, step) from None
        size = deb.stat().st_size
        write_private(directory / VERIFIED, json.dumps({
            'schema_version': 1, 'version': version, 'sha256': actual, 'size': size,
            'signed_by': signed_by, 'verified_at_utc': state_module.stamp(self.utcnow()),
        }, sort_keys=True).encode('utf-8'))
        if signed_by:
            self.log.info('Package %s verified (sha256 %s..., signed by %s)', version, actual[:12], signed_by)
        else:
            self.log.info('Package %s verified (not signed; signatures not required)', version)
        return signed_by, actual, size

    def _download_target(self):
        if cached_package(self.paths, self.to_version) is not None:
            return
        self._download_assets(self.plan.release or {}, self.to_version, step='download')

    def _prepare(self):
        """Keep a verified package of the installed version; return whether one is kept."""
        from_version = self.from_version
        if not from_version:
            return False
        if cached_package(self.paths, from_version) is not None:
            return True
        release = find_release(self.state, from_version)
        deb = (release or {}).get('deb') or {}
        if release is not None and release.get('installable') and deb.get('url'):
            self._download_assets(release, from_version, step='prepare')
            self._verify_package(from_version, step='prepare', preflight=False)
            return True
        self.log.warning('No package of the installed version %s; an unhealthy update '
                         'cannot be rolled back automatically', from_version)
        return False

    # -- backup and snapshot ---------------------------------------------
    def _backup(self):
        config = Path(self.paths.config)
        if not config.exists():
            return
        ensure_private_dir(Path(self.paths.backups), code='backup_failed', step='backup')
        now = self.utcnow()
        directory = Path(self.paths.backups) / (now.strftime('%Y%m%dT%H%M%SZ') + '-' + self.tx['id'][:8])
        try:
            directory.mkdir(mode=0o700)
            with configuration_lock(config):
                data = config.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            write_private(directory / 'qmi-supervisor.ini', data)
            write_private(directory / 'meta.json', json.dumps({
                'schema_version': 1, 'id': self.tx['id'],
                'created_at_utc': state_module.stamp(now), 'from': self.from_version,
                'to': self.to_version, 'sha256': digest,
            }, sort_keys=True).encode('utf-8'))
        except OSError as error:
            raise UpdateError('backup', 'backup_failed',
                              getattr(error, 'strerror', None) or str(error)) from None
        self.backup_dir, self.backup_sha = directory, digest
        self._prune_backups()

    def _prune_backups(self):
        try:
            entries = [entry for entry in Path(self.paths.backups).iterdir()
                       if entry.is_dir() and not entry.is_symlink()
                       and BACKUP_PATTERN.fullmatch(entry.name)]
        except OSError as error:
            self.log.warning('Cannot read the configuration backups: %s', error)
            return
        # Keep this transaction's backup even if a backward clock step made its
        # name sort older than the rest: the rollback restores from it.
        current = self.backup_dir.name if self.backup_dir is not None else None
        others = [entry for entry in sorted(entries, key=lambda item: item.name, reverse=True)
                  if entry.name != current]
        for entry in others[KEEP_BACKUPS - 1:]:
            shutil.rmtree(entry, ignore_errors=True)

    def _snapshot(self):
        self.online_before = health.online_profiles(self.paths.runtime, self.clock())
        self.web_before = health.web_running(self.paths.runtime, self.clock())
        self.tx['online_before'] = sorted(self.online_before)
        self.tx['web_before'] = self.web_before
        self.log.info('Before the update: online %s, web %s',
                      ', '.join(sorted(self.online_before)) or 'none', self.web_before)

    # -- install, health and rollback ------------------------------------
    def _install(self):
        deb = Path(self.paths.cache) / self.to_version / deb_name(self.to_version)
        args = ['apt-get', 'install', '-y', '--no-install-recommends',
                '-o', 'DPkg::Lock::Timeout=120',
                '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold']
        if self.plan.downgrade or self.reason == 'repair':
            args.append('--allow-downgrades')
        args += ['--', str(deb)]
        self.apt_started = self.clock()
        result, timed_out = self._run(args, timeout=APT_TIMEOUT, env=command_env(APT_ENV))
        self.apt_end = self.clock()
        after = dpkg_status(self.runner)
        detail = self._apt_detail(result, timed_out)
        if self.reason == 'repair':
            if after != ('ii', self.to_version):
                try:
                    self.runner(['dpkg', '-i', '--force-confdef', '--force-confold', str(deb)],
                                timeout=APT_TIMEOUT, env=command_env(APT_ENV))
                except (OSError, subprocess.SubprocessError) as error:
                    self.log.warning('dpkg -i failed: %s', error)
            if dpkg_status(self.runner) != ('ii', self.to_version):
                raise UpdateError('install', 'repair_failed', detail or 'the package could not be repaired')
            self.changed = True
            return
        if after == ('ii', self.to_version):
            self.changed = True
            return
        if after == self.installed_before:
            output = (result.stdout or '') + (result.stderr or '')
            if any(marker in output for marker in LOCK_MARKERS):
                raise UpdateError('install', 'dpkg_busy', detail)
            raise UpdateError('install', 'install_timeout' if timed_out else 'install_failed', detail)
        self.changed = True
        raise UpdateError('install', 'install_timeout' if timed_out else 'install_failed', detail)

    def _restore_config(self):
        if self.backup_dir is None:
            return False
        config = Path(self.paths.config)
        source = Path(self.backup_dir) / 'qmi-supervisor.ini'
        try:
            data = source.read_bytes()
        except OSError:
            raise UpdateError('rollback', 'rollback_failed',
                              'the saved configuration is missing') from None
        with configuration_lock(config):
            try:
                current = config.read_bytes() if config.exists() else None
            except OSError:
                current = None
            if current is not None and hashlib.sha256(current).hexdigest() == self.backup_sha:
                return False
            if current is not None:
                try:
                    write_private(Path(self.backup_dir) / 'qmi-supervisor.ini.rejected', current)
                except OSError as error:
                    self.log.warning('Cannot keep the rejected configuration: %s', error)
            write_private(config, data, 0o600)
        return True

    def _start_service_if_needed(self):
        try:
            state = health.service_show(self.runner).get('ActiveState')
        except (OSError, subprocess.SubprocessError):
            state = None
        if state != 'active':
            try:
                self.runner(['systemctl', 'start', 'qmi-supervisor.service'],
                            timeout=SYSTEMCTL_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as error:
                self.log.warning('Cannot start qmi-supervisor.service: %s', error)

    def _rollback_failed(self, original, detail):
        # A failed rollback must never leave the service stopped by us.
        self._start_service_if_needed()
        self._mark('rollback', 'failed')
        return {'result': 'failed', 'step_failed': 'rollback', 'code': 'rollback_failed',
                'detail': f'after {original.code}: {detail}'}

    def _rollback(self, original):
        self._enter('rollback')
        self.log.warning('Rolling back to %s after %s', self.from_version, original.code)
        try:
            self.runner(['systemctl', 'stop', 'qmi-supervisor.service'], timeout=SYSTEMCTL_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as error:
            self.log.warning('Cannot stop qmi-supervisor.service: %s', error)
        try:
            self.config_restored = self._restore_config()
        except UpdateError as failure:
            return self._rollback_failed(original, failure.detail or failure.code)
        version = self.from_version
        record = cached_package(self.paths, version)
        deb = Path(self.paths.cache) / version / deb_name(version)
        try:
            actual = sha256_file(deb)
        except OSError:
            actual = None
        if record is None or actual != record.get('sha256'):
            self._start_service_if_needed()
            return self._rollback_failed(original, 'the kept package is missing or has changed')
        started = self.clock()
        args = ['apt-get', 'install', '-y', '--no-install-recommends',
                '-o', 'DPkg::Lock::Timeout=120',
                '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold',
                '--allow-downgrades', '--', str(deb)]
        result, timed_out = self._run(args, timeout=APT_TIMEOUT, env=command_env(APT_ENV))
        if dpkg_status(self.runner) != ('ii', version):
            try:
                self.runner(['dpkg', '-i', '--force-confdef', '--force-confold', str(deb)],
                            timeout=APT_TIMEOUT, env=command_env(APT_ENV))
            except (OSError, subprocess.SubprocessError) as error:
                self.log.warning('dpkg -i failed: %s', error)
        if dpkg_status(self.runner) != ('ii', version):
            self._start_service_if_needed()
            return self._rollback_failed(original, self._apt_detail(result, timed_out)
                                         or 'the previous package could not be installed')
        self._start_service_if_needed()
        try:
            failure = health.wait_service(
                self.paths.runtime, self.runner, target=version, after=started,
                web_required=self.web_before, clock=self.clock, sleep=self.sleep, stop=self.stop)
        except Interrupted:
            return self._rollback_failed(original, 'the updater stopped during the rollback')
        if failure is not None:
            return self._rollback_failed(original, failure[2])
        online = sorted(health.online_profiles(self.paths.runtime, self.clock()))
        detail = original.detail
        suffix = 'modems online after the rollback: ' + (', '.join(online) or 'none')
        detail = f'{detail}; {suffix}' if detail else suffix
        self._done('rollback')
        return {'result': 'rolled_back', 'step_failed': original.step,
                'code': original.code, 'detail': detail}

    def _wait_health(self):
        self._enter('restart')
        moved = []
        # The health budget starts when apt-get and its postinst have finished.
        healthy_from = self.apt_end or self.apt_started
        remaining = (healthy_from + health.SERVICE_SECONDS + health.STABLE_SECONDS
                     - self.clock())
        if remaining > 0:
            self.tx['wait_until_utc'] = state_module.stamp(
                self.utcnow() + timedelta(seconds=remaining))
            self._save()

        def on_stage(stage):
            if stage == 'health' and not moved:
                moved.append(True)
                self._mark('restart', 'done')
                self._enter('health')

        failure = health.wait_service(
            self.paths.runtime, self.runner, target=self.to_version, after=self.apt_started,
            deadline_from=healthy_from,
            web_required=self.web_before, clock=self.clock, sleep=self.sleep, stop=self.stop,
            on_stage=on_stage)
        self.tx['wait_until_utc'] = None
        if failure is not None:
            if failure[0] == 'health' and not moved:
                self._mark('restart', 'done')
                self._enter('health')
            raise UpdateError(failure[0], failure[1], failure[2])
        if not moved:
            self._mark('restart', 'done')
            self._enter('health')
        self._done('health')

    def _wait_modems(self):
        if not (self.update.auto_rollback and self.online_before
                and self.reason != 'repair'):
            return
        deadline = self.apt_end + health.MODEMS_SECONDS
        remaining = deadline - self.clock()
        if remaining > 0:
            self.tx['wait_until_utc'] = state_module.stamp(
                self.utcnow() + timedelta(seconds=remaining))
            self._save()
        missing = health.wait_modems(self.paths.runtime, self.online_before, deadline=deadline,
                                     clock=self.clock, sleep=self.sleep, stop=self.stop)
        self.tx['wait_until_utc'] = None
        self._save()
        if missing:
            raise UpdateError('health', 'modems_not_back',
                              f'profile {", ".join(missing)} was ONLINE before the update '
                              f'and not within {health.MODEMS_SECONDS} s')
        self.log.info('Modems online again: %s', ', '.join(sorted(self.online_before)))

    # -- transaction ------------------------------------------------------
    def _transaction(self):
        self._enter('resolve')
        self.installed_before = dpkg_status(self.runner)
        self.plan = resolve(self.paths, self.settings, self.state, self.to_version,
                            reason=self.reason, installed=self.installed_before)
        # A host that must verify signatures but ships no keyring cannot verify any
        # release: stop before downloading rather than after, and say why.
        if (self.update.require_signature and self.reason in ('manual', 'auto')
                and not Path(self.keyring).is_file()):
            raise UpdateError('resolve', 'no_keyring',
                              f'the signing keyring {Path(self.keyring).name} is missing')
        self.from_version = self.installed_before[1] if self.installed_before else None
        self.tx['from'] = self.from_version
        self.log.info('Update %s -> %s (%s) started', self.from_version, self.to_version, self.reason)
        self._done('resolve')

        if self.reason in ('manual', 'auto'):
            self._enter('download')
            self._download_target()
            self._check_stop('download')
            self._done('download')

        self._enter('verify')
        self.signed_by, _, _ = self._verify_package(
            self.to_version, step='verify', preflight=self.reason != 'repair')
        self.tx['signed_by'] = self.signed_by
        self._done('verify')

        if self.reason != 'repair':
            self._enter('prepare')
            self.rollback_available = self._prepare()
            self.tx['rollback_available'] = self.rollback_available
            self._done('prepare')

        self._enter('backup')
        self._backup()
        self._done('backup')

        self._enter('snapshot')
        self._snapshot()
        self._done('snapshot')

        self._enter('install')
        self._install()
        self._check_stop('install')
        self._done('install')

        self._wait_health()
        self._wait_modems()

    def _should_rollback(self, error):
        return (self.changed and self.update.auto_rollback and self.rollback_available
                and self.reason in ('manual', 'auto') and self.from_version is not None)

    def _handle_failure(self, error):
        self._mark(error.step, 'failed')
        if not self._should_rollback(error):
            return {'result': 'failed', 'step_failed': error.step, 'code': error.code,
                    'detail': error.detail}
        try:
            return self._rollback(error)
        except Interrupted as stopped:
            self._mark('rollback', 'failed')
            return {'result': 'interrupted', 'step_failed': stopped.step,
                    'code': 'interrupted', 'detail': None}
        except Exception as crash:  # noqa: BLE001 - recording must still happen
            self.log.exception('Unexpected error during the rollback')
            self._mark('rollback', 'failed')
            return {'result': 'failed', 'step_failed': 'rollback', 'code': 'rollback_failed',
                    'detail': f'after {error.code}: {type(crash).__name__}: {crash}'}

    def _rollback_state(self, installed_after):
        # Keep what was kept before: a failed attempt that changed nothing must
        # not destroy the package of the previous version.
        rollback = dict(self.state.get('rollback') or {})
        if not rollback:
            rollback = {'available': False, 'version': None}
        if installed_after is not None and self.from_version and installed_after == self.to_version:
            record = cached_package(self.paths, self.from_version)
            if record is not None:
                rollback = {'available': True, 'version': self.from_version,
                            'sha256': record.get('sha256'),
                            'signed_by': record.get('signed_by'),
                            'cached_at_utc': record.get('verified_at_utc')}
        # The kept version stops being a rollback target once it is installed again.
        if rollback.get('version') == installed_after:
            rollback = {'available': False, 'version': None}
        return rollback

    def _rollback_ready(self, installed_after):
        return rollback_ready(self.paths, self.state, installed_after or self.from_version)

    def _prune_cache(self, installed_after):
        repair = self.state.get('repair') or {}
        keep = {version for version in
                (installed_after, (self.state.get('rollback') or {}).get('version'),
                 repair.get('target')) if version}
        cache = Path(self.paths.cache)
        try:
            entries = list(cache.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir() and VERSION_PATTERN.fullmatch(entry.name):
                    if entry.name not in keep:
                        shutil.rmtree(entry, ignore_errors=True)
                elif entry.is_file() and entry.name.endswith('.part'):
                    entry.unlink()
            except OSError:
                continue
        for version in keep:
            try:
                for leftover in (cache / version).glob('*.part'):
                    leftover.unlink()
            except OSError:
                continue

    def _finish(self, outcome):
        at = self.utcnow()
        try:
            installed = dpkg_status(self.runner)
        except Exception as error:  # noqa: BLE001 - recording must not raise here
            self.log.error('Cannot read the package state: %s', error)
            installed = None
        installed_after = installed[1] if installed else None
        for step in self.tx['steps']:
            if step['state'] == 'pending':
                step['state'] = 'skipped'
        fields = {
            'id': self.tx['id'], 'request_id': self.tx.get('request_id'),
            'started_at_utc': self.tx.get('started_at_utc'),
            'at_utc': state_module.stamp(at),
            'duration_s': max(0, int((at - self.started_at).total_seconds())),
            'from': self.tx.get('from'), 'to': self.to_version, 'reason': self.reason,
            'result': outcome['result'], 'step_failed': outcome.get('step_failed'),
            'code': outcome.get('code'), 'detail': outcome.get('detail'),
            'rollback_available': bool(self.rollback_available),
            'installed_after': installed_after, 'config_restored': bool(self.config_restored),
            'signed_by': self.signed_by,
        }
        record = history.make_record(**fields)

        try:
            history.append(self.paths, record)
        except OSError as error:
            self.log.error('Cannot append to the update history: %s', error)

        document = self.state
        try:
            document['in_progress'] = None
            document['last_result'] = {**record, 'steps': [dict(step) for step in self.tx['steps']]}
        except Exception as error:  # noqa: BLE001
            self.log.error('Cannot update the recorded result: %s', error)
        try:
            document['history'] = list(reversed(history.read(self.paths, limit=10)))
        except Exception as error:  # noqa: BLE001
            self.log.error('Cannot read the update history: %s', error)
        try:
            document['current_version'] = installed_after or document.get('current_version')
        except Exception as error:  # noqa: BLE001
            self.log.error('Cannot update the recorded version: %s', error)
        try:
            document['rollback'] = self._rollback_state(installed_after)
        except Exception as error:  # noqa: BLE001
            self.log.error('Cannot record the rollback package: %s', error)
        try:
            document['rollback_ready'] = self._rollback_ready(installed_after)
        except Exception as error:  # noqa: BLE001
            self.log.error('Cannot record whether a rollback is ready: %s', error)
        if record['result'] == 'success':
            # A fully installed package leaves nothing to repair, whatever the action was.
            document['repair'] = None
        elif self.reason == 'repair':
            repair = dict(document.get('repair') or {})
            package = Path(self.paths.cache) / self.to_version / deb_name(self.to_version)
            # A person may run the full configure; the updater itself never does.
            manual = ('sudo dpkg --configure ' + '-' + 'a'
                      f' && sudo apt install {package}')
            repair.update({'needed': True, 'attempted': True, 'manual_command': manual})
            document['repair'] = repair
        elif installed is None or installed[0] != 'ii':
            # A transaction that left dpkg without a fully configured package
            # (interrupted, failed install or failed rollback) is repaired from
            # the cached verified package by the next check.
            repair = dict(document.get('repair') or {})
            repair.update({
                'needed': True, 'status': installed[0] if installed else None,
                'target': (installed[1] if installed else None) or self.to_version,
                'attempted': False,
                'detected_at_utc': repair.get('detected_at_utc') or state_module.stamp(at),
                'manual_command': None,
            })
            document['repair'] = repair
        try:
            self._prune_cache(installed_after)
        except Exception as error:  # noqa: BLE001
            self.log.error('Cannot prune the update cache: %s', error)
        try:
            state_module.write_state(self.paths.state, document, now=self.utcnow())
        except OSError as error:
            self.log.error('Cannot write the update state: %s', error)

        if record['result'] == 'success':
            self.log.info('Update to %s succeeded', self.to_version)
        else:
            self.log.error('Update to %s %s at %s: %s: %s', self.to_version,
                           record['result'], record.get('step_failed'),
                           record.get('code'), record.get('detail'))
        return record

    def run(self, version, *, reason, request_id=None, unit=None):
        """Run the transaction; the caller already holds the update lock."""
        self.to_version = version
        self.reason = reason
        self.started_at = self.utcnow()
        self.state = state_module.load_state(self.paths.state) or state_module.new_state(
            runtime_version.installed_version(), self.started_at)
        self.tx = {
            'schema_version': 1, 'id': uuid.uuid4().hex, 'request_id': request_id,
            'from': None, 'to': version, 'reason': reason, 'step': 'resolve',
            'steps': [{'id': step, 'state': 'pending'} for step in history.STEPS],
            'started_at_utc': state_module.stamp(self.started_at),
            'updated_at_utc': state_module.stamp(self.started_at),
            'wait_until_utc': None, 'pid': os.getpid(), 'boot_id': read_boot_id(),
            'unit': unit, 'rollback_available': False, 'signed_by': None,
            'online_before': [], 'web_before': False,
        }
        if reason in ('rollback', 'repair'):
            self._mark('download', 'skipped')
        if reason == 'repair':
            self._mark('prepare', 'skipped')
        self.state['in_progress'] = self.tx
        self._save()
        try:
            self._check_stop('resolve')
            self._transaction()
            outcome = {'result': 'success', 'step_failed': None, 'code': None, 'detail': None}
        except Interrupted as error:
            self._mark(error.step, 'failed')
            outcome = {'result': 'interrupted', 'step_failed': error.step,
                       'code': 'interrupted', 'detail': None}
        except UpdateError as error:
            outcome = self._handle_failure(error)
        except Exception as error:  # noqa: BLE001 - a crash during health still rolls back
            self.log.exception('Unexpected updater error')
            outcome = self._handle_failure(UpdateError(
                self.current_step, 'internal_error', f'{type(error).__name__}: {error}'))
        return self._finish(outcome)
