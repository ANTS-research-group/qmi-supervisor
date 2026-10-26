"""Read-only host inspection and installer consent, using only temporary fixtures."""
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modules.deployment import preflight, provisioning
from modules.device.device import Device
from modules.runtime.settings import Settings
from tests.deployment.test_provisioning import FakeRunner


class HostRunner:
    """An explicitly read-only network/service model; unknown commands fail closed."""
    def __init__(self):
        self.calls = []
        self.services = {'NetworkManager.service': {'active': True, 'enabled': True, 'exists': True}}
        self.nm_profiles = ''
        self.nm_active = ''
        self.nm_interfaces = {}
        self.mm_list = {'modem-list': []}
        self.mm_modems = {}
        self.addresses = {}
        self.routes = {}
        self.ssh_route = []
        self.failures = {}

    def __call__(self, args, *, check=True, timeout=None, input=None):
        command = tuple(args)
        self.calls.append(command)
        if timeout is None or not 0 < timeout <= 10:
            raise AssertionError(f'Preflight command must have a bounded timeout: {command}')
        if check:
            raise AssertionError(f'Preflight must handle unavailable queries: {command}')
        if command in self.failures:
            result = self.failures[command]
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(args, *result)
        code, stdout = 0, ''
        if command[0] == 'systemctl' and command[1] in ('is-active', 'is-enabled', 'cat'):
            service = self.services.get(command[-1], {})
            exists = bool(service.get('exists') or service.get('active') or service.get('enabled')
                          or service.get('enabled_state'))
            if command[1] == 'is-active':
                code, stdout = (0, 'active\n') if service.get('active') else (3, 'inactive\n')
            elif command[1] == 'is-enabled':
                stdout = (service.get('enabled_state', 'enabled' if service.get('enabled') else 'disabled')
                          if exists else '') + '\n'
                code = 0 if service.get('enabled') else 1
            else:
                code = 0 if service.get('exists') else 1
                stdout = '# a unit exists\n' if code == 0 else ''
        elif command[0] == 'systemctl' and command[1] == 'show' and '--property=LoadState' in command:
            service = self.services.get(command[2], {})
            exists = bool(service.get('exists') or service.get('active') or service.get('enabled')
                          or service.get('enabled_state'))
            stdout = 'loaded\n' if exists else 'not-found\n'
        elif command[0] == 'nmcli' and 'connection.interface-name' in command:
            stdout = self.nm_interfaces.get(command[-1], '')
        elif command[0] == 'nmcli' and 'connection' in command and 'show' in command:
            stdout = self.nm_active if '--active' in command else self.nm_profiles
        elif command[0] == 'mmcli' and '--list-modems' in command:
            stdout = self.mm_list if isinstance(self.mm_list, str) else json.dumps(self.mm_list)
        elif command[0] == 'mmcli' and '--modem' in command:
            data = self.mm_modems[command[command.index('--modem')+1]]
            stdout = data if isinstance(data, str) else json.dumps(data)
        elif command[0] == 'ip' and 'addr' in command:
            stdout = json.dumps(self.addresses.get(command[-1], []))
        elif command[0] == 'ip' and 'route' in command and 'get' in command:
            stdout = json.dumps(self.ssh_route)
        elif command[0] == 'ip' and 'route' in command and 'default' in command:
            family = 6 if '-6' in command else 4
            stdout = json.dumps(self.routes.get((family, command[-1]), []))
        elif command[0] == 'dpkg-query' and command[-1] == 'resolvconf':
            code = 1  # the DNS integration package is not installed in this modeled host
        elif command == ('resolvectl', 'status'):
            code = 1
        else:
            raise AssertionError(f'Unrecognized or mutating preflight command: {command}')
        return subprocess.CompletedProcess(args, code, stdout, '')


class PreflightFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qmi-preflight-tests-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = provisioning.Paths(
            source=self.root/'source', app=self.root/'opt/qmi-supervisor',
            config=self.root/'etc/qmi-supervisor.ini',
            unit=self.root/'etc/systemd/system/qmi-supervisor.service',
            command=self.root/'usr/local/sbin/qmi-supervisor-configure',
            state=self.root/'var/lib/qmi-supervisor',
            rules=self.root/'etc/udev/rules.d/85-qmi-supervisor.rules',
            runtime=self.root/'run/qmi-supervisor')
        self.settings = replace(Settings(), network=replace(Settings().network, dns='none'),
                                telemetry=replace(Settings().telemetry, directory=str(self.paths.state)))
        self.devices = [Device('/dev/cdc-wdm0', 'wwan0', '/sys/test/one', 'test-one', 'Test modem'),
                        Device('/dev/cdc-wdm1', 'wwan1', '/sys/test/two', 'test-two', 'Other modem')]
        self.host = HostRunner()
        self.tools = {'systemctl', 'ip', 'nmcli', 'mmcli', 'resolvectl', 'resolvconf'}
        self.output = StringIO()

    def args(self, *flags):
        return provisioning.parser().parse_args(['install', *flags])

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def inspect(self, *, settings=None, flags=(), devices=None, environment=None):
        with patch.dict('os.environ', environment or {}, clear=True):
            return preflight.inspect_host(settings or self.settings, self.paths, self.args(*flags),
                runner=self.host, devices=self.devices if devices is None else devices,
                which=lambda tool: '/fake/bin/'+tool if tool in self.tools else None)

    @staticmethod
    def details(findings):
        return '\n'.join(item['code']+' '+item['detail'] for item in findings)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file() and not path.is_symlink()}


class HostInspectionTests(PreflightFixture):
    def test_named_resolver_dependency_is_inspected_even_when_default_dns_is_disabled(self):
        from modules.runtime.fleet_config import Configuration
        base = replace(self.settings, modem=replace(self.settings.modem, mode='monitor', serial='test-one'))
        private = replace(base, modem=replace(base.modem, mode='connect', serial='test-two'),
                          network=replace(base.network, dns='resolvconf'))
        self.write(self.paths.resolver, 'nameserver 192.0.2.53\n')
        args = self.args('--network-manager', 'keep')
        args._effective_configuration = Configuration(base, {'private': private})
        findings = preflight.inspect_host(base, self.paths, args, runner=self.host,
            devices=self.devices, which=lambda tool: '/fake/'+tool)
        self.assertIn('dns-install', {finding['code'] for finding in findings})

    def test_disabling_a_named_connection_inspects_the_interface_being_released(self):
        from modules.runtime.fleet_config import Configuration
        base = replace(self.settings, modem=replace(self.settings.modem, serial='test-one'))
        private = replace(base, modem=replace(base.modem, serial='test-two'))
        configuration = Configuration(base, {'private': private})
        self.write(self.paths.config, configuration.render())
        unit = '[Service]\nType=notify\n'
        self.write(self.paths.unit, unit)
        self.write(self.paths.app/'tools/deployment/qmi-supervisor.service', unit)
        self.write(self.paths.app/'qmi_supervisor.py', 'from modules.runtime.fleet import FleetManager\n')
        self.write(self.paths.rules, preflight.RULE)
        self.host.services['qmi-supervisor.service'] = {'active': True, 'enabled': True, 'exists': True}
        for interface in ('wwan0', 'wwan1'):
            self.host.addresses[interface] = [{'addr_info': [{'local': '198.18.0.2', 'scope': 'global'}]}]
        changed = replace(private, modem=replace(private.modem, mode='monitor'))
        args = self.args('--modem-profile', 'private')
        args.action = 'configure'
        args._effective_configuration = configuration.update('private', changed)
        findings = preflight.inspect_host(changed, self.paths, args, runner=self.host,
            devices=self.devices, which=lambda tool: '/fake/'+tool)
        addresses = [finding for finding in findings if finding['code'] == 'interface-addresses']
        self.assertEqual([finding['interfaces'] for finding in addresses], [['wwan1']])

    def test_whole_platform_install_inspects_a_connected_named_modem_with_default_connection_disabled(self):
        from modules.runtime.fleet_config import Configuration
        base = replace(self.settings, modem=replace(self.settings.modem, mode='monitor', serial='test-one'))
        private = replace(base, modem=replace(base.modem, mode='connect', serial='test-two'))
        args = self.args('--network-manager', 'keep')
        args._effective_configuration = Configuration(base, {'private': private})
        self.host.addresses['wwan1'] = [{'ifindex': 7, 'addr_info': [{'local': '198.18.0.6', 'scope': 'global'}]}]
        checkpoint = self.paths.runtime/'modems/private/connection-state.json'
        self.write(checkpoint, json.dumps({'version': 1, 'network': {'interface': 'wwan1', 'ifindex': 7}}))
        findings = preflight.inspect_host(base, self.paths, args, runner=self.host,
            devices=self.devices, which=lambda tool: '/fake/'+tool)
        addresses = [finding for finding in findings if finding['code'] == 'interface-addresses']
        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0]['interfaces'], ['wwan1'])
        self.assertTrue(addresses[0]['supervisor_owned'])

    def test_live_named_modem_edit_warns_only_for_its_own_interface(self):
        from modules.runtime.fleet_config import Configuration
        base = replace(self.settings, modem=replace(self.settings.modem, serial='test-one'))
        private = replace(base, modem=replace(base.modem, serial='test-two'))
        configuration = Configuration(base, {'private': private})
        self.write(self.paths.config, configuration.render())
        unit = '[Service]\nType=notify\n'
        self.write(self.paths.unit, unit)
        self.write(self.paths.app/'tools/deployment/qmi-supervisor.service', unit)
        self.write(self.paths.app/'qmi_supervisor.py', 'from modules.runtime.fleet import FleetManager\n')
        self.write(self.paths.rules, preflight.RULE)
        self.host.services['qmi-supervisor.service'] = {'active': True, 'enabled': True, 'exists': True}
        for interface in ('wwan0', 'wwan1'):
            self.host.addresses[interface] = [{'addr_info': [{'local': '198.18.0.2', 'scope': 'global'}]}]
        changed = replace(private, modem=replace(private.modem, apn='changed'))
        args = self.args('--modem-profile', 'private')
        args.action = 'configure'
        args._effective_configuration = configuration.update('private', changed)
        findings = preflight.inspect_host(changed, self.paths, args, runner=self.host,
            devices=self.devices, which=lambda tool: '/fake/'+tool)
        addresses = [finding for finding in findings if finding['code'] == 'interface-addresses']
        self.assertEqual([finding['interfaces'] for finding in addresses], [['wwan1']])

    def test_live_telemetry_edit_does_not_claim_a_connection_takeover(self):
        self.write(self.paths.config, provisioning.render_config(self.settings))
        unit = '[Service]\nType=notify\n'
        self.write(self.paths.unit, unit)
        self.write(self.paths.app/'tools/deployment/qmi-supervisor.service', unit)
        self.write(self.paths.app/'qmi_supervisor.py', 'from modules.runtime.processes import ProcessManager\n')
        self.write(self.paths.rules, preflight.RULE)
        self.host.services['qmi-supervisor.service'] = {'active':True, 'enabled':True, 'exists':True}
        self.host.addresses['wwan0'] = [{'addr_info': [{'local':'198.18.0.2', 'scope':'global', 'family':'inet'}]}]
        settings = replace(self.settings, telemetry=replace(self.settings.telemetry, enabled=False))
        args = self.args()
        args.action = 'configure'
        with patch.dict('os.environ', {}, clear=True):
            findings = preflight.inspect_host(settings, self.paths, args, runner=self.host,
                                              devices=self.devices, which=lambda tool: '/fake/'+tool)
        self.assertNotIn('interface-addresses', {finding['code'] for finding in findings})

    def test_idle_web_preparation_reports_qmi_exclusion_effects(self):
        settings = replace(self.settings, modem=replace(self.settings.modem, mode='monitor'),
                           telemetry=replace(self.settings.telemetry, enabled=False),
                           web=replace(self.settings.web, enabled=True))
        self.host.addresses['wwan0'] = [{'addr_info': [{'local':'198.18.0.2', 'scope':'global', 'family':'inet'}]}]
        self.assertIn('wwan0', self.details(self.inspect(settings=settings)))

    def test_explicit_global_manager_policy_is_reported_while_idle(self):
        settings = replace(self.settings, modem=replace(self.settings.modem, mode='monitor'),
                           telemetry=replace(self.settings.telemetry, enabled=False))
        self.host.services['ModemManager.service'] = {'active':True, 'enabled':True, 'exists':True}
        self.assertIn('modem-manager-global', self.details(self.inspect(settings=settings,
                                                                      flags=('--modem-manager', 'disable'))))

    def test_idle_web_reports_resolver_package_preparation_without_applying_cellular_dns(self):
        self.write(self.paths.resolver, 'nameserver 192.0.2.53\n')
        settings = replace(self.settings, modem=replace(self.settings.modem, mode='monitor'),
                           network=replace(self.settings.network, dns='auto'),
                           web=replace(self.settings.web, enabled=True))
        self.tools.discard('resolvectl')
        codes = {finding['code'] for finding in self.inspect(settings=settings)}
        self.assertIn('dns-install', codes)
        self.assertNotIn('dns-change', codes)

    def test_no_existing_state_and_unrelated_ethernet_profiles_have_no_takeover_warning(self):
        self.host.services['NetworkManager.service'] = {'active': True, 'enabled': True, 'exists': True}
        self.host.nm_profiles = '11111111-1111-1111-1111-111111111111:802-3-ethernet:Office Ethernet:eth0\n'
        self.host.nm_active = self.host.nm_profiles
        self.assertEqual(self.inspect(), [])

    def test_global_exclusion_checks_other_qmi_interfaces_even_with_selected_modem(self):
        other = '22222222-2222-2222-2222-222222222222'
        self.host.nm_profiles = other+':gsm:Other modem profile:\n'
        self.host.nm_interfaces[other] = 'wwan1\n'
        self.host.nm_active = other+':gsm:Other modem profile:wwan1\n'
        settings = replace(self.settings, modem=replace(self.settings.modem, device='/dev/cdc-wdm0'))
        text = self.details(self.inspect(settings=settings))
        self.assertIn('Other modem profile', text)
        self.assertIn('wwan1', text)
        self.assertTrue(any('wwan1' in command for command in self.host.calls if command[0] == 'ip'))

    def test_nmcli_escaped_profile_name_is_parsed_without_secrets(self):
        identifier = '33333333-3333-3333-3333-333333333333'
        self.host.nm_profiles = identifier+':gsm:Lab\\: modem\\\\profile:wwan0\n'
        self.host.nm_interfaces[identifier] = 'wwan0\n'
        findings = self.inspect()
        text = self.details(findings)
        self.assertIn('Lab: modem\\profile', text.replace('\\\\', '\\'))
        self.assertFalse(any('--show-secrets' in command for command in self.host.calls))
        self.assertFalse(any('gsm.apn' in command or 'gsm.password' in command for command in self.host.calls))

    def test_global_modem_manager_disable_warns_about_connected_other_modems(self):
        modem = '/org/freedesktop/ModemManager1/Modem/7'
        self.host.services['ModemManager.service'] = {'active': True, 'enabled': True, 'exists': True}
        self.host.mm_list = {'modem-list': [modem]}
        self.host.mm_modems[modem] = {'modem': {'generic': {
            'state': 'connected', 'ports': ['wwan9 (net)', 'ttyUSB0 (at)'],
            'bearers': ['/org/freedesktop/ModemManager1/Bearer/3'],
            'equipment-identifier': 'SECRET_IMEI'}, '3gpp': {'operator-name': 'SECRET_OPERATOR'}}}
        findings = self.inspect(flags=('--modem-manager', 'disable'), devices=[])
        text = self.details(findings).lower()
        self.assertIn('all', text)
        self.assertIn('connected', text)
        self.assertIn('1', text)
        with redirect_stdout(self.output):
            preflight.print_findings(findings)
        self.assertNotIn('SECRET_IMEI', self.output.getvalue())
        self.assertNotIn('SECRET_OPERATOR', self.output.getvalue())

    def test_already_masked_inactive_manager_does_not_trigger_global_takeover_warning(self):
        self.host.services['ModemManager.service'] = {'enabled_state': 'masked', 'exists': True}
        self.assertEqual(self.inspect(flags=('--modem-manager', 'disable'), devices=[]), [])

    def test_missing_service_is_known_only_when_loadstate_query_confirms_absence(self):
        self.assertEqual(self.inspect(flags=('--modem-manager', 'disable'), devices=[]), [])
        load_state = ('systemctl', 'show', 'ModemManager.service', '--property=LoadState', '--value')
        self.assertIn(load_state, self.host.calls)
        self.host.failures[load_state] = (1, '', 'inspection failed')
        findings = self.inspect(flags=('--modem-manager', 'disable'), devices=[])
        self.assertIn('modem-manager-unknown', self.details(findings))

    def test_missing_or_invalid_manager_inspection_is_reported_as_unknown(self):
        self.host.services['ModemManager.service'] = {'active': True, 'exists': True}
        for malformed in (None, '{broken JSON', {'unexpected': 'SECRET'}):
            with self.subTest(malformed=malformed):
                if malformed is None:
                    self.tools.discard('mmcli')
                else:
                    self.tools.add('mmcli')
                    self.host.mm_list = malformed
                findings = self.inspect(flags=('--modem-manager', 'disable'), devices=[])
                self.assertIn('unknown', self.details(findings).lower())
                self.assertNotIn('SECRET', self.details(findings))

    def test_active_ips_default_routes_and_ssh_path_warn_with_interface(self):
        self.host.addresses['wwan0'] = [{'ifname': 'wwan0', 'addr_info': [
            {'family': 'inet', 'local': '192.0.2.10', 'prefixlen': 24, 'scope': 'global'},
            {'family': 'inet6', 'local': '2001:db8::10', 'prefixlen': 64, 'scope': 'global'}]}]
        self.host.routes[(4, 'wwan0')] = [{'dst': 'default', 'dev': 'wwan0', 'gateway': '192.0.2.1'}]
        self.host.routes[(6, 'wwan0')] = [{'dst': 'default', 'dev': 'wwan0', 'gateway': '2001:db8::1'}]
        self.host.ssh_route = [{'dst': '198.51.100.4', 'dev': 'wwan0'}]
        findings = self.inspect(environment={'SSH_CONNECTION': '198.51.100.4 12345 192.0.2.10 22'})
        text = self.details(findings)
        self.assertIn('wwan0', text)
        self.assertIn('interface-addresses', text)
        self.assertIn('IPv4 default route', text)
        self.assertIn('IPv6 default route', text)
        self.assertIn('SSH', text)
        self.assertIn(('ip', '-j', 'route', 'get', '198.51.100.4'), self.host.calls)

    def test_untrusted_ssh_connection_is_never_passed_to_shell_or_ip(self):
        self.inspect(environment={'SSH_CONNECTION': '$(touch /tmp/unowned) 5 192.0.2.1 22'})
        self.assertFalse(any('get' in command for command in self.host.calls if command[0] == 'ip'))
        self.assertFalse(any(command[0] in ('sh', '/bin/sh') for command in self.host.calls))

    def test_unchanged_exact_qmi_rule_avoids_warning_just_for_active_network_manager(self):
        self.write(self.paths.rules, provisioning.QMI_NETWORK_RULE)
        self.host.services['NetworkManager.service'] = {'active': True, 'enabled': True, 'exists': True}
        self.assertEqual(self.inspect(), [])

    def test_custom_rule_is_reported_only_when_it_will_be_overwritten(self):
        self.write(self.paths.rules, '# administrator custom rule\n')
        findings = self.inspect()
        self.assertIn(str(self.paths.rules), self.details(findings))
        self.assertEqual(self.inspect(flags=('--network-manager', 'keep')), [])
        monitor = replace(self.settings, modem=replace(self.settings.modem, mode='monitor'))
        self.assertEqual(self.inspect(settings=monitor), [])

    def test_monitor_warns_if_it_will_remove_existing_generated_rule(self):
        self.write(self.paths.rules, provisioning.QMI_NETWORK_RULE)
        monitor = replace(self.settings, modem=replace(self.settings.modem, mode='monitor'))
        text = self.details(self.inspect(settings=monitor))
        self.assertIn(str(self.paths.rules), text)
        self.assertIn('remov', text.lower())

    def test_fallback_profiles_are_read_only_from_temporary_etc_and_run_roots(self):
        self.tools.discard('nmcli')
        for base, name in ((self.paths.config.parent, 'saved'), (self.paths.runtime.parent, 'runtime')):
            self.write(base/'NetworkManager/system-connections'/f'{name}.nmconnection',
                '[connection]\nid = '+name+' modem\ntype = gsm\ninterface-name = wwan1\n'
                '[gsm]\napn = SECRET_APN\npassword = SECRET_PASSWORD\n')
        self.write(self.paths.config.parent/'systemd/network/30-qmi.network',
                   '[Match]\nName=wwan1\n[Network]\nDHCP=yes\n')
        before = self.snapshot()
        findings = self.inspect()
        text = self.details(findings)
        self.assertIn('saved modem', text)
        self.assertIn('runtime modem', text)
        self.assertIn('wwan1', text)
        self.assertNotIn('SECRET_APN', text)
        self.assertNotIn('SECRET_PASSWORD', text)
        self.assertEqual(self.snapshot(), before)

    def test_failed_address_probe_reports_unknown_instead_of_no_configuration(self):
        command = ('ip', '-j', 'addr', 'show', 'dev', 'wwan0')
        for result in ((1, '', 'probe failed'), (0, '{broken JSON', ''),
                       subprocess.TimeoutExpired(command, 5)):
            with self.subTest(result=result):
                self.host.failures[command] = result
                text = self.details(self.inspect()).lower()
                self.assertIn('unknown', text)
                self.assertIn('wwan0', text)

    def test_inactive_daemon_uses_saved_keyfiles_without_activating_it_through_cli(self):
        self.host.services['NetworkManager.service'] = {'active': False, 'exists': True}
        self.write(self.paths.config.parent/'NetworkManager/system-connections/saved.nmconnection',
                   '[connection]\nid=Saved cellular\ntype=gsm\ninterface-name=wwan0\n')
        text = self.details(self.inspect())
        self.assertIn('Saved cellular', text)
        self.assertFalse(any(command[0] == 'nmcli' for command in self.host.calls))

    def test_malformed_fallback_profile_is_unknown_without_printing_its_contents(self):
        self.tools.discard('nmcli')
        self.write(self.paths.config.parent/'NetworkManager/system-connections/broken.nmconnection',
                   '[connection]\nSECRET_PROFILE_LINE_WITHOUT_EQUALS\n')
        text = self.details(self.inspect())
        self.assertIn('network-manager-unknown', text)
        self.assertNotIn('SECRET_PROFILE_LINE', text)

    def test_resolver_change_warning_does_not_print_existing_configuration_contents(self):
        self.write(self.paths.resolver, '# SECRET_RESOLVER_COMMENT\nnameserver 192.0.2.53\n')
        automatic = replace(self.settings, network=replace(self.settings.network, dns='auto'))
        self.tools.discard('resolvectl')
        text = self.details(self.inspect(settings=automatic)).lower()
        self.assertIn('dns', text)
        self.assertNotIn('secret_resolver_comment', text)
        self.assertEqual(self.inspect(settings=self.settings), [])


class ConsentIntegrationTests(PreflightFixture):
    def setUp(self):
        super().setUp()
        for path, content in (
            (self.paths.source/'qmi_supervisor.py', '# fixture application'),
            (self.paths.source/'modules/__init__.py', ''),
            (self.paths.source/'tools/deployment/setup.py', '# fixture setup'),
            (self.paths.source/'tools/deployment/qmi-supervisor.service', '[Service]\n'),
            (self.paths.source/'config/qmi-supervisor.ini.example', '# fixture example'),
            (self.paths.rules, '# administrator existing rule\n')):
            self.write(path, content)

    def apply(self, *, flags=(), tty=False, answer=None, runner=None, action='install'):
        runner = runner or FakeRunner()
        args = provisioning.parser().parse_args([action, *flags])
        with patch.object(provisioning.os, 'geteuid', return_value=0), \
                patch.object(provisioning, 'scan_devices', return_value=[]), \
                patch.object(preflight, 'scan_devices', return_value=[]), \
                patch.object(provisioning.sys.stdin, 'isatty', return_value=tty), \
                patch('builtins.input', side_effect=answer if isinstance(answer, BaseException) else None,
                      return_value=answer) as prompt, redirect_stdout(self.output):
            provisioning.apply(self.settings, self.paths, args, ['python3'], runner=runner)
        return runner, prompt

    def assert_read_only_calls(self, runner):
        for command in runner.calls:
            self.assertIn(command[0], ('systemctl', 'ip', 'nmcli', 'mmcli', 'resolvectl', 'dpkg-query'), command)
            if command[0] == 'systemctl':
                self.assertIn(command[1], ('is-active', 'is-enabled', 'cat', 'show'), command)

    def test_non_terminal_risk_refuses_before_receipt_apt_or_any_mutation(self):
        before = self.snapshot()
        runner = FakeRunner(installed=False)
        with self.assertRaises(ValueError):
            self.apply(runner=runner)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.paths.manifest.exists())
        self.assert_read_only_calls(runner)
        self.assertIn(str(self.paths.rules), self.output.getvalue())

    def test_decline_empty_response_or_eof_leaves_existing_state_unchanged(self):
        for answer in ('n', '', EOFError()):
            with self.subTest(answer=answer):
                before = self.snapshot()
                runner = FakeRunner()
                with self.assertRaises(ValueError):
                    self.apply(tty=True, answer=answer, runner=runner)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.paths.manifest.exists())
                self.assert_read_only_calls(runner)

    def test_declining_reconfiguration_does_not_change_existing_receipt(self):
        self.paths.app.mkdir(parents=True)
        self.write(self.paths.app/'old-version.py', '# current application')
        receipt = provisioning.install_state.new_state(
            {'active': False, 'enabled': False, 'masked': False, 'changed': False},
            {'existed': True, 'content': '# old rule', 'mode': 0o644, 'changed': False})
        provisioning.install_state.save(self.paths, receipt)
        before = self.snapshot()
        runner = FakeRunner()
        with self.assertRaises(ValueError):
            self.apply(action='configure', flags=('--network-manager', 'exclude-qmi'),
                       tty=True, answer='n', runner=runner)
        self.assertEqual(self.snapshot(), before)
        self.assert_read_only_calls(runner)

    def test_acknowledgement_permits_existing_behavior_without_prompting(self):
        runner, prompt = self.apply(flags=('--accept-network-changes',))
        prompt.assert_not_called()
        self.assertTrue(self.paths.manifest.exists())
        self.assertTrue(self.paths.app.exists())
        self.assertEqual(self.paths.rules.read_text(), provisioning.QMI_NETWORK_RULE)
        self.assertIn(str(self.paths.rules), self.output.getvalue())
        self.assertIn(('systemctl', 'restart', provisioning.SERVICE), runner.calls)

    def test_explicit_yes_prompts_once_then_permits_install(self):
        _, prompt = self.apply(tty=True, answer='yes')
        prompt.assert_called_once()
        self.assertTrue(self.paths.manifest.exists())
        self.assertTrue(self.paths.app.exists())

    def test_no_start_and_skip_packages_still_require_risk_acknowledgement(self):
        before = self.snapshot()
        runner = FakeRunner()
        with self.assertRaises(ValueError):
            self.apply(flags=('--no-start', '--skip-packages'), runner=runner)
        self.assertEqual(self.snapshot(), before)
        self.assert_read_only_calls(runner)

    def test_selected_modem_manager_disable_is_not_risk_acknowledgement(self):
        before = self.snapshot()
        runner = FakeRunner(manager_active=True, manager_enabled=True)
        with self.assertRaises(ValueError):
            self.apply(flags=('--modem-manager', 'disable'), runner=runner)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(runner.manager_active)
        self.assert_read_only_calls(runner)

    def test_acknowledgement_does_not_override_modem_manager_keep_conflict(self):
        runner = FakeRunner(manager_active=True, manager_enabled=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'ModemManager is active'):
            self.apply(flags=('--accept-network-changes',), runner=runner)
        self.assertEqual(self.snapshot(), before)
        self.assert_read_only_calls(runner)

    def test_dry_run_reports_real_findings_without_prompt_or_receipt_writes(self):
        before = self.snapshot()
        confirm = provisioning.confirm_network_changes
        with patch.object(provisioning, 'Paths', return_value=self.paths), \
                patch.object(provisioning, 'supported_system', return_value='Debian fixture'), \
                patch.object(provisioning, 'packages_for', return_value=['python3']), \
                patch.object(preflight, 'scan_devices', return_value=[]), \
                patch.object(provisioning, 'confirm_network_changes',
                             side_effect=lambda settings, paths, args: confirm(settings, paths, args, runner=self.host)), \
                patch.object(provisioning, 'apply') as apply, \
                patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', side_effect=AssertionError('dry-run must not prompt')), \
                redirect_stdout(self.output):
            self.assertEqual(provisioning.main(['install', '--connection', 'on', '--web', 'off', '--dry-run']), 0)
        self.assertIn(str(self.paths.rules), self.output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.paths.manifest.exists())
        apply.assert_not_called()

    def test_interactive_dry_run_never_opens_the_configuration_wizard_or_prompt(self):
        before = self.snapshot()
        with patch.object(provisioning, 'Paths', return_value=self.paths), \
                patch.object(provisioning, 'supported_system', return_value='Debian fixture'), \
                patch.object(provisioning, 'packages_for', return_value=['python3']), \
                patch.object(preflight, 'inspect_host', return_value=[]) as inspect, \
                patch.object(provisioning, 'interactive', side_effect=AssertionError('No wizard on dry-run')), \
                patch.object(provisioning, 'apply') as apply, \
                patch('builtins.input', side_effect=AssertionError('No input on dry-run')), \
                redirect_stdout(self.output):
            self.assertEqual(provisioning.main(['install', '--web', 'off', '--interactive', '--dry-run']), 0)
        inspect.assert_called_once()
        apply.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_no_planned_impact_needs_no_prompt(self):
        self.paths.rules.unlink()
        # Use explicit no-DNS policy and empty device inspection to isolate a new, unused host.
        with patch.object(preflight, 'inspect_host', return_value=[]) as inspect:
            _, prompt = self.apply(tty=False)
        inspect.assert_called_once()
        prompt.assert_not_called()
        self.assertTrue(self.paths.app.exists())

    def test_uninstall_rejects_install_only_risk_acknowledgement(self):
        with patch('modules.deployment.uninstall.uninstall') as remove, \
                redirect_stdout(self.output), patch.object(provisioning.sys, 'stderr', self.output):
            with self.assertRaises(SystemExit):
                provisioning.main(['uninstall', '--accept-network-changes'])
        remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()
