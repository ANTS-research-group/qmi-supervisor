"""Remove a managed installation without claiming ownership of unrelated host state."""
from contextlib import contextmanager
from configparser import ConfigParser, Error as IniError
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from modules.deployment.install_state import load, save
from modules.device.device import scan_devices

SERVICE = 'qmi-supervisor.service'
MANAGER = 'ModemManager.service'
QMI_NETWORK_RULE = ('# QMI-Supervisor owns QMI USB network interfaces only.\n'
                    'SUBSYSTEM=="net", DRIVERS=="qmi_wwan", ENV{NM_UNMANAGED}="1"\n')
_CAPTURE = re.compile(r'(?:static_stats_\d{8}\.csv|dynamic_stats_(?:v2_)?\d{8}\.csv|'
                      r'stats\.(?:json|tmp)|[A-Za-z0-9_.-]+_qmi\.log)\Z')
_PACKAGE = re.compile(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?\Z')
_PROTECTED = {'/', '/bin', '/sbin', '/lib', '/lib64', '/usr', '/usr/local', '/etc',
              '/var', '/var/lib', '/var/log', '/opt', '/home', '/root', '/tmp',
              '/run', '/srv', '/mnt', '/media', '/dev', '/proc', '/sys'}
_APT_POLICY = ['-o', 'APT::Get::AutomaticRemove=false']


def _plain_path(path, *, allow_leaf_symlink=False):
    """Never traverse links while deleting a receipt-owned path."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Unsafe uninstall path: {path}')
    for component in (path, *path.parents):
        if component.is_symlink() and not (allow_leaf_symlink and component == path):
            raise ValueError(f'Refusing symlink during uninstall: {component}')
    return path


def _remove(path, *, tree=False):
    path = _plain_path(path)
    if path.is_dir():
        if tree:
            shutil.rmtree(path)
        else:
            path.rmdir()
    else:
        path.unlink(missing_ok=True)


def _write_file(path, content, mode):
    path = _plain_path(path, allow_leaf_symlink=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _supervisor_lock(path):
    """Use an existing lock inode; an uninstall must not create an unowned lock."""
    path = _plain_path(path)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield
        return
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f'Invalid supervisor lock: {path}')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A supervisor process still owns the lock; installation and recovery state '
                             'were retained. Stop that instance and retry uninstall.') from None
        yield
    finally:
        os.close(descriptor)


def _stopped(runner):
    result = runner(['systemctl', 'is-active', '--quiet', SERVICE], check=False)
    if result.returncode == 0:
        return False
    if result.returncode not in (3, 4):
        raise ValueError('Cannot verify that qmi-supervisor is stopped; no application files were removed.')
    result = runner(['systemctl', 'show', SERVICE, '--property=ActiveState', '--value'], check=False)
    state = (result.stdout or '').strip()
    if state in ('inactive', 'failed'):
        return True
    # An unloaded/removed unit has no active state and systemctl reports it missing.
    if result.returncode and not state:
        return True
    raise ValueError(f'QMI service has not finished stopping (state={state or "unknown"}); retry uninstall.')


def _pending_recovery(paths):
    pending = [path for path in (paths.checkpoint, paths.attach_checkpoint) if path.exists() or path.is_symlink()]
    runtime = paths.runtime/'modems'
    if runtime.exists():
        pending.extend(path for path in runtime.glob('*/connection-state*.json') if path.exists() or path.is_symlink())
    pending.extend(path for path in (paths.runtime/'ownership').glob('*/connection-state*.json')
                   if path.exists() or path.is_symlink())
    pending.extend(path for path in (paths.runtime/'ownership').glob('*/migration.json')
                   if path.exists() or path.is_symlink())
    if pending:
        names = ', '.join(str(path) for path in pending)
        raise ValueError('Unconfirmed modem/network recovery remains in '+names+'. Application and receipt '
                         'were retained. Reconnect the modem and run the supervisor to complete recovery, '
                         'stop it cleanly, then retry uninstall. No checkpoint was erased.')


def _backup_paths(paths, receipt):
    artifacts = receipt.get('artifacts', {}) if receipt else {}
    output = []
    for kind, base, suffix in (('app_backups', paths.app, r'\.(?:previous|failed)-'),
                               ('config_backups', paths.config, r'\.bak-')):
        pattern = re.compile(re.escape(base.name)+suffix+r'\d{8}-\d{6}-\d{6}\Z')
        for value in artifacts.get(kind, []):
            candidate = _plain_path(value)
            if candidate.parent != base.parent or not pattern.fullmatch(candidate.name):
                raise ValueError(f'Refusing backup outside the recorded project family: {candidate}')
            output.append((candidate, kind == 'app_backups'))
    return output


def _data_paths(paths, receipt):
    output = []
    for entry in receipt.get('data_directories', []) if receipt else []:
        path = _plain_path(entry['path'])
        if str(path) in _PROTECTED and not entry['existed']:
            raise ValueError(f'Refusing to purge a shared top-level directory: {path}')
        output.append((path, entry['existed']))
    discovered = [paths.state]
    # Web/direct INI edits can change captures after the last provisioning transaction.
    # Read just this option: malformed unrelated settings must not block uninstall.
    parser = ConfigParser(interpolation=None)
    try:
        with paths.config.open() as stream:
            parser.read_file(stream)
        sections = {'telemetry', *('telemetry:'+section.split(':', 1)[1]
                    for section in parser.sections() if section.startswith(('modem:', 'telemetry:')))}
        base_directory = parser.get('telemetry', 'directory', fallback='').strip()
        for section in sorted(sections):
            directory = parser.get(section, 'directory', fallback=base_directory).strip()
            if directory:
                path = Path(directory)
                root = Path(os.path.abspath(path if path.is_absolute() else paths.app / path))
                if section.startswith('telemetry:'):
                    from modules.runtime.fleet_config import valid_profile
                    identifier = section.split(':', 1)[1]
                    if valid_profile(identifier):
                        discovered.append(root/'profiles'/identifier)
                else:
                    discovered.append(root)
    except (OSError, IniError, UnicodeError, ValueError):
        pass
    known = {path for path, _ in output}
    for path in discovered:
        path = _plain_path(path)
        if path not in known:
            output.append((path, True))
            known.add(path)
            if receipt is not None:
                receipt['data_directories'].append({'path': str(path), 'existed': True})
    if any(path == paths.app or path in paths.app.parents for path, _ in output):
        raise ValueError('A capture directory contains the application directory. Move the captures and '
                         'configure a separate capture directory before uninstalling; no files were removed.')
    return output


def _purge_data(path, existed):
    if not path.exists():
        return
    if not path.is_dir():
        raise ValueError(f'Capture directory is no longer a directory: {path}')
    if not existed:
        _remove(path, tree=True)
        return
    # A preexisting directory can contain other users' files, even inside modem_*.
    for modem in path.iterdir():
        if not modem.name.startswith('modem_') or modem.is_symlink() or not modem.is_dir():
            continue
        for capture in modem.iterdir():
            if _CAPTURE.fullmatch(capture.name) and not capture.is_symlink() and capture.is_file():
                capture.unlink()
        try:
            modem.rmdir()
        except OSError:
            pass  # Non-QMI files or subdirectories remain deliberately.


def _package_candidates(receipt, runner):
    owned = set(receipt.get('packages', [])) if receipt else set()
    if any(not isinstance(name, str) or not _PACKAGE.fullmatch(name) for name in owned):
        raise ValueError('Invalid package ownership in installation receipt')
    installed = _purgeable_packages(runner) if owned else set()
    candidates = sorted(owned & installed)
    return candidates, installed


def _purgeable_packages(runner):
    """Include rc and partial states so a failed dpkg purge remains retryable."""
    result = runner(['dpkg-query', '-W', '-f=${binary:Package}\\t${Status}\\n'])
    if result.returncode:
        raise ValueError('Cannot inspect package state before purge')
    output = set()
    for line in (result.stdout or '').splitlines():
        if not line:
            continue
        parts = line.split('\t', 1)
        if len(parts) != 2 or not _PACKAGE.fullmatch(parts[0]):
            raise ValueError('Invalid dpkg package inventory before purge')
        status = parts[1].split()
        if len(status) != 3:
            raise ValueError('Invalid dpkg status before purge')
        if status[-1] in {'installed', 'config-files', 'unpacked', 'half-installed',
                          'half-configured', 'triggers-awaited', 'triggers-pending'}:
            output.add(parts[0])
    return output


def _apt_preflight(candidates, installed, runner):
    if not candidates:
        return
    result = runner(['apt-get', '--simulate', 'purge', *_APT_POLICY, '--', *candidates])
    allowed = set(candidates)
    removals = set()
    for line in (result.stdout or '').splitlines():
        match = re.match(r'^\s*(Remv|Purg|Inst|Conf)\s+(\S+)', line)
        if not match:
            continue
        action, name = match.groups()
        if action in ('Inst', 'Conf'):
            raise ValueError(f'APT purge would install/configure {name}; refusing the transaction. '
                             'The installation receipt was retained.')
        variants = {item for item in installed if item.split(':')[0] == name}
        accounted = name in allowed or (':' not in name and variants and variants <= allowed)
        if not accounted:
            raise ValueError(f'APT purge would remove unrelated package {name}; refusing the transaction. '
                             'No packages were removed; the installation receipt was retained.')
        removals.add(name)
    if not removals:
        raise ValueError('APT simulation did not report any package removals; refusing an unverifiable purge.')


def _restore_network_rule(paths, receipt, runner):
    baseline = receipt.get('network_rule') if receipt else None
    if baseline and baseline.get('content') == QMI_NETWORK_RULE:
        # An upgrade may have first observed a rule left by an older installation.
        # The project's own generated rule must never be restored as administrator policy.
        if paths.rules.is_file() and paths.rules.read_text() == QMI_NETWORK_RULE:
            _remove(paths.rules)
        else:
            return
    elif baseline and baseline['changed']:
        if baseline['existed']:
            _write_file(paths.rules, baseline['content'], baseline['mode'])
        else:
            _remove(paths.rules)
    elif receipt is None and paths.rules.is_file() and paths.rules.read_text() == QMI_NETWORK_RULE:
        _remove(paths.rules)
    else:
        return
    runner(['udevadm', 'control', '--reload-rules'])
    for device in scan_devices():
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', device.interface):
            raise ValueError('Invalid QMI interface name during udev restoration')
        runner(['udevadm', 'trigger', '--action=change', '/sys/class/net/'+device.interface])
    runner(['udevadm', 'settle', '--timeout=10'])


def _restore_manager(receipt, runner):
    baseline = receipt.get('modem_manager', {}) if receipt else {}
    if not baseline.get('changed'):
        return
    # Unmask temporarily so enable/disable/start/stop can reproduce the original state.
    runner(['systemctl', 'unmask', MANAGER])
    runner(['systemctl', 'unmask', '--runtime', MANAGER])
    enabled = ['systemctl', 'enable' if baseline['enabled'] else 'disable']
    if baseline.get('enabled_state') == 'enabled-runtime':
        enabled.append('--runtime')
    runner([*enabled, MANAGER])
    runner(['systemctl', 'start' if baseline['active'] else 'stop', MANAGER])
    if baseline['masked']:
        runtime = ['--runtime'] if baseline.get('enabled_state') == 'masked-runtime' else []
        runner(['systemctl', 'mask', *runtime, MANAGER])


def _restore_resolver(paths, receipt):
    baseline = receipt.get('resolver_config', {}) if receipt else {}
    if not baseline.get('changed'):
        return
    path = _plain_path(paths.resolver, allow_leaf_symlink=True)
    kind = baseline['kind']
    if kind == 'absent':
        path.unlink(missing_ok=True)
    elif kind == 'file':
        _write_file(path, baseline['content'], baseline['mode'])
    elif kind == 'symlink':
        # Replace the link itself; never open or erase its target.
        descriptor, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
        os.close(descriptor)
        os.unlink(temporary)
        try:
            os.symlink(baseline['target'], temporary)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    else:
        raise ValueError('Invalid resolver baseline in installation receipt')


def _clean_runtime(paths):
    for name in ('status.json', 'status.tmp', 'web-sessions.json'):
        _remove(paths.runtime / name)
    for checkpoint in (paths.checkpoint, paths.attach_checkpoint):
        _remove(checkpoint.with_suffix('.tmp'))
    if paths.runtime.exists():
        for temporary in paths.runtime.iterdir():
            if re.fullmatch(r'\.web-sessions\.json\.[A-Za-z0-9_-]+', temporary.name):
                _remove(temporary)
        try:
            paths.runtime.rmdir()
        except OSError:
            pass


def _remove_application(app, data, purge):
    """Leave preserved captures in place when an INI stores them inside the app."""
    if not app.exists():
        return
    preserved = {path for path, existed in data
                 if path.exists() and app in path.parents and (not purge or existed)}
    if not preserved:
        _remove(app, tree=True)
        return

    def prune(directory):
        for child in directory.iterdir():
            if child in preserved:
                continue
            if any(child in path.parents for path in preserved):
                prune(child)
            elif child.is_symlink():
                child.unlink()
            else:
                _remove(child, tree=True)
        try:
            directory.rmdir()
        except OSError:
            pass
    prune(app)
    print(f'Preserved capture contents inside {app}; application code and commands were removed.')


def _remove_receipt(paths):
    # The receipt directory may contain interrupted-bootstrap evidence; only remove known files.
    for name in ('bootstrap-packages.before', 'bootstrap-packages.after'):
        _remove(paths.manifest.parent / name)
    _remove(paths.manifest)
    try:
        paths.manifest.parent.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        print(f'Preserved unrecognized files in {paths.manifest.parent}')


def uninstall(paths, args, runner=None):
    """Remove QMI-owned deployment files; --purge additionally removes recorded data/packages."""
    if runner is None:
        from modules.deployment.provisioning import run
        runner = run
    purge, dry_run = bool(getattr(args, 'purge', False)), bool(getattr(args, 'dry_run', False))
    receipt = load(paths)
    progress = receipt.setdefault('uninstall', {}) if receipt is not None else {}
    if not isinstance(progress, dict) or any(type(value) is not bool for value in progress.values()):
        raise ValueError('Invalid uninstall progress in installation receipt')
    for path in (paths.app, paths.config, paths.unit, paths.command, paths.uninstall_command,
                 paths.rules, paths.runtime, paths.lock):
        _plain_path(path)
    backups = _backup_paths(paths, receipt) if purge else []
    data = _data_paths(paths, receipt)
    candidates, installed = _package_candidates(receipt, runner) if purge else ([], set())
    if purge:
        _apt_preflight(candidates, installed, runner)
    print('Plan: uninstall' + (' --purge' if purge else ' (captures and APT packages preserved)'))
    print(f'  Stop/disable {SERVICE}; remove {paths.app}, {paths.config} and both installed commands.')
    print('  Restore recorded ModemManager, QMI udev and resolver baselines; retain shared drivers and system logs.')
    if receipt is None:
        print('  No installation receipt: prior manager state, package ownership and old backups are unknown. '
              'Unknown packages and backups will be preserved; purge removes only recognized QMI capture files.')
    if purge:
        print('  Recorded backup paths: '+str(len(backups))+'; recorded capture directories: '+str(len(data)))
        print('  Owned packages eligible for purge: '+(', '.join(candidates) or 'none'))
    else:
        print(f'  Ownership receipt retained at {paths.manifest}; run the source uninstall script with --purge later.')
    if dry_run:
        print('Dry run: no files, services or packages were changed.')
        return 0
    if os.geteuid() != 0:
        raise ValueError('Run uninstall with sudo; --dry-run does not require root')
    runner(['systemctl', 'stop', SERVICE], check=False)
    if not _stopped(runner):
        raise ValueError('QMI service is still active; application and recovery state were retained. Retry after stopping it.')
    with _supervisor_lock(paths.lock):
        _pending_recovery(paths)
        if receipt is not None:
            save(paths, receipt)  # Retain newly discovered capture paths before removing the INI.
        runner(['systemctl', 'disable', SERVICE], check=paths.unit.exists())
        for path in (paths.unit, paths.command, paths.uninstall_command, paths.config):
            _remove(path)
        runner(['systemctl', 'daemon-reload'])
        runner(['systemctl', 'reset-failed', SERVICE], check=False)
        _clean_runtime(paths)
        if not progress.get('network_restored'):
            _restore_network_rule(paths, receipt, runner)
            progress['network_restored'] = True
            if receipt is not None:
                save(paths, receipt)
        if not progress.get('manager_restored'):
            _restore_manager(receipt, runner)
            progress['manager_restored'] = True
            if receipt is not None:
                save(paths, receipt)
        if not progress.get('resolver_restored'):
            _restore_resolver(paths, receipt)
            progress['resolver_restored'] = True
            if receipt is not None:
                save(paths, receipt)
        if purge:
            for backup, tree in backups:
                _remove(backup, tree=tree)
            for directory, existed in data:
                _purge_data(directory, existed)
        _remove_application(paths.app, data, purge)
        progress['application_removed'] = True
        if receipt is not None:
            save(paths, receipt)
        if purge and candidates:
            # Recheck just before package mutation; another APT transaction may have changed the plan.
            _apt_preflight(candidates, _purgeable_packages(runner), runner)
            progress['resolver_restored'] = False
            if receipt is not None:
                save(paths, receipt)
            runner(['apt-get', 'purge', '-y', *_APT_POLICY, '--', *candidates])
            remaining = set(candidates) & _purgeable_packages(runner)
            if remaining:
                raise ValueError('APT left owned packages installed: '+', '.join(sorted(remaining))+'. Retry --purge.')
            # Package maintainer scripts (notably resolvconf) may have rewritten resolv.conf.
            _restore_resolver(paths, receipt)
            progress['resolver_restored'] = True
            if receipt is not None:
                save(paths, receipt)
        _remove(paths.lock)
        if purge and receipt is not None:
            _remove_receipt(paths)
    print('Uninstalled'+(' and purged recorded QMI-owned data, backups and packages.' if purge else
                        '; captures, APT packages and ownership receipt were preserved.'))
    if receipt is None:
        print('Legacy installation: exact pre-install restoration cannot be established without an ownership receipt.')
    return 0
