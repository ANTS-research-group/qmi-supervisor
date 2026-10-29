"""Debian/Ubuntu provisioning and configuration, with an inspectable dry run."""
import argparse
from configparser import ConfigParser
from dataclasses import dataclass, replace, asdict
from datetime import datetime
from io import StringIO
import getpass
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import stat
import warnings

from modules.device.query_plan import GROUPS
from modules.runtime.settings import Settings, load_settings, validate_settings
from modules.runtime.fleet_config import Configuration, valid_profile
from modules.runtime.configuration import configuration_lock
from modules.runtime.ipc import revision
from modules.device.device import scan_devices
from modules.deployment import install_state

BASE_PACKAGES = ('python3', 'python3-gi', 'gir1.2-qmi-1.0', 'libqmi-utils',
                 'libqmi-proxy', 'iproute2', 'udev', 'kmod')
SERVICE = 'qmi-supervisor.service'
QMI_NETWORK_RULE = ('# QMI-Supervisor owns QMI USB network interfaces only.\n'
                    'SUBSYSTEM=="net", DRIVERS=="qmi_wwan", ENV{NM_UNMANAGED}="1"\n')


@dataclass(frozen=True)
class Paths:
    source: Path
    app: Path = Path('/opt/qmi-supervisor')
    config: Path = Path('/etc/qmi-supervisor.ini')
    unit: Path = Path('/etc/systemd/system/qmi-supervisor.service')
    command: Path = Path('/usr/local/sbin/qmi-supervisor-configure')
    state: Path = Path('/var/lib/qmi-supervisor')
    rules: Path = Path('/etc/udev/rules.d/85-qmi-supervisor.rules')
    runtime: Path = Path('/run/qmi-supervisor')

    @property
    def uninstall_command(self):
        return self.command.with_name('qmi-supervisor-uninstall')

    @property
    def manifest(self):
        return self.state.with_name(self.state.name + '-install') / 'manifest.json'

    @property
    def resolver(self):
        return self.config.parent / 'resolv.conf'

    @property
    def checkpoint(self):
        return self.runtime.parent / 'qmi-supervisor-state.json'

    @property
    def attach_checkpoint(self):
        return self.runtime.parent / 'qmi-supervisor-state-attach.json'

    @property
    def lock(self):
        return self.runtime.parent / 'qmi-supervisor.lock'


def run(args, *, check=True, input=None, timeout=None):
    if args[0] == 'apt-get':
        # Keep package progress visible on slow boards; package installation has no interactive prompts.
        simulation = any(option in {'--simulate', '-s', '--just-print', '--dry-run'} for option in args[1:])
        return subprocess.run(args, text=True, check=check, timeout=600 if timeout is None else timeout,
                              capture_output=simulation,
                              env=dict(os.environ, DEBIAN_FRONTEND='noninteractive', LC_ALL='C'))
    return subprocess.run(args, text=True, capture_output=True, check=check, input=input,
                          timeout=300 if timeout is None else timeout)


def supported_system(release=None, systemd=None):
    release = platform.freedesktop_os_release() if release is None else release
    families = {release.get('ID', ''), *release.get('ID_LIKE', '').split()}
    if not families.intersection({'debian', 'ubuntu'}):
        raise ValueError('This installer supports Debian/Ubuntu with systemd')
    if systemd is None:
        systemd = Path('/run/systemd/system').is_dir()
    if not systemd:
        raise ValueError('systemd must be running on the target (not a container/chroot without systemd)')
    return release.get('PRETTY_NAME', release.get('ID', 'Debian/Ubuntu'))


def render_config(settings, *, redact=False):
    parser = ConfigParser(interpolation=None)
    for section, values in asdict(settings).items():
        parser[section] = {
            key: ('<redacted>' if value else '<not set>')
            if redact and section == 'web' and key == 'password_hash'
            else str(value).lower() if isinstance(value, bool) else str(value)
            for key, value in values.items()}
    stream = StringIO()
    stream.write('# Managed by qmi-supervisor-configure; no hardware identity is required.\n')
    parser.write(stream)
    return stream.getvalue()


def candidate_validation(settings):
    """Validate a plan candidate, allowing a LAN listener that awaits its account.

    A non-loopback web interface requires an account password, but the installer
    creates that account after building the candidate. Apply revalidates the
    finished configuration; every other consumer still gets the strict rule.
    """
    try:
        return validate_settings(settings)
    except ValueError as error:
        if (settings.web.enabled and not settings.web.password_hash
                and str(error) == 'LAN access requires a web password'):
            return settings
        raise


def candidate_settings(args, paths):
    identifier = getattr(args, 'modem_profile', 'default')
    if not valid_profile(identifier):
        raise ValueError('Invalid modem profile identifier')
    if paths.config.exists():
        configuration = Configuration.load(paths.config)
        args._configuration_revision = configuration.revision
        settings = configuration.profiles.get(identifier, configuration.base)
    else:
        args._configuration_revision = None
        defaults = Settings()
        enable_web = getattr(args, 'web', None) != 'off'
        settings = replace(defaults, modem=replace(defaults.modem, mode='monitor', plmn='auto'),
            telemetry=replace(defaults.telemetry, enabled=False, directory=str(paths.state)),
            web=replace(defaults.web, enabled=enable_web, host='0.0.0.0'))
        configuration = Configuration(settings, {})
    args._configuration = configuration
    changes = {'modem': {}, 'network': {}, 'telemetry': {}, 'web': {}}
    if getattr(args, 'connection', None) is not None:
        changes['modem']['mode'] = 'connect' if args.connection == 'on' else 'monitor'
    for argument, section, key in (
        ('apn', 'modem', 'apn'), ('plmn', 'modem', 'plmn'), ('serial', 'modem', 'serial'), ('device', 'modem', 'device'),
        ('mode', 'modem', 'mode'), ('method', 'network', 'method'), ('dns', 'network', 'dns'),
        ('route_metric', 'network', 'route_metric'), ('collector_host', 'telemetry', 'collector_host'),
        ('connectivity_targets', 'network', 'connectivity_targets'),
        ('connectivity_interval', 'network', 'connectivity_interval'),
        ('connectivity_timeout', 'network', 'connectivity_timeout'),
        ('connectivity_failures', 'network', 'connectivity_failures'),
        ('connectivity_successes', 'network', 'connectivity_successes'),
        ('sample_interval', 'telemetry', 'interval'), ('telemetry_profile', 'telemetry', 'profile'),
        ('collection_budget', 'telemetry', 'collection_budget'), ('telemetry_indications', 'telemetry', 'indications'), ('web_host', 'web', 'host'), ('web_port', 'web', 'port'),
        ('web_user', 'web', 'username'),
        *(('telemetry_query_' + group, 'telemetry', 'query_' + group) for group in GROUPS),
    ):
        value = getattr(args, argument, None)
        if value is not None:
            if isinstance(value, str):
                value = value.strip()
            changes[section][key] = value
    if getattr(args, 'sample_interval', None) is not None and args.collection_budget is None:
        changes['telemetry']['collection_budget'] = min(settings.telemetry.collection_budget, args.sample_interval * 0.8)
    if getattr(args, 'web', None) is not None:
        changes['web']['enabled'] = args.web == 'on'
    if args.auto_modem:
        changes['modem'].update(device='auto', serial='')
    elif args.serial is not None:
        changes['modem']['device'] = 'auto'
    elif args.device is not None:
        changes['modem']['serial'] = ''
    if args.route_metric is not None and args.network_profile not in ('primary', 'backup'):
        changes['network']['route_policy'] = 'fixed'
    if args.network_profile:
        changes['network']['default_route'] = args.network_profile != 'no-default'
        if args.network_profile in ('primary', 'backup'):
            changes['network']['route_policy'] = args.network_profile
            if args.route_metric is None:
                changes['network']['route_metric'] = 100 if args.network_profile == 'primary' else 700
    if args.connectivity_check is not None:
        changes['network']['connectivity_check'] = args.connectivity_check == 'on'
    if args.telemetry is not None:
        changes['telemetry']['enabled'] = args.telemetry == 'on'
    if args.gps is not None:
        changes['telemetry']['gps'] = args.gps == 'on'
    if args.gps_source is not None:
        changes['telemetry']['gps_source'] = args.gps_source
    if args.throughput is not None:
        changes['telemetry']['throughput'] = args.throughput == 'on'
    sections = {name: replace(getattr(settings, name), **values) for name, values in changes.items()}
    return candidate_validation(replace(settings, **sections))


def ask(label, current, choices=None):
    suffix = f" ({'/'.join(choices)})" if choices else ''
    answer = input(f'{label}{suffix} [{current}]: ').strip() or str(current)
    if choices and answer not in choices:
        raise ValueError(f'{label}: choose one of {", ".join(choices)}')
    return answer


def interactive(settings):
    if not sys.stdin.isatty():
        raise ValueError('--interactive requires a terminal; use CLI options for unattended setup')
    modem = replace(settings.modem, apn=ask('APN', settings.modem.apn),
                    mode=ask('Mode', settings.modem.mode, ('connect', 'monitor')))
    selector = ask('Modem selection: auto or USB serial', modem.serial or 'auto')
    modem = replace(modem, device='auto', serial='' if selector == 'auto' else selector)
    profile = 'no-default' if not settings.network.default_route else settings.network.route_policy
    profile = ask('Cellular route', profile, ('fixed', 'backup', 'primary', 'no-default'))
    route_policy, route_metric = settings.network.route_policy, settings.network.route_metric
    if profile != 'no-default':
        route_policy = profile
        if profile in ('primary', 'backup') and profile != settings.network.route_policy:
            route_metric = 100 if profile == 'primary' else 700
        description = {'fixed':'Exact cellular route metric', 'primary':'Primary metric ceiling',
                       'backup':'Backup metric floor'}[profile]
        route_metric = int(ask(description, route_metric))
    network = replace(settings.network,
                      method=ask('IPv4 configuration', settings.network.method, ('qmi', 'dhcp')),
                      dns=ask('DNS management', settings.network.dns, ('auto', 'none', 'resolvectl', 'resolvconf')),
                      default_route=profile != 'no-default',
                      route_policy=route_policy, route_metric=route_metric,
                      connectivity_check=ask('Verify WWAN connectivity before preferring its route',
                                             'on' if settings.network.connectivity_check else 'off', ('on', 'off')) == 'on')
    telemetry = replace(settings.telemetry,
                        enabled=ask('Collect metrics', 'on' if settings.telemetry.enabled else 'off', ('on', 'off')) == 'on')
    interval = float(ask('Target metric interval (seconds)', telemetry.interval))
    telemetry = replace(telemetry, interval=interval, collection_budget=min(telemetry.collection_budget, interval*.8))
    web = replace(settings.web, enabled=ask('Web configuration and live status',
                                           'on' if settings.web.enabled else 'off', ('on', 'off')) == 'on')
    if web.enabled:
        web = replace(web, host=ask('Web IP (0.0.0.0 for every interface) or auto (localhost plus management LAN)',
                                    web.host))
    return candidate_validation(replace(settings, modem=modem, network=network, telemetry=telemetry, web=web))


def web_credentials(settings, args):
    """Read a password only during an actual apply, never while building a plan.

    Account credentials are stored only as a salted hash in the private
    configuration.
    """
    if args.dry_run or args.doctor:
        return settings
    explicit_prompt = args.set_web_password
    from_stdin = args.web_password_stdin
    automatic = settings.web.enabled and not settings.web.password_hash and sys.stdin.isatty()
    if not (explicit_prompt or from_stdin or automatic):
        return settings
    if explicit_prompt and not sys.stdin.isatty():
        raise ValueError('--set-web-password requires a terminal; use --web-password-stdin for unattended setup')
    web = settings.web
    if automatic and not (explicit_prompt or from_stdin or args.web_user is not None):
        web = replace(web, username=ask('Web username', web.username))
        # The password is asked next, so a LAN listener may still await it.
        candidate_validation(replace(settings, web=web))
    try:
        if from_stdin:
            # Bounded even if the producer sends an unbounded/multiline stream.
            password = sys.stdin.readline(258)
            if password.endswith('\r\n'):
                password = password[:-2]
            elif password.endswith('\n'):
                password = password[:-1]
        else:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                password = getpass.getpass('Web password (at least 8 characters): ')
                repeated = getpass.getpass('Repeat web password: ')
            if password != repeated:
                raise ValueError('Web passwords do not match; no configuration was changed')
        from modules.web.auth import hash_password
        encoded = hash_password(password)
    except getpass.GetPassWarning:
        raise ValueError('Cannot read a password without terminal echo; use --web-password-stdin') from None
    except (EOFError, KeyboardInterrupt):
        raise ValueError('Web password entry cancelled; no configuration was changed') from None
    finally:
        # No plaintext is put on args/settings or passed to the application plan.
        password = repeated = None
    return validate_settings(replace(settings, web=replace(web, password_hash=encoded)))


def password_changed(settings, args):
    """True when this run replaces the saved account password."""
    previous = getattr(args, '_configuration', None)
    return previous is not None and settings.web.password_hash != previous.base.web.password_hash


def packages_for(settings, runner=run, which=shutil.which):
    packages = list(BASE_PACKAGES)
    if settings.modem.mode == 'connect' or settings.web.enabled:
        if settings.network.method == 'dhcp' or settings.web.enabled:
            packages.append('udhcpc')
        if settings.network.dns == 'resolvectl':
            if not which('resolvectl') or runner(['resolvectl', 'status'], check=False).returncode:
                raise ValueError('dns=resolvectl requires an operational systemd-resolved service; use auto or none')
        if settings.network.dns == 'resolvconf':
            packages.append('resolvconf')
        elif settings.network.dns == 'auto':
            resolved = which('resolvectl') and runner(['resolvectl', 'status'], check=False).returncode == 0
            if not resolved:
                packages.append('resolvconf')
    if settings.telemetry.gps and settings.telemetry.gps_source == 'gpsd':
        packages.append('gpsd')
    return packages


def missing_packages(packages, runner=run):
    missing = []
    for package in packages:
        result = runner(['dpkg-query', '-W', '-f=${Status}', package], check=False)
        if result.returncode or result.stdout.strip() != 'install ok installed':
            missing.append(package)
    return missing


def write_atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def stage_application(paths):
    paths.app.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.qmi-supervisor-stage-', dir=paths.app.parent))
    try:
        shutil.copy2(paths.source / 'qmi_supervisor.py', stage / 'qmi_supervisor.py')
        for name in ('README.md', 'INSTALLATION.md', 'CONFIGURATION.md', 'LICENSE'):
            document = paths.source / name
            if document.is_file():
                shutil.copy2(document, stage / name)
        for name in ('modules', 'tools', 'config'):
            shutil.copytree(paths.source / name, stage / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        export = paths.source / 'frontend/out'
        if export.exists() or export.is_symlink():
            from modules.web.server import validate_static_export
            validate_static_export(export)
            # Only browser output is deployed: no Node, sources or build caches.
            shutil.copytree(export, stage / 'frontend/out')
        return stage
    except Exception:
        shutil.rmtree(stage)
        raise


def validate_web_export(settings, paths, action):
    root = (paths.source if action == 'install' else paths.app) / 'frontend/out'
    if settings.web.enabled or (action == 'install' and (root.exists() or root.is_symlink())):
        from modules.web.server import validate_static_export
        validate_static_export(root)


def network_manager_policy(args, paths):
    if args.network_manager is not None:
        return args.network_manager
    if paths.app.exists():
        return 'exclude-qmi' if paths.rules.exists() and paths.rules.read_text() == QMI_NETWORK_RULE else 'keep'
    return 'exclude-qmi'


def supports_live_configuration(paths):
    """Detect the installed manager without importing or executing application code."""
    try:
        unit = paths.unit.read_text()
        return ('Type=notify' in unit
                and unit == (paths.app/'tools/deployment/qmi-supervisor.service').read_text()
                and any(name in (paths.app/'qmi_supervisor.py').read_text()
                        for name in ('modules.runtime.processes', 'modules.runtime.fleet')))
    except OSError:
        return False


def can_restart_rollback(paths):
    """An older binary must not ignore journals created by the failed update."""
    try:
        ownership = paths.runtime/'ownership'
        pending = [*ownership.glob('*/connection-state*.json'), *ownership.glob('*/migration.json')]
        if not pending:
            return True
        return ('modules.runtime.fleet' in (paths.app/'qmi_supervisor.py').read_text() and
                'modules.runtime.ownership' in (paths.app/'modules/runtime/fleet.py').read_text() and
                'source_record' in (paths.app/'modules/runtime/ownership.py').read_text() and
                'relocated_device' in (paths.app/'modules/runtime/ownership.py').read_text() and
                'legacy_transport_unverified' in (paths.app/'modules/device/backend.py').read_text() and
                'dns_record' in (paths.app/'modules/network/manager.py').read_text())
    except (OSError, UnicodeError):
        return False


def resolver_snapshot(path):
    if path.is_symlink():
        return dict(existed=True, kind='symlink', mode=stat.S_IMODE(path.lstat().st_mode),
                    content=None, target=os.readlink(path), changed=False)
    if path.exists():
        if not path.is_file():
            raise ValueError(f'Resolver configuration is not a file or symlink: {path}')
        return dict(existed=True, kind='file', mode=stat.S_IMODE(path.stat().st_mode),
                    content=path.read_text(), target=None, changed=False)
    return dict(existed=False, kind='absent', mode=0o644, content=None, target=None, changed=False)


def tracking_state(paths, runner=run, *, modem_manager=None):
    """Capture host baselines once; subsequent transactions retain ownership."""
    state = install_state.load(paths)
    if state is None:
        if paths.rules.is_symlink():
            raise ValueError(f'Refusing to replace a symlinked udev rule: {paths.rules}')
        content = paths.rules.read_text() if paths.rules.exists() else None
        if modem_manager is None:
            enabled_state = runner(['systemctl', 'is-enabled', 'ModemManager.service'], check=False).stdout.strip()
            modem_manager = dict(
                active=runner(['systemctl', 'is-active', '--quiet', 'ModemManager.service'], check=False).returncode == 0,
                enabled=runner(['systemctl', 'is-enabled', '--quiet', 'ModemManager.service'], check=False).returncode == 0,
                masked=enabled_state in {'masked', 'masked-runtime'}, enabled_state=enabled_state, changed=False)
        state = install_state.new_state(modem_manager, dict(
            existed=content is not None, content=content,
            mode=stat.S_IMODE(paths.rules.stat().st_mode) if content is not None else 0o644, changed=False),
            resolver_snapshot(paths.resolver))
    elif 'resolver_config' not in state:
        state['resolver_config'] = resolver_snapshot(paths.resolver)
    install_state.consume_bootstrap(paths, state)
    return state


def print_plan(settings, paths, packages, args):
    print('Plan: ' + args.action)
    configuration = getattr(args, '_configuration', Configuration(settings, {})).update(
        getattr(args, 'modem_profile', 'default'), settings, validate=candidate_validation)
    args._effective_configuration = configuration
    print('  Modem profile: ' + getattr(args, 'modem_profile', 'default') +
          '; saved named profiles: ' + str(len(configuration.profiles)))
    print('  Modem: ' + ('automatic, accepts replacement devices' if settings.modem.device == 'auto' and not settings.modem.serial
                          else f'device={settings.modem.device}, serial={settings.modem.serial or "any"}'))
    print(f'  APN: {settings.modem.apn}; mode: {settings.modem.mode}; IPv4: {settings.network.method}')
    print(f'  Default route: {settings.network.default_route}; policy: {settings.network.route_policy}; '
          f'metric base: {settings.network.route_metric}; DNS: {settings.network.dns}')
    print('  WWAN connectivity: ' + ('verified before applying route preference' if settings.network.connectivity_check
                                    else 'verification disabled explicitly'))
    print(f'  Sampling: {1/settings.telemetry.interval:g} Hz, {settings.telemetry.profile}; web: {settings.web.enabled}')
    if settings.telemetry.profile == 'custom':
        print('  Query periods: ' + ', '.join(f'{group}={getattr(settings.telemetry, "query_" + group):g}s'
                                              for group in GROUPS))
    print('  Required packages: ' + ', '.join(packages))
    print(f'  Configuration: {paths.config}; application: {paths.app}')
    print('  ModemManager: ' + args.modem_manager + '; NetworkManager: ' + network_manager_policy(args, paths))
    print('  The Ethernet configuration and the NetworkManager service are retained.')
    print('  DNS=auto uses resolved/resolvconf only while the eligible cellular default is preferred.' if settings.network.dns == 'auto'
          else '  DNS policy is explicit in the generated configuration.')
    print('  Service: leave stopped' if args.start is False else (
        '  Service: enable and start/restart' if args.action == 'install' or args.start else
        '  Service: apply to active components; legacy installations restart'))
    if settings.web.enabled or password_changed(settings, args):
        # Plan only: the new hash reaches the INI when apply completes.
        print('  Web account: ' + settings.web.username + '; password ' +
              ('will be updated' if password_changed(settings, args) else
               'configured' if settings.web.password_hash else 'not configured'))
        if not settings.web.password_hash:
            print('  Create/reset a web account: sudo qmi-supervisor-configure --set-web-password --web-user admin')
    if args.dry_run and (args.set_web_password or args.web_password_stdin):
        print('  Password input is deferred: dry-run does not read or hash passwords.')
    print(f'  View the saved configuration at: {paths.config}')


def confirm_network_changes(settings, paths, args, runner=run):
    """Report planned networking effects and require one explicit acknowledgement."""
    from modules.deployment.preflight import inspect_host, print_findings
    findings = inspect_host(settings, paths, args, runner=runner)
    print_findings(findings)
    if not findings or args.dry_run or getattr(args, 'accept_network_changes', False):
        return
    if not sys.stdin.isatty():
        raise ValueError('Existing host networking may be affected. Review --dry-run, then use '
                         '--accept-network-changes to acknowledge these changes in an unattended setup.')
    try:
        answer = input('Continue with these host networking changes? [y/N] ').strip().casefold()
    except (EOFError, KeyboardInterrupt):
        raise ValueError('Installation/configuration cancelled before applying networking changes.') from None
    if answer not in {'y', 'yes'}:
        raise ValueError('Installation/configuration cancelled before applying networking changes.')


def apply(settings, paths, args, packages, runner=run):
    with configuration_lock(paths.config):
        if hasattr(args, '_configuration_revision'):
            current = Configuration.load(paths.config).revision if paths.config.exists() else None
            if current != args._configuration_revision:
                raise ValueError('Configuration changed during preparation; review the current settings and retry')
        return _apply(settings, paths, args, packages, runner)


def _apply(settings, paths, args, packages, runner=run):
    if os.geteuid() != 0:
        raise ValueError('Run with sudo; --dry-run and --doctor do not require root')
    if args.action == 'configure' and not paths.app.is_dir():
        raise ValueError('Application is not installed; run tools/deployment/install.sh first')
    validate_web_export(settings, paths, args.action)
    network_policy = network_manager_policy(args, paths)
    configuration = getattr(args, '_configuration', Configuration(settings, {})).update(
        getattr(args, 'modem_profile', 'default'), settings)
    args._effective_configuration = configuration
    connection_required = any(value.modem.mode == 'connect'
                              for value in [configuration.base, *configuration.profiles.values()])
    active = runner(['systemctl', 'is-active', '--quiet', SERVICE], check=False).returncode == 0
    will_start = args.start if args.start is not None else (args.action == 'install' or active)
    mm_active = runner(['systemctl', 'is-active', '--quiet', 'ModemManager.service'], check=False).returncode == 0
    mm_enabled = runner(['systemctl', 'is-enabled', '--quiet', 'ModemManager.service'], check=False).returncode == 0
    mm_enabled_state = runner(['systemctl', 'is-enabled', 'ModemManager.service'], check=False).stdout.strip()
    mm_masked = mm_enabled_state in {'masked', 'masked-runtime'}
    mm_available = mm_active or mm_enabled or mm_masked or runner(
        ['systemctl', 'cat', 'ModemManager.service'], check=False).returncode == 0
    if connection_required and will_start and mm_active and args.modem_manager == 'keep':
        raise ValueError('ModemManager is active. Use --interactive or --modem-manager disable on a dedicated '
                         'QMI-Supervisor host; use --mode monitor to keep another modem manager')
    confirm_network_changes(settings, paths, args, runner=runner)
    receipt = tracking_state(paths, runner, modem_manager=dict(
        active=mm_active, enabled=mm_enabled, masked=mm_masked, enabled_state=mm_enabled_state, changed=False))
    receipt['completed'] = False
    receipt.pop('uninstall', None)
    install_state.add_data_directory(receipt, paths.state)
    for identifier, value in [('default', configuration.base), *configuration.profiles.items()]:
        capture_directory = Path(value.telemetry.directory)
        if not capture_directory.is_absolute():
            capture_directory = paths.app / capture_directory
        if identifier != 'default':
            capture_directory = capture_directory/'profiles'/identifier
        install_state.add_data_directory(receipt, capture_directory)
    install_state.save(paths, receipt)
    if not args.skip_packages:
        missing = missing_packages(packages, runner)
        if missing:
            print('Installing: ' + ', '.join(missing))
            if 'resolvconf' in missing:
                receipt['resolver_config']['changed'] = True
            with install_state.record_packages(paths, receipt, runner):
                runner(['apt-get', 'update'])
                runner(['apt-get', 'install', '-y', '--no-remove', *missing])
    # Validate system bindings before stopping/replacing a running installation.
    runner(['/usr/bin/python3', '-c', 'import gi; gi.require_version("Qmi", "1.0"); from gi.repository import Qmi'])
    commands = ['ip', 'systemctl', 'udevadm', 'modprobe']
    if settings.modem.mode == 'connect' and settings.network.method == 'dhcp':
        commands.append('udhcpc')
    if 'resolvconf' in packages:
        commands.append('resolvconf')
    for command in commands:
        runner(['/bin/sh', '-c', 'command -v "$1" >/dev/null', 'check-command', command])
    enabled = runner(['systemctl', 'is-enabled', '--quiet', SERVICE], check=False).returncode == 0
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    old_config = paths.config.read_text() if paths.config.exists() else None
    old_unit = paths.unit.read_text() if paths.unit.exists() else None
    old_command = paths.command.read_text() if paths.command.exists() else None
    old_uninstall_command = paths.uninstall_command.read_text() if paths.uninstall_command.exists() else None
    old_rules = paths.rules.read_text() if paths.rules.exists() else None
    old_rules_mode = stat.S_IMODE(paths.rules.stat().st_mode) if old_rules is not None else 0o644
    stage = stage_application(paths) if args.action == 'install' else None
    desired_rule = (connection_required or settings.web.enabled) and network_policy == 'exclude-qmi'
    rule_will_change = old_rules != QMI_NETWORK_RULE if desired_rule else old_rules == QMI_NETWORK_RULE
    mm_will_change = args.modem_manager == 'disable' and mm_available and (mm_enabled_state != 'masked' or mm_active)
    # An installed process manager watches the atomic INI. Ordinary CLI edits
    # have the same component scope as web edits; deployment and host policy
    # changes still receive the existing complete stop/rollback transaction.
    live_configure = (args.action == 'configure' and active and args.start is None
                      and not rule_will_change and not mm_will_change
                      and supports_live_configuration(paths))
    backup_app = paths.app.with_name(paths.app.name + '.previous-' + stamp)
    backup_config = paths.config.with_name(paths.config.name + '.bak-' + stamp)
    app_moved = deployed = stopped = enable_attempted = mm_changed = rules_changed = changes_started = False
    try:
        application = stage or paths.app
        candidate = configuration.render()
        # Both commands are local validation, without opening a modem or changing host networking.
        runner(['/usr/bin/python3', '-c',
                'import pathlib,sys; root=pathlib.Path(sys.argv[1]); '
                '[compile(p.read_text(), str(p), "exec") for p in root.rglob("*.py")]; '
                'sys.path.insert(0,str(root)); import modules.device.backend, modules.deployment.provisioning', str(application)])
        with tempfile.TemporaryDirectory(prefix='qmi-config-check-') as directory:
            check_path = Path(directory)/'candidate.ini'
            check_path.write_text(candidate)
            runner(['/usr/bin/python3', str(application/'qmi_supervisor.py'), '--config', str(check_path), '--check-config'])
        changes_started = True
        if old_config is not None:
            install_state.add_artifact(receipt, 'config_backups', backup_config)
            install_state.save(paths, receipt)
            write_atomic(backup_config, old_config)
        if active and not live_configure:
            runner(['systemctl', 'stop', SERVICE])
            stopped = True
        if stage:
            if paths.app.exists():
                install_state.add_artifact(receipt, 'app_backups', backup_app)
                install_state.save(paths, receipt)
                paths.app.rename(backup_app)
                app_moved = True
            stage.rename(paths.app)
            deployed = True
        elif not paths.app.is_dir():
            raise ValueError('Application is not installed; run tools/deployment/install.sh first')
        write_atomic(paths.config, candidate)
        unit = (paths.app / 'tools/deployment/qmi-supervisor.service').read_text()
        write_atomic(paths.unit, unit, 0o644)
        wrapper = '#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\nexec /usr/bin/python3 ' + shlex.quote(
            str(paths.app/'tools/deployment/setup.py')) + ' configure "$@"\n'
        write_atomic(paths.command, wrapper, 0o755)
        uninstall_wrapper = '#!/bin/sh\nexport PYTHONDONTWRITEBYTECODE=1\nexec /usr/bin/python3 ' + shlex.quote(
            str(paths.app/'tools/deployment/setup.py')) + ' uninstall "$@"\n'
        write_atomic(paths.uninstall_command, uninstall_wrapper, 0o755)
        paths.state.mkdir(parents=True, exist_ok=True)
        if (settings.modem.mode == 'connect' or settings.web.enabled) and network_policy == 'exclude-qmi':
            if old_rules != QMI_NETWORK_RULE:
                receipt['network_rule']['changed'] = True
                install_state.save(paths, receipt)
                write_atomic(paths.rules, QMI_NETWORK_RULE, 0o644)
                rules_changed = True
        elif old_rules is not None and old_rules == QMI_NETWORK_RULE:
            receipt['network_rule']['changed'] = True
            install_state.save(paths, receipt)
            paths.rules.unlink()
            rules_changed = True
        runner(['systemctl', 'daemon-reload'])
        # Driver loading is independent of modem vendor and remains useful before hotplug.
        for module in ('cdc_wdm', 'qmi_wwan'):
            result = runner(['modprobe', module], check=False)
            if result.returncode:
                print(f'Warning: cannot load {module}; check the target kernel if no QMI modem appears.')
        should_start = args.start if args.start is not None else (args.action == 'install' or active)
        if args.modem_manager == 'disable' and mm_available and (mm_enabled_state != 'masked' or mm_active):
            mm_changed = True
            receipt['modem_manager']['changed'] = True
            install_state.save(paths, receipt)
            runner(['systemctl', 'disable', '--now', 'ModemManager.service'])
            # Disabling boot autostart alone still permits activation through D-Bus.
            runner(['systemctl', 'mask', '--now', 'ModemManager.service'])
        if rules_changed:
            runner(['udevadm', 'control', '--reload-rules'])
            if should_start:
                for device in scan_devices():
                    runner(['udevadm', 'trigger', '--action=change', '/sys/class/net/' + device.interface])
                runner(['udevadm', 'settle', '--timeout=10'])
        if should_start:
            if not live_configure:
                enable_attempted = True
                runner(['systemctl', 'enable', SERVICE])
                runner(['systemctl', 'restart', SERVICE])
            runner(['systemctl', 'is-active', '--quiet', SERVICE])
        receipt['completed'] = True
        install_state.save(paths, receipt)
        print(f'Configuration saved: {paths.config}')
        if password_changed(settings, args):
            print('Web password updated for account ' + settings.web.username)
        if app_moved:
            print(f'Previous application retained: {backup_app}')
        if old_config is not None:
            print(f'Previous configuration retained: {backup_config}')
    except Exception:
        if not changes_started:
            raise
        if enable_attempted:
            runner(['systemctl', 'stop', SERVICE], check=False)
        # OS package installation is not rolled back; application/configuration are.
        if deployed and paths.app.exists():
            failed_app = paths.app.with_name(paths.app.name + '.failed-' + stamp)
            install_state.add_artifact(receipt, 'app_backups', failed_app)
            install_state.save(paths, receipt)
            paths.app.rename(failed_app)
        if app_moved:
            backup_app.rename(paths.app)
        if enable_attempted and not enabled:
            runner(['systemctl', 'disable', SERVICE], check=False)
        for path, previous, mode in ((paths.config, old_config, 0o600), (paths.unit, old_unit, 0o644),
                                     (paths.command, old_command, 0o755),
                                     (paths.uninstall_command, old_uninstall_command, 0o755),
                                     (paths.rules, old_rules, old_rules_mode)):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                write_atomic(path, previous, mode)
        runner(['systemctl', 'daemon-reload'], check=False)
        if rules_changed:
            runner(['udevadm', 'control', '--reload-rules'], check=False)
        if mm_changed:
            runner(['systemctl', 'unmask', 'ModemManager.service'], check=False)
            runner(['systemctl', 'unmask', '--runtime', 'ModemManager.service'], check=False)
            if mm_enabled:
                runner(['systemctl', 'enable', *(['--runtime'] if mm_enabled_state == 'enabled-runtime' else []),
                        'ModemManager.service'], check=False)
            if mm_active:
                runner(['systemctl', 'start', 'ModemManager.service'], check=False)
            if mm_masked:
                runner(['systemctl', 'mask', *(['--runtime'] if mm_enabled_state == 'masked-runtime' else []),
                        'ModemManager.service'], check=False)
        if stopped:
            if can_restart_rollback(paths):
                runner(['systemctl', 'start', SERVICE], check=False)
            else:
                print('Rollback restored the previous files but left qmi-supervisor stopped: '
                    'that version cannot recover pending physical-modem ownership journals. '
                    'Recovery journals and the failed application were retained. Reinstall a version '
                    'supporting physical ownership recovery before restarting the service.', file=sys.stderr)
        raise
    finally:
        if stage and stage.exists():
            shutil.rmtree(stage)


def parser():
    cli = argparse.ArgumentParser(description='Install, configure or uninstall QMI-Supervisor on Debian/Ubuntu with systemd',
        allow_abbrev=False,
        epilog='Uninstall accepts only --dry-run, --purge and --help. It preserves captures, backups and APT packages; '
               '--purge also removes tracked installer-owned data and packages. Other options apply to install/configure.')
    cli.add_argument('action', choices=('install', 'configure', 'uninstall'))
    cli.add_argument('--purge', action='store_true',
                     help='Uninstall and remove tracked captures, backups and introduced APT packages')
    cli.add_argument('--apn', help='APN (default on a new machine: internet)')
    cli.add_argument('--modem-profile', default='default',
                     help='Named modem profile to create/update; default edits the automatic modem template')
    cli.add_argument('--plmn', help='Optional MCC+MNC (5/6 digits); auto restores automatic selection, empty preserves modem selection')
    selector = cli.add_mutually_exclusive_group()
    selector.add_argument('--auto-modem', action='store_true', help='Clear device/serial restrictions; discover QMI USB modems automatically')
    selector.add_argument('--serial', help='Optional USB serial selector; empty clears it')
    selector.add_argument('--device', help='Optional fixed /dev/cdc-wdm path')
    connection = cli.add_mutually_exclusive_group()
    connection.add_argument('--mode', choices=('connect', 'monitor'), help='Legacy connection policy')
    connection.add_argument('--connection', choices=('on', 'off'), help='Enable or disable autonomous connection')
    cli.add_argument('--method', choices=('qmi', 'dhcp'))
    cli.add_argument('--network-profile', choices=('backup', 'primary', 'no-default'),
                     help='Dynamic cellular priority in IPv4 main; no-default only disables its default route')
    cli.add_argument('--route-metric', type=int,
                     help='Exact uint32 metric, or primary ceiling/backup floor when combined with that profile')
    cli.add_argument('--dns', choices=('auto', 'none', 'resolvectl', 'resolvconf'))
    cli.add_argument('--connectivity-check', choices=('on', 'off'),
                     help='Verify WWAN TCP reachability before preferring its route (default: on)')
    cli.add_argument('--connectivity-targets',
                     help='Up to 4 numeric IPv4:port TCP targets, comma-separated; use your own targets for a private network')
    cli.add_argument('--connectivity-interval', type=float, help='Seconds between connectivity checks (1..60; default: 3)')
    cli.add_argument('--connectivity-timeout', type=float, help='Connectivity check timeout in seconds (0.1..5, no longer than interval; default: 1)')
    cli.add_argument('--connectivity-failures', type=int, help='Consecutive failed checks before fallback (1..10; default: 2)')
    cli.add_argument('--connectivity-successes', type=int, help='Consecutive successful checks before restoring preference (1..10; default: 2)')
    cli.add_argument('--telemetry', choices=('on', 'off'))
    cli.add_argument('--gps', choices=('on', 'off'))
    cli.add_argument('--gps-source', choices=('gpsd', 'modem'))
    cli.add_argument('--throughput', choices=('on', 'off'))
    cli.add_argument('--collector-host')
    cli.add_argument('--sample-interval', type=float, help='Target seconds per sample (default on a new host: 1)')
    cli.add_argument('--telemetry-profile', choices=('fast', 'standard', 'full', 'custom'))
    cli.add_argument('--telemetry-indications', choices=('off', 'shadow', 'on'),
                     help='Modem-pushed WDS statistics/status and NAS serving system; shadow compares with polling')
    for group in GROUPS:
        cli.add_argument('--telemetry-query-' + group.replace('_', '-'), type=float, metavar='SECONDS',
                         help=f'Refresh period of the {group} query group with --telemetry-profile custom (0 disables)')
    cli.add_argument('--collection-budget', type=float, help='Maximum QMI collection seconds per sample')
    cli.add_argument('--web', choices=('on', 'off'), help='Enable/disable optional web process; first terminal setup asks for an account')
    cli.add_argument('--web-host', help='Listen IP, or auto for localhost plus physical/bridged management LAN '
                     '(default on a new installation: 0.0.0.0, every interface)')
    cli.add_argument('--web-port', type=int)
    cli.add_argument('--web-user', help='Web account username (default: admin; preserves an existing username)')
    password = cli.add_mutually_exclusive_group()
    password.add_argument('--set-web-password', action='store_true', help='Create/reset the web password using two hidden terminal prompts')
    password.add_argument('--web-password-stdin', action='store_true', help='Read one web password line from stdin, up to 256 UTF-8 bytes')
    cli.add_argument('--modem-manager', choices=('keep', 'disable'), default='keep',
                     help='Keep ModemManager or stop/disable/mask it for a dedicated QMI-Supervisor host')
    cli.add_argument('--network-manager', choices=('exclude-qmi', 'keep'),
                     help='Exclude only qmi_wwan USB net interfaces using udev; Ethernet remains managed')
    cli.add_argument('--accept-network-changes', action='store_true',
                     help='Acknowledge reported host networking impacts without prompting (install/configure only)')
    cli.add_argument('--interactive', action='store_true', help='Ask for APN, automatic selection and network options')
    cli.add_argument('--dry-run', action='store_true', help='Show a plan summary and configuration path without modifying the system')
    cli.add_argument('--doctor', action='store_true', help='Read-only dependency, USB and network diagnostics')
    cli.add_argument('--skip-packages', action='store_true', help='Use existing OS packages (offline/manual provisioning)')
    start = cli.add_mutually_exclusive_group()
    start.add_argument('--start', action='store_true', default=None, help='Enable and start/restart the service')
    start.add_argument('--no-start', dest='start', action='store_false', help='Leave the service stopped after applying changes')
    return cli


def main(argv=None):
    cli = parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = cli.parse_args(arguments)
    if args.purge and args.action != 'uninstall':
        cli.error('--purge is only valid with uninstall')
    if args.action == 'uninstall':
        allowed = {'--purge', '--dry-run', '--help', '-h'}
        invalid = [value.split('=', 1)[0] for value in arguments if value.startswith('-')
                   and value.split('=', 1)[0] not in allowed]
        if invalid:
            cli.error('uninstall only accepts --purge, --dry-run and --help; unsupported: ' + ', '.join(invalid))
    paths = Paths(source=Path(__file__).resolve().parents[2])
    password_pending = False
    try:
        if args.action == 'uninstall':
            from modules.deployment.uninstall import uninstall
            return uninstall(paths, args, runner=run)
        bootstrap = [paths.manifest.parent / ('bootstrap-packages.' + suffix) for suffix in ('before', 'after')]
        if not args.dry_run and not args.doctor and any(path.exists() or path.is_symlink() for path in bootstrap):
            if os.geteuid() != 0:
                raise ValueError('Run with sudo to record the Python bootstrap transaction')
            tracking_state(paths)
        settings = candidate_settings(args, paths)
        if args.interactive and not (args.doctor or args.dry_run):
            settings = interactive(settings)
            if settings.modem.mode == 'connect':
                args.modem_manager = ask('ModemManager (disable on dedicated cellular controller)',
                                         args.modem_manager, ('keep', 'disable'))
        if args.doctor:
            from modules.deployment.diagnostics import diagnose_configuration, print_report
            checks = diagnose_configuration(args._configuration.update(args.modem_profile, settings,
                                                                       validate=candidate_validation))
            print_report(checks)
            return int(any(check['status'] == 'error' for check in checks))
        validate_web_export(settings, paths, args.action)
        system = supported_system()
        configuration = args._configuration.update(args.modem_profile, settings, validate=candidate_validation)
        packages = packages_for(settings)
        for profile in dict.fromkeys([configuration.base, *configuration.profiles.values()]):
            if profile != settings:
                packages = sorted(set(packages).union(packages_for(profile)))
        settings = web_credentials(settings, args)
        password_pending = password_changed(settings, args)
        # Apply only ever sees a startable configuration; a dry run does not
        # read passwords, so its plan may still await the account.
        try:
            (candidate_validation if args.dry_run else validate_settings)(settings)
        except ValueError as error:
            if str(error) == 'LAN access requires a web password':
                raise ValueError('LAN access requires a web password: use --set-web-password in a terminal, '
                                 '--web-password-stdin, or --web-host 127.0.0.1') from None
            raise
        print('Target: ' + system)
        print_plan(settings, paths, packages, args)
        if args.dry_run:
            confirm_network_changes(settings, paths, args)
            return 0
        apply(settings, paths, args, packages)
        password_pending = False
        from modules.deployment.diagnostics import diagnose_configuration, print_report
        checks = diagnose_configuration(Configuration.load(paths.config) if paths.config.exists()
                                        else args._configuration.update(args.modem_profile, settings))
        healthy = print_report(checks)
        print('Installed/configured. Logs: journalctl -u qmi-supervisor -f' if healthy else
              'Installed/configured, but required diagnostic checks failed; inspect the report above.')
        return 0 if healthy else 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'stderr', None) or str(error)
        print('Setup failed: ' + detail, file=sys.stderr)
        if password_pending:
            print('The web password was not changed; the previous one is still valid.', file=sys.stderr)
        return 1
