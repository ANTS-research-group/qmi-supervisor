"""Bounded, read-only checks for networking that a proposed deployment may affect."""
from configparser import ConfigParser, Error as IniError
import fnmatch
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from modules.device.device import scan_devices

RULE = ('# QMI-Supervisor owns QMI USB network interfaces only.\n'
        'SUBSYSTEM=="net", DRIVERS=="qmi_wwan", ENV{NM_UNMANAGED}="1"\n')
MAX_ITEMS = 64
MAX_BYTES = 256 * 1024
IFACE = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
MODEM_PATH = re.compile(r'/org/freedesktop/ModemManager1/Modem/\d+\Z')


def _label(value):
    """Quote control characters and bound names without exposing other profile properties."""
    value = ''.join(character if character.isprintable() else f'\\u{ord(character):04x}'
                    for character in str(value)[:120])
    return '"'+value.replace('"', '\\"')+'"'


def _terse(line):
    """nmcli --escape yes escapes both the separator and backslash in tabular output."""
    fields, field, escaped = [], [], False
    for character in line:
        if escaped:
            field.append(character)
            escaped = False
        elif character == '\\':
            escaped = True
        elif character == ':':
            fields.append(''.join(field))
            field = []
        else:
            field.append(character)
    if escaped:
        raise ValueError('Incomplete nmcli escape')
    fields.append(''.join(field))
    return fields


def _field(document, name):
    if name in document:
        return document[name]
    current = document
    for part in name.split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _ini(path):
    with path.open() as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError('Profile inspection size limit exceeded')
    parser = ConfigParser(interpolation=None, strict=False)
    parser.read_string(content)
    return parser


def inspect_host(settings, paths, args, *, runner, devices=None, which=shutil.which):
    """Report actual proposed effects; never request credentials or execute network changes.

    Commands receive a five-second timeout and share a thirty-second inspection budget.
    mmcli/nmcli are only used against already running daemons, avoiding D-Bus activation.
    """
    findings = []
    deadline = time.monotonic() + 30

    def add(code, detail, **metadata):
        item = dict(code=code, detail=detail, **metadata)
        if item not in findings:
            findings.append(item)

    def query(command):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            result = runner(command, check=False, timeout=min(5, remaining))
            if len(result.stdout or '') > MAX_BYTES:
                return None
            return result
        except (OSError, subprocess.SubprocessError):
            return None

    def output(command):
        result = query(command)
        return result.stdout if result is not None and result.returncode == 0 else None

    def service(name):
        result = query(['systemctl', 'is-active', '--quiet', name])
        active = None if result is None or result.returncode not in (0, 3, 4) else result.returncode == 0
        enabled = query(['systemctl', 'is-enabled', name])
        raw = (enabled.stdout or '').strip() if enabled is not None else ''
        known = raw in {'enabled', 'enabled-runtime', 'disabled', 'static', 'indirect',
                        'masked', 'masked-runtime', 'alias', 'linked', 'linked-runtime', 'generated', 'transient'}
        exists = bool(active or raw in {'enabled', 'enabled-runtime', 'masked', 'masked-runtime'})
        cat = None
        if not exists:
            cat = query(['systemctl', 'cat', name])
            exists = cat is not None and cat.returncode == 0
        unknown = active is None or not known
        if not exists and unknown:
            load_state = query(['systemctl', 'show', name, '--property=LoadState', '--value'])
            if (active is False and load_state is not None and load_state.returncode == 0
                    and (load_state.stdout or '').strip() == 'not-found'):
                unknown = False
        return active, raw, exists, unknown

    active_service = query(['systemctl', 'is-active', '--quiet', 'qmi-supervisor.service'])
    supervisor_active = active_service is not None and active_service.returncode == 0
    will_start = getattr(args, 'start', None)
    if will_start is None:
        will_start = getattr(args, 'action', 'install') == 'install' or supervisor_active
    connect = settings.modem.mode == 'connect'
    configuration = getattr(args, '_effective_configuration', None)
    previous_configuration = getattr(args, '_configuration', None)
    if configuration is not None and previous_configuration is None and paths.config.is_file():
        from modules.runtime.fleet_config import Configuration
        try:
            previous_configuration = Configuration.load(paths.config)
        except (OSError, ValueError):
            pass
    configured_scopes = [('default', configuration.base), *configuration.profiles.items()] if configuration else [('default', settings)]
    connect_any = (any(value.modem.mode == 'connect' for value in
                      [configuration.base, *configuration.profiles.values()]) if configuration else connect)
    try:
        current_rule = paths.rules.read_text() if paths.rules.exists() else None
        rule_unknown = False
    except (OSError, UnicodeError):
        current_rule, rule_unknown = None, True
    policy = getattr(args, 'network_manager', None)
    if policy is None:
        policy = 'exclude-qmi' if not paths.app.exists() or current_rule == RULE else 'keep'
    wants_rule = (connect_any or settings.web.enabled) and policy == 'exclude-qmi'
    rule_added = wants_rule and current_rule != RULE
    rule_removed = not wants_rule and current_rule == RULE
    rule_change = rule_added or rule_removed
    affects_connection = connect
    live_configure = False
    if (getattr(args, 'action', 'install') == 'configure' and supervisor_active
            and getattr(args, 'start', None) is None and not rule_change
            and getattr(args, 'modem_manager', 'keep') == 'keep'):
        from modules.deployment.provisioning import supports_live_configuration
        from modules.runtime.settings import load_settings
        if supports_live_configuration(paths):
            live_configure = True
            try:
                previous = load_settings(paths.config, profile=getattr(args, 'modem_profile', 'default'))
                affects_connection = (connect or previous.modem.mode == 'connect') and (previous.modem, previous.network, previous.recovery) != (
                    settings.modem, settings.network, settings.recovery)
            except ValueError:
                pass
    if not live_configure:
        affects_connection = connect_any or bool(previous_configuration and any(value.modem.mode == 'connect'
            for value in [previous_configuration.base, *previous_configuration.profiles.values()]))
    if rule_unknown and wants_rule:
        add('custom-udev-unknown', f'Cannot inspect {paths.rules}; the proposed QMI exclusion may replace an existing rule.')
    elif rule_added and current_rule is not None:
        add('custom-udev-rule', f'The custom udev rule at {paths.rules} will be overwritten by a rule excluding all qmi_wwan interfaces from NetworkManager.')
    if rule_removed:
        add('qmi-udev-removal', f'The generated QMI exclusion rule at {paths.rules} will be removed; NetworkManager may manage all qmi_wwan interfaces again, including previously configured cellular connections.')

    try:
        devices = list(scan_devices() if devices is None else devices)
    except OSError:
        devices = []
        if rule_change or (affects_connection and will_start):
            add('devices-unknown', 'QMI interface discovery failed; existing cellular interface use could not be checked.')
    interfaces = {device.interface for device in devices if IFACE.fullmatch(device.interface)}
    selected = {device.interface for device in devices if IFACE.fullmatch(device.interface)
                and (settings.modem.device == 'auto' or Path(settings.modem.device).resolve() == Path(device.path).resolve())
                and (not settings.modem.serial or settings.modem.serial == device.serial)}
    if configuration is not None:
        from modules.runtime.fleet import assignments
        from modules.runtime.ipc import read_json
        bindings = read_json(paths.runtime/'bindings.json') or {}
        selected = set()
        for candidate, historical in ((configuration, False), (previous_configuration, True)):
            if candidate is None:
                continue
            for identifier, (value, device, _) in assignments(candidate, devices, bindings).items():
                if (device is not None and IFACE.fullmatch(device.interface) and value.modem.mode == 'connect'
                        and (not live_configure or identifier == getattr(args, 'modem_profile', 'default'))
                        and (not historical or identifier == 'default' or identifier in candidate.profiles)):
                    selected.add(device.interface)
    affected = set(interfaces if rule_change else ())
    if affects_connection and (will_start or supervisor_active):
        affected.update(selected)
    action = ('exclude every qmi_wwan interface from NetworkManager' if rule_added else
              'remove the QMI exclusion and allow NetworkManager to manage these interfaces again' if rule_removed else
              'stop/reopen the supervisor connection' if supervisor_active else 'start supervisor control')
    mm_change = False
    mm_ports = set()

    def effect(interface):
        effects = []
        if mm_change and interface in mm_ports:
            effects.append('stop, disable and mask ModemManager globally')
        if interface in interfaces and rule_change:
            effects.append(action)
        if interface in selected and affects_connection and (will_start or supervisor_active):
            effects.append('stop/reopen the supervisor connection' if supervisor_active else 'start supervisor control')
        return '; '.join(effects) or action

    # Disabling this daemon is global, independent of the selected modem or --no-start.
    if getattr(args, 'modem_manager', 'keep') == 'disable':
        mm_active, mm_enabled, mm_exists, mm_unknown = service('ModemManager.service')
        changing_mm = mm_exists and (mm_enabled != 'masked' or mm_active)
        mm_change = bool(changing_mm or mm_unknown)
        if changing_mm or mm_unknown:
            add('modem-manager-global', 'ModemManager will be stopped, disabled and persistently masked for ALL modems, including modems not selected by this supervisor.')
            if mm_unknown:
                add('modem-manager-unknown', 'ModemManager service state could not be fully inspected; existing connections are unknown.')
            if mm_active:
                if not which('mmcli'):
                    add('modem-manager-unknown', 'mmcli is unavailable; ModemManager modem and bearer use is unknown.')
                else:
                    listing = output(['mmcli', '--list-modems', '--output-json'])
                    try:
                        document = json.loads(listing) if listing is not None else None
                        modems = document['modem-list']
                        if not isinstance(modems, list) or any(not isinstance(p, str) or not MODEM_PATH.fullmatch(p) for p in modems):
                            raise ValueError('Invalid modem list')
                        if len(modems) > MAX_ITEMS:
                            add('modem-manager-unknown', 'Modem inspection limit reached; additional modem use is unknown.')
                        for modem in modems[:MAX_ITEMS]:
                            raw = output(['mmcli', '--modem', modem, '--output-json'])
                            try:
                                info = json.loads(raw) if raw is not None else None
                                if not isinstance(info, dict):
                                    raise ValueError('Invalid modem state')
                                state = _field(info, 'modem.generic.state')
                                ports = _field(info, 'modem.generic.ports')
                                bearers = _field(info, 'modem.generic.bearers')
                                if state not in {'unknown', 'failed', 'initializing', 'locked', 'disabled',
                                                 'disabling', 'enabling', 'enabled', 'searching', 'registered',
                                                 'disconnecting', 'connecting', 'connected'}:
                                    raise ValueError('Missing modem state')
                                if isinstance(ports, list):
                                    for port in ports:
                                        if isinstance(port, str):
                                            match = re.fullmatch(r'([A-Za-z0-9_.:-]{1,64}) \(net\)', port)
                                            if match:
                                                affected.add(match[1])
                                                mm_ports.add(match[1])
                                count = len(bearers) if isinstance(bearers, list) else None
                                if state in {'connected', 'connecting', 'disconnecting'} or count:
                                    detail = f'ModemManager modem {modem.rsplit("/", 1)[-1]} is {_label(state)}'
                                    if count is not None:
                                        detail += f' with {count} configured bearer(s)'
                                    add('modem-manager-connected', detail+'; stopping the daemon can interrupt its cellular connection.', modem_state=state, bearer_count=count)
                            except (ValueError, TypeError):
                                add('modem-manager-unknown', 'A ModemManager modem could not be inspected; its connection state is unknown.')
                    except (ValueError, TypeError, KeyError):
                        add('modem-manager-unknown', 'ModemManager modem listing failed or returned invalid JSON; modem use is unknown.')

    # A new/removable global QMI rule can affect every saved cellular profile.
    inspect_profiles = rule_change or mm_change
    if inspect_profiles or affected:
        nm_active, _, nm_exists, nm_unknown = service('NetworkManager.service')
        profiles = []
        if nm_active and which('nmcli'):
            command = ['nmcli', '-t', '--escape', 'yes', '-f', 'UUID,TYPE,NAME,DEVICE', 'connection', 'show']
            saved = output(command)
            active = output([*command, '--active'])
            for raw, is_active in ((saved, False), (active, True)):
                try:
                    if raw is None:
                        raise ValueError('Unavailable connection list')
                    rows = [line for line in raw.splitlines() if line]
                    if len(rows) > MAX_ITEMS:
                        add('network-manager-unknown', 'NetworkManager profile inspection limit reached; additional profile use is unknown.')
                    for line in rows[:MAX_ITEMS]:
                        values = _terse(line)
                        if len(values) != 4 or not UUID.fullmatch(values[0]):
                            raise ValueError('Invalid connection metadata')
                        uuid, kind, name, interface = values
                        if is_active:
                            if interface in affected:
                                add('network-manager-active', f'NetworkManager profile {_label(name)} is active on {interface}; the deployment will {effect(interface)}. The saved profile is retained.', interfaces=[interface])
                        else:
                            binding = ''
                            if inspect_profiles:
                                binding_output = output(['nmcli', '-g', 'connection.interface-name', 'connection', 'show', 'uuid', uuid])
                                if binding_output is None:
                                    add('network-manager-unknown', 'Some NetworkManager saved interface bindings could not be inspected.')
                                else:
                                    binding = _terse(binding_output.strip())[0]
                            profiles.append((kind, name, binding))
                except (ValueError, TypeError):
                    add('network-manager-unknown', 'NetworkManager connection metadata could not be inspected or parsed; existing profiles are unknown.')
        else:
            # Only inspect metadata from keyfiles; never emit GSM credentials/APNs.
            for root in (paths.config.parent, paths.runtime.parent):
                directory = root / 'NetworkManager/system-connections'
                try:
                    files = sorted(directory.iterdir()) if directory.exists() else []
                    if len(files) > MAX_ITEMS:
                        add('network-manager-unknown', 'NetworkManager keyfile inspection limit reached; additional profiles are unknown.')
                    for path in files[:MAX_ITEMS]:
                        if not path.is_file():
                            continue
                        try:
                            parser = _ini(path)
                            profiles.append((parser.get('connection', 'type', fallback=''),
                                             parser.get('connection', 'id', fallback=path.name),
                                             parser.get('connection', 'interface-name', fallback='')))
                        except (OSError, IniError, UnicodeError, ValueError):
                            add('network-manager-unknown', f'Cannot inspect NetworkManager profile metadata in {_label(path.name)}; profile use is unknown.')
                except OSError:
                    add('network-manager-unknown', f'Cannot read profile metadata in {directory}; profile use is unknown.')
            if nm_active or nm_unknown:
                add('network-manager-unknown', 'Live NetworkManager connection inspection is unavailable; saved keyfiles cannot establish which connections are active.')
        if inspect_profiles:
            for kind, name, binding in profiles:
                if kind in {'gsm', 'cdma'} or binding in affected:
                    suffix = f' on {binding}' if binding in affected else ' for cellular modems'
                    planned = 'stop, disable and mask ModemManager globally' if mm_change else action
                    add('network-manager-profile', f'NetworkManager has saved profile {_label(name)}{suffix}; the deployment will {planned}. The profile is retained.', interfaces=[binding] if binding in affected else [])

    # Interface state comes from rtnetlink via ip, never a QMI request.
    owned_networks = []
    checkpoints = [paths.checkpoint, *sorted((paths.runtime/'modems').glob('*/connection-state.json')),
                   *sorted((paths.runtime/'ownership').glob('*/connection-state.json'))]
    if len(checkpoints) > MAX_ITEMS:
        add('checkpoint-unknown', 'Modem checkpoint inspection limit reached; additional ownership is unknown.')
    for checkpoint in checkpoints[:MAX_ITEMS]:
        if not checkpoint.is_file():
            continue
        try:
            with checkpoint.open() as stream:
                owned = json.loads(stream.read(MAX_BYTES + 1))
            owned_network = owned.get('network', {}) if owned.get('version') == 1 else {}
            if isinstance(owned_network, dict):
                owned_networks.append(owned_network)
        except (OSError, ValueError, AttributeError):
            pass
    if affected:
        if not which('ip'):
            add('interface-unknown', 'ip is unavailable; existing QMI interface addresses, routes and SSH routing are unknown.')
        else:
            for interface in sorted(affected)[:MAX_ITEMS]:
                raw = output(['ip', '-j', 'addr', 'show', 'dev', interface])
                try:
                    values = json.loads(raw) if raw is not None else None
                    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
                        raise ValueError('Invalid address list')
                    occupied = any(address.get('scope') == 'global'
                                   for item in values for address in item.get('addr_info', []) if isinstance(address, dict))
                    if occupied:
                        own = any(owned_network.get('interface') == interface
                               and type(owned_network.get('ifindex')) is int
                               and owned_network['ifindex'] > 0
                               and any(item.get('ifindex') == owned_network.get('ifindex') for item in values)
                               for owned_network in owned_networks)
                        attribution = "The supervisor's checkpoint owns this interface" if own else 'The existing owner must be checked'
                        add('interface-addresses', f'{interface} already has global IPv4/IPv6 addresses. {attribution}; the deployment will {effect(interface)} and can interrupt traffic.', interfaces=[interface], supervisor_owned=own)
                except (ValueError, TypeError):
                    add('interface-unknown', f'Cannot inspect IP addresses on {interface}; existing interface use is unknown.', interfaces=[interface])
                for family in ('-4', '-6'):
                    raw = output(['ip', '-j', family, 'route', 'show', 'default', 'dev', interface])
                    try:
                        routes = json.loads(raw) if raw is not None else None
                        if not isinstance(routes, list) or any(not isinstance(item, dict) for item in routes):
                            raise ValueError('Invalid route list')
                        if routes:
                            add('interface-default-route', f'{interface} carries an existing IPv{family[-1]} default route; the deployment will {effect(interface)} and can interrupt traffic using it.', interfaces=[interface])
                    except (ValueError, TypeError):
                        add('interface-unknown', f'Cannot inspect IPv{family[-1]} default routes on {interface}; route use is unknown.', interfaces=[interface])
            ssh = os.environ.get('SSH_CONNECTION', '').split()
            if ssh:
                try:
                    remote = str(ipaddress.ip_address(ssh[0]))
                    raw = output(['ip', '-j', 'route', 'get', remote])
                    routes = json.loads(raw) if raw is not None else None
                    if not isinstance(routes, list):
                        raise ValueError('Invalid SSH route')
                    used = {route.get('dev') for route in routes if isinstance(route, dict)} & affected
                    if used:
                        add('ssh-route', 'This SSH session routes through '+', '.join(sorted(used))+'. These networking changes can disconnect the current remote session.', interfaces=sorted(used))
                except (ValueError, TypeError):
                    add('ssh-route-unknown', 'The route for the current SSH session could not be verified; remote disconnection risk is unknown.')

        for root in (paths.config.parent, paths.runtime.parent):
            directory = root / 'systemd/network'
            try:
                profiles = sorted(directory.glob('*.network'))[:MAX_ITEMS]
                for path in profiles:
                    try:
                        parser = _ini(path)
                        patterns = parser.get('Match', 'Name', fallback='').split()
                        matched = sorted(interface for interface in affected
                                         if any(fnmatch.fnmatchcase(interface, pattern) for pattern in patterns))
                        if matched:
                            effects = '; '.join(dict.fromkeys(effect(interface) for interface in matched))
                            add('networkd-profile', f'Existing networkd profile {path} matches '+', '.join(matched)+f'; the deployment will {effects}. That profile is retained.', interfaces=matched)
                    except (OSError, IniError, UnicodeError, ValueError):
                        add('networkd-unknown', f'Cannot inspect networkd profile {_label(path.name)}; explicit interface ownership is unknown.')
            except OSError:
                add('networkd-unknown', f'Cannot inspect networkd profiles in {directory}; interface ownership is unknown.')

    resolver_exists = paths.resolver.exists() or paths.resolver.is_symlink()
    dns_scopes = [(identifier, value) for identifier, value in configured_scopes
                  if (value.modem.mode == 'connect' or value.web.enabled) and value.network.dns != 'none']
    if dns_scopes and resolver_exists:
        backends = {value.network.dns for _, value in dns_scopes}
        needs_resolvconf = 'resolvconf' in backends
        if 'auto' in backends:
            resolved = output(['resolvectl', 'status']) if which('resolvectl') else None
            needs_resolvconf = needs_resolvconf or resolved is None
        if needs_resolvconf and not getattr(args, 'skip_packages', False):
            package = query(['dpkg-query', '-W', '-f=${Status}', 'resolvconf'])
            if package is None or package.returncode or (package.stdout or '').strip() != 'install ok installed':
                add('dns-install', f'Installing resolvconf may replace the host resolver configuration or symlink at {paths.resolver}, affecting DNS beyond the modem.')
        previous = None
        try:
            previous = _ini(paths.config)
        except (OSError, IniError, UnicodeError, ValueError):
            pass
        for identifier, value in dns_scopes:
            if live_configure and identifier != getattr(args, 'modem_profile', 'default'):
                continue
            old_dns = old_mode = None
            if previous is not None:
                suffix = ':'+identifier if identifier != 'default' else ''
                old_dns = previous.get('network'+suffix, 'dns', fallback=previous.get('network', 'dns', fallback='auto'))
                old_mode = previous.get('modem'+suffix, 'mode', fallback=previous.get('modem', 'mode', fallback='connect'))
            if affects_connection and will_start and value.modem.mode == 'connect' and (
                    old_mode != 'connect' or old_dns != value.network.dns):
                profile = '' if identifier == 'default' else f' for profile {_label(identifier)}'
                add('dns-change', f'Cellular DNS policy {value.network.dns}{profile} will be applied alongside the existing host resolver when the verified cellular route is preferred.')
    return findings


def print_findings(findings):
    for finding in findings:
        print('[NETWORK WARNING] '+finding['detail'])
