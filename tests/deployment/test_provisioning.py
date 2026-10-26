from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import json
import shutil
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from modules.deployment import provisioning
from modules.runtime.settings import Settings, load_settings, validate_settings
from tests.web.static_fixture import make_export


class FakeRunner:
    """Model packages and services in memory, without running host processes."""

    def __init__(self, *, active=False, enabled=False, manager_active=False,
                 manager_enabled=False, manager_masked=False, installed=True,
                 packages=None, install_dependencies=(), partial_packages=(),
                 residual_packages=(), package_statuses=None, simulation_extras=(), failures=()):
        self.active = active
        self.enabled = enabled
        self.manager_active = manager_active
        self.manager_enabled = manager_enabled
        self.manager_masked = manager_masked
        self.installed = installed
        self.packages = set(packages if packages is not None else
                            provisioning.BASE_PACKAGES if installed else ())
        self.residual_packages = set(residual_packages)
        self.package_statuses = dict(package_statuses or {})
        self.install_dependencies = set(install_dependencies)
        self.partial_packages = set(partial_packages)
        self.simulation_extras = set(simulation_extras)
        self.failures = {tuple(command) for command in failures}
        self.calls = []

    def __call__(self, args, *, check=True, input=None, timeout=None):
        command = tuple(args)
        self.calls.append(command)
        code, stdout, stderr = 0, '', ''
        if command in self.failures:
            code, stderr = 1, 'simulated command failure'
            if command[:2] == ('apt-get', 'install'):
                self.packages.update(self.partial_packages)
        elif command[0] == 'dpkg-query':
            if len(command) == 3:
                stdout = ''.join(f'{package}\tinstall ok installed\n'
                                 for package in sorted(self.packages))
                stdout += ''.join(f'{package}\tdeinstall ok config-files\n'
                                  for package in sorted(self.residual_packages))
                stdout += ''.join(f'{package}\t{status}\n'
                                  for package, status in sorted(self.package_statuses.items()))
            else:
                package = command[-1]
                code = 0 if package in self.packages or package in self.package_statuses else 1
                stdout = self.package_statuses.get(package, 'install ok installed' if code == 0 else '')
        elif command[0] == 'apt-get' and 'purge' in command:
            candidates = {item for item in command[command.index('purge')+1:]
                          if not item.startswith('-')}
            if '--simulate' in command or '-s' in command:
                stdout = ''.join(f'Purg {package} [1.0]\n' for package in
                                 sorted((candidates & (self.packages | self.residual_packages | set(self.package_statuses)))
                                        | self.simulation_extras))
            else:
                self.packages.difference_update(candidates)
                self.residual_packages.difference_update(candidates)
                for package in candidates:
                    self.package_statuses.pop(package, None)
        elif command[:2] == ('apt-get', 'install'):
            newly_installed = {item for item in command[2:] if not item.startswith('-')} | self.install_dependencies
            self.packages.update(newly_installed)
            self.residual_packages.difference_update(newly_installed)
            for package in newly_installed:
                self.package_statuses.pop(package, None)
        elif command[:2] == ('systemctl', 'is-active'):
            active = self.active if command[-1] == provisioning.SERVICE else self.manager_active
            code, stdout = (0, 'active\n') if active else (3, 'inactive\n')
        elif command[:2] == ('systemctl', 'is-enabled'):
            manager = command[-1] != provisioning.SERVICE
            enabled = self.manager_enabled if manager else self.enabled
            masked = manager and self.manager_masked
            code = 0 if enabled and not masked else 1
            stdout = 'masked\n' if masked else 'enabled\n' if enabled else 'disabled\n'
        elif command[:2] == ('systemctl', 'show'):
            service = provisioning.SERVICE in command
            active = self.active if service else self.manager_active
            stdout = 'active\n' if active else 'inactive\n'
        elif command[:2] == ('systemctl', 'stop'):
            if command[-1] == provisioning.SERVICE:
                self.active = False
            else:
                self.manager_active = False
        elif command[:2] in {('systemctl', 'start'), ('systemctl', 'restart')}:
            if command[-1] == provisioning.SERVICE:
                self.active = True
            else:
                self.manager_active = True
        elif command[:2] == ('systemctl', 'enable'):
            if command[-1] == provisioning.SERVICE:
                self.enabled = True
            else:
                self.manager_enabled = True
        elif command[:2] == ('systemctl', 'disable'):
            if command[-1] == provisioning.SERVICE:
                self.enabled = False
                if '--now' in command:
                    self.active = False
            else:
                self.manager_enabled = False
                if '--now' in command:
                    self.manager_active = False
        elif command[:2] == ('systemctl', 'mask'):
            self.manager_masked = True
            if '--now' in command:
                self.manager_active = False
        elif command[:2] == ('systemctl', 'unmask'):
            self.manager_masked = False
        result = subprocess.CompletedProcess(list(args), code, stdout, stderr)
        if check and code:
            raise subprocess.CalledProcessError(code, list(args), output=stdout, stderr=stderr)
        return result


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='qmi-provisioning-tests-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = provisioning.Paths(
            source=self.root/'source', app=self.root/'app root',
            config=self.root/'etc/config.ini', unit=self.root/'etc/systemd/qmi-supervisor.service',
            command=self.root/'bin/qmi-supervisor-configure', state=self.root/'state',
            rules=self.root/'etc/udev/rules.d/85-qmi-supervisor.rules',
            runtime=self.root/'run/qmi-supervisor',
        )
        self.paths.source.mkdir()
        files = {
            'qmi_supervisor.py': 'new application: qmi_supervisor.py',
            'modules/__init__.py': '# new modules\n',
            'modules/telemetry/metrics.py': '# metric definitions\n',
            'modules/network/dhcp_hook.py': '# DHCP hook\n',
            'modules/web/__main__.py': '# optional web process\n',
            'tools/deployment/qmi-supervisor.service': '[Service]\nType=simple\n',
            'tools/deployment/setup.py': '# new setup tool\n',
            'tools/deployment/install.sh': '# new install tool\n',
            'tools/deployment/configure.sh': '# new configure tool\n',
            'tools/deployment/uninstall.sh': '# new uninstall tool\n',
            'config/qmi-supervisor.ini.example': '# example configuration\n',
            'legacy/dashboard.js': '// undeclared source tree',
        }
        for relative, content in files.items():
            target = self.paths.source/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        make_export(self.paths.source/'frontend/out')
        self.output = StringIO()

    def arguments(self, *args):
        return provisioning.parser().parse_args(args or ('install',))

    def save(self, settings):
        self.paths.config.parent.mkdir(parents=True, exist_ok=True)
        self.paths.config.write_text(provisioning.render_config(settings))

    def existing_installation(self):
        settings = replace(Settings(), modem=replace(Settings().modem, apn='old.example', serial='old-serial'))
        self.save(settings)
        self.paths.app.mkdir()
        (self.paths.app/'old-version').write_text('old application')
        self.paths.unit.parent.mkdir(parents=True)
        self.paths.unit.write_text('old unit')
        self.paths.command.parent.mkdir(parents=True)
        self.paths.command.write_text('old command')
        return settings

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def apply(self, settings, runner, *arguments):
        args = self.arguments(*(arguments or ('install',)), '--accept-network-changes')
        with patch.object(provisioning.os, 'geteuid', return_value=0), \
                patch.object(provisioning, 'scan_devices', return_value=[]), \
                patch('modules.deployment.preflight.scan_devices', return_value=[]), redirect_stdout(self.output):
            provisioning.apply(settings, self.paths, args, ['python3'], runner=runner)

    def test_new_configuration_has_no_hardware_identity(self):
        settings = provisioning.candidate_settings(self.arguments('install'), self.paths)
        self.assertEqual(settings.modem.device, 'auto')
        self.assertEqual(settings.modem.serial, '')
        self.assertEqual(settings.modem.apn, 'internet')
        self.assertEqual(settings.telemetry.directory, str(self.paths.state))
        self.assertFalse(self.paths.config.exists())
        self.assertNotIn('612EDE63', provisioning.render_config(settings))

    def test_selective_flags_preserve_existing_configuration(self):
        original = replace(Settings(), modem=replace(Settings().modem, serial='chosen', apn='custom.apn'),
                           network=replace(Settings().network, route_metric=925, dns='none'),
                           telemetry=replace(Settings().telemetry, interval=17, csv=False, gps=True,
                                             collector_host='collector.example'))
        self.save(original)
        before = self.paths.config.read_bytes()
        settings = provisioning.candidate_settings(
            self.arguments('configure', '--apn', 'new.apn', '--throughput', 'on'), self.paths)
        self.assertEqual(settings, replace(original, modem=replace(original.modem, apn='new.apn'),
                                          telemetry=replace(original.telemetry, throughput=True)))
        self.assertEqual(self.paths.config.read_bytes(), before)

    def test_auto_modem_clears_both_selectors(self):
        original = replace(Settings(), modem=replace(Settings().modem, serial='chosen', device='/dev/cdc-wdm8'))
        self.save(original)
        settings = provisioning.candidate_settings(self.arguments('configure', '--auto-modem'), self.paths)
        self.assertEqual(settings.modem, replace(original.modem, serial='', device='auto'))

    def test_explicit_test_network_and_apn_are_configurable_without_hardcoding_hardware(self):
        settings = provisioning.candidate_settings(self.arguments(
            'install', '--plmn', '99910', '--apn', 'tenten'), self.paths)
        self.assertEqual((settings.modem.plmn, settings.modem.apn), ('99910', 'tenten'))
        self.assertEqual((settings.modem.device, settings.modem.serial), ('auto', ''))
        self.save(settings)
        automatic = provisioning.candidate_settings(self.arguments('configure', '--plmn', 'auto'), self.paths)
        self.assertEqual(automatic.modem.plmn, 'auto')
        self.assertEqual(automatic.modem.apn, 'tenten')

    def test_explicit_selector_replaces_previous_selector(self):
        original = replace(Settings(), modem=replace(Settings().modem, serial='old', device='/dev/cdc-wdm8'))
        self.save(original)
        by_serial = provisioning.candidate_settings(self.arguments('configure', '--serial', 'new'), self.paths)
        self.assertEqual((by_serial.modem.device, by_serial.modem.serial), ('auto', 'new'))
        by_device = provisioning.candidate_settings(self.arguments('configure', '--device', '/dev/cdc-wdm2'), self.paths)
        self.assertEqual((by_device.modem.device, by_device.modem.serial), ('/dev/cdc-wdm2', ''))

    def test_network_profile_and_feature_flags(self):
        settings = provisioning.candidate_settings(self.arguments(
            'install', '--method', 'dhcp', '--network-profile', 'primary', '--dns', 'none',
            '--telemetry', 'off', '--gps', 'on', '--collector-host', 'collector.example'), self.paths)
        self.assertEqual((settings.network.method, settings.network.route_metric,
                          settings.network.default_route, settings.network.dns), ('dhcp', 100, True, 'none'))
        self.assertEqual(settings.network.route_policy, 'primary')
        self.assertFalse(settings.telemetry.enabled)
        self.assertTrue(settings.telemetry.gps)
        self.assertEqual(settings.telemetry.collector_host, 'collector.example')
        no_default = provisioning.candidate_settings(self.arguments(
            'install', '--network-profile', 'no-default', '--route-metric', '813'), self.paths)
        self.assertFalse(no_default.network.default_route)
        self.assertEqual(no_default.network.route_metric, 813)
        self.assertEqual(no_default.network.route_policy, 'fixed')

    def test_route_profiles_set_explicit_policy_with_canonical_or_custom_bases(self):
        original = replace(Settings(), network=replace(Settings().network, route_metric=925,
                                                       route_policy='fixed', default_route=False))
        self.save(original)
        for profile, canonical in (('primary', 100), ('backup', 700)):
            with self.subTest(profile=profile):
                network = provisioning.candidate_settings(self.arguments('configure', '--network-profile', profile), self.paths).network
                self.assertEqual((network.route_policy, network.route_metric, network.default_route),
                                 (profile, canonical, True))
                for metric in (0, 813, 4294967295):
                    customized = provisioning.candidate_settings(self.arguments('configure', '--network-profile', profile,
                                                                               '--route-metric', str(metric)), self.paths).network
                    self.assertEqual((customized.route_policy, customized.route_metric, customized.default_route),
                                     (profile, metric, True))

    def test_route_metric_alone_returns_to_fixed_and_no_default_alone_preserves_policy_and_base(self):
        original = replace(Settings(), network=replace(Settings().network, route_metric=57, route_policy='primary'))
        self.save(original)
        disabled = provisioning.candidate_settings(self.arguments('configure', '--network-profile', 'no-default'), self.paths)
        self.assertEqual(disabled.network, replace(original.network, default_route=False))
        exact = provisioning.candidate_settings(self.arguments('configure', '--route-metric', '100'), self.paths)
        self.assertEqual(exact.network, replace(original.network, route_metric=100, route_policy='fixed'))
        customized_disabled = provisioning.candidate_settings(self.arguments('configure', '--network-profile', 'no-default',
                                                                           '--route-metric', '813'), self.paths)
        self.assertEqual(customized_disabled.network, replace(original.network, default_route=False,
                                                             route_metric=813, route_policy='fixed'))

    def test_route_metrics_validate_boundaries_and_legacy_ini_roundtrip_stays_fixed(self):
        self.paths.config.parent.mkdir(parents=True)
        self.paths.config.write_text('[network]\nroute_metric=100\n')
        legacy = provisioning.candidate_settings(self.arguments('configure', '--apn', 'new.example'), self.paths)
        self.assertEqual((legacy.network.route_metric, legacy.network.route_policy), (100, 'fixed'))
        self.save(legacy)
        self.assertEqual(load_settings(self.paths.config), legacy)
        for metric in (0, 4294967295):
            configured = provisioning.candidate_settings(self.arguments('configure', '--route-metric', str(metric)), self.paths)
            self.assertEqual((configured.network.route_metric, configured.network.route_policy), (metric, 'fixed'))
        for metric in (-1, 4294967296):
            with self.subTest(metric=metric), self.assertRaises(ValueError):
                provisioning.candidate_settings(self.arguments('configure', '--route-metric', str(metric)), self.paths)

    def test_wizard_preserves_fixed_legacy_values_and_selects_dynamic_policy_coherently(self):
        for original_policy, original_metric, selected, expected_policy, expected_metric in (
                ('fixed', 100, 'fixed', 'fixed', 100), ('primary', 57, 'primary', 'primary', 57),
                ('fixed', 925, 'primary', 'primary', 100), ('fixed', 925, 'backup', 'backup', 700),
                ('backup', 813, 'no-default', 'backup', 813)):
            settings = replace(Settings(), network=replace(Settings().network, route_policy=original_policy,
                                                           route_metric=original_metric))
            def answer(label, current, choices=None):
                return selected if label == 'Cellular route' else str(current)
            with self.subTest(policy=original_policy, selected=selected), \
                    patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                    patch.object(provisioning, 'ask', side_effect=answer):
                configured = provisioning.interactive(settings)
            self.assertEqual((configured.network.route_policy, configured.network.route_metric),
                             (expected_policy, expected_metric))
            self.assertEqual(configured.network.default_route, selected != 'no-default')

    def test_connectivity_flags_roundtrip_and_selective_changes_preserve_private_targets(self):
        original = provisioning.candidate_settings(self.arguments('install', '--network-profile', 'primary',
            '--connectivity-targets', '10.20.0.1:8443,192.168.1.2:53', '--connectivity-interval', '4',
            '--connectivity-timeout', '0.5', '--connectivity-failures', '3', '--connectivity-successes', '1'), self.paths)
        self.assertTrue(original.network.connectivity_check)
        self.assertEqual((original.network.connectivity_interval, original.network.connectivity_timeout,
                          original.network.connectivity_failures, original.network.connectivity_successes), (4, 0.5, 3, 1))
        self.save(original)
        self.assertEqual(load_settings(self.paths.config), original)
        disabled = provisioning.candidate_settings(self.arguments('configure', '--connectivity-check', 'off'), self.paths)
        self.assertEqual(disabled.network, replace(original.network, connectivity_check=False))
        self.save(disabled)
        changed_apn = provisioning.candidate_settings(self.arguments('configure', '--apn', 'private.apn'), self.paths)
        self.assertEqual(changed_apn.network, disabled.network)
        enabled = provisioning.candidate_settings(self.arguments('configure', '--connectivity-check', 'on'), self.paths)
        self.assertEqual(enabled.network, original.network)

    def test_invalid_connectivity_flags_fail_before_configuration_is_written(self):
        for flags in (('--connectivity-targets', 'server.example:443'), ('--connectivity-targets', '127.0.0.1:443'),
                      ('--connectivity-interval', '0.5'), ('--connectivity-timeout', '4'),
                      ('--connectivity-failures', '0'), ('--connectivity-successes', '11')):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                provisioning.candidate_settings(self.arguments('configure', *flags), self.paths)
        self.assertFalse(self.paths.config.exists())

    def test_wizard_connectivity_choice_changes_no_advanced_settings(self):
        settings = replace(Settings(), network=replace(Settings().network, connectivity_targets='10.0.0.1:443',
                                                       connectivity_interval=4, connectivity_failures=3))
        def answer(label, current, choices=None):
            return 'off' if label == 'Verify WWAN connectivity before preferring its route' else str(current)
        with patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                patch.object(provisioning, 'ask', side_effect=answer):
            configured = provisioning.interactive(settings)
        self.assertEqual(configured.network, replace(settings.network, connectivity_check=False))

    def test_sampling_flags_support_fractional_interval_and_explicit_budget(self):
        settings = provisioning.candidate_settings(self.arguments(
            'install', '--sample-interval', '0.25', '--telemetry-profile', 'fast',
            '--collection-budget', '0.18'), self.paths)
        self.assertEqual(settings.telemetry.interval, 0.25)
        self.assertEqual(settings.telemetry.profile, 'fast')
        self.assertEqual(settings.telemetry.collection_budget, 0.18)
        self.save(settings)
        self.assertEqual(load_settings(self.paths.config), settings)

    def test_shorter_sampling_interval_adjusts_implicit_budget_and_preserves_other_settings(self):
        original = replace(Settings(),
            telemetry=replace(Settings().telemetry, interval=2.0, collection_budget=0.7, profile='standard'),
            web=replace(Settings().web, history_size=90))
        self.save(original)
        before = self.paths.config.read_bytes()
        settings = provisioning.candidate_settings(self.arguments(
            'configure', '--sample-interval', '0.1'), self.paths)
        self.assertEqual(settings.telemetry.interval, 0.1)
        self.assertAlmostEqual(settings.telemetry.collection_budget, 0.08)
        self.assertEqual(settings.telemetry.profile, original.telemetry.profile)
        self.assertEqual(settings.web, original.web)
        self.assertEqual(self.paths.config.read_bytes(), before)

    def test_invalid_sampling_values_do_not_change_configuration(self):
        self.existing_installation()
        before = self.snapshot()
        cases = (
            ('--sample-interval', '0'), ('--sample-interval', 'nan'),
            ('--collection-budget', '0'), ('--collection-budget', 'inf'),
            ('--sample-interval', '0.1', '--collection-budget', '0.2'),
        )
        for flags in cases:
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                provisioning.candidate_settings(self.arguments('configure', *flags), self.paths)
            self.assertEqual(self.snapshot(), before)

    def test_enabling_web_supports_remote_address_and_port_before_the_account_exists(self):
        settings = provisioning.candidate_settings(self.arguments(
            'install', '--web', 'on', '--web-host', '0.0.0.0', '--web-port', '8090'), self.paths)
        self.assertTrue(settings.web.enabled)
        self.assertEqual(settings.web.password_hash, '')
        self.assertEqual((settings.web.host, settings.web.port), ('0.0.0.0', 8090))
        self.assertEqual((settings.modem.device, settings.modem.serial), ('auto', ''))
        self.assertFalse(self.paths.config.exists())
        # The plan is not startable until the account exists.
        with self.assertRaises(ValueError):
            validate_settings(settings)

    def test_web_toggle_preserves_existing_web_settings_and_unrelated_configuration(self):
        original = replace(Settings(), modem=replace(Settings().modem, apn='fleet.apn'),
                           web=replace(Settings().web, enabled=True, history_size=90, port=8123))
        self.save(original)
        for flags, enabled in (((), True), (('--web', 'off'), False), (('--web', 'on'), True)):
            with self.subTest(flags=flags):
                settings = provisioning.candidate_settings(self.arguments('configure', *flags), self.paths)
            self.assertEqual(settings.web, replace(original.web, enabled=enabled))
            self.assertEqual(settings.modem, original.modem)

    def test_disabling_fresh_web_keeps_credentials_empty(self):
        settings = provisioning.candidate_settings(self.arguments('install', '--web', 'off'), self.paths)
        self.assertFalse(settings.web.enabled)
        self.assertEqual(settings.web.password_hash, '')

    def test_invalid_web_port_does_not_change_files(self):
        original = replace(Settings(), web=replace(Settings().web, history_size=90))
        self.save(original)
        before = self.snapshot()
        for flags in (('--web-port', '0'), ('--web-port', '65536')):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                provisioning.candidate_settings(self.arguments('configure', *flags), self.paths)
            self.assertEqual(self.snapshot(), before)

    def test_invalid_values_do_not_change_files(self):
        self.existing_installation()
        before = self.snapshot()
        for flags in (('--route-metric', '-1'), ('--apn', ''), ('--device', 'relative')):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                provisioning.candidate_settings(self.arguments('configure', *flags), self.paths)
            self.assertEqual(self.snapshot(), before)

    def test_main_invalid_configuration_does_not_apply(self):
        self.existing_installation()
        before = self.snapshot()
        with patch.object(provisioning, 'Paths', return_value=self.paths), \
                patch.object(provisioning, 'apply') as apply, redirect_stderr(self.output):
            result = provisioning.main(['configure', '--route-metric', '-1'])
        self.assertEqual(result, 1)
        apply.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_qmi_and_dns_none_need_only_base_packages(self):
        settings = replace(Settings(), network=replace(Settings().network, dns='none'))
        runner, which = Mock(), Mock()
        self.assertEqual(provisioning.packages_for(settings, runner=runner, which=which),
                         list(provisioning.BASE_PACKAGES))
        runner.assert_not_called()
        which.assert_not_called()

    def test_dhcp_and_enabled_gps_add_operational_dependencies(self):
        settings = replace(Settings(), network=replace(Settings().network, method='dhcp', dns='none'),
                           telemetry=replace(Settings().telemetry, gps=True))
        packages = provisioning.packages_for(settings, runner=Mock(), which=Mock())
        self.assertEqual(packages, [*provisioning.BASE_PACKAGES, 'udhcpc', 'gpsd'])
        disabled = replace(settings, telemetry=replace(settings.telemetry, enabled=False))
        self.assertIn('gpsd', provisioning.packages_for(disabled, runner=Mock(), which=Mock()))
        modem_gps = replace(settings, telemetry=replace(settings.telemetry, gps_source='modem'))
        self.assertNotIn('gpsd', provisioning.packages_for(modem_gps, runner=Mock(), which=Mock()))

    def test_auto_dns_uses_operational_resolved(self):
        runner = FakeRunner()
        packages = provisioning.packages_for(Settings(), runner=runner, which=lambda name: '/usr/bin/'+name)
        self.assertNotIn('resolvconf', packages)
        self.assertEqual(runner.calls, [('resolvectl', 'status')])

    def test_auto_dns_installs_fallback_for_missing_or_failed_resolved(self):
        for available in (False, True):
            with self.subTest(available=available):
                runner = FakeRunner(failures=[('resolvectl', 'status')])
                packages = provisioning.packages_for(Settings(), runner=runner,
                    which=lambda name: '/usr/bin/'+name if available else None)
                self.assertIn('resolvconf', packages)
                self.assertEqual(runner.calls, [('resolvectl', 'status')] if available else [])

    def test_explicit_resolved_requires_working_service(self):
        settings = replace(Settings(), network=replace(Settings().network, dns='resolvectl'))
        for available in (False, True):
            with self.subTest(available=available), self.assertRaises(ValueError):
                provisioning.packages_for(settings,
                    runner=FakeRunner(failures=[('resolvectl', 'status')]),
                    which=lambda name: '/usr/bin/'+name if available else None)
        self.assertNotIn('resolvconf', provisioning.packages_for(settings,
            runner=FakeRunner(), which=lambda name: '/usr/bin/'+name))

    def test_explicit_resolvconf_and_monitor_dependencies(self):
        settings = replace(Settings(), network=replace(Settings().network, method='dhcp', dns='resolvconf'))
        self.assertIn('resolvconf', provisioning.packages_for(settings, runner=Mock(), which=Mock()))
        monitor = replace(settings, modem=replace(settings.modem, mode='monitor'))
        self.assertEqual(provisioning.packages_for(monitor, runner=Mock(), which=Mock()),
                         list(provisioning.BASE_PACKAGES))

    def test_missing_packages_distinguishes_installed_status(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, 'install ok installed'),
                                   subprocess.CompletedProcess([], 0, 'deinstall ok config-files'),
                                   subprocess.CompletedProcess([], 1, '')])
        self.assertEqual(provisioning.missing_packages(['present', 'removed', 'absent'], runner),
                         ['removed', 'absent'])

    def test_apt_failure_does_not_deploy_or_stop_service(self):
        self.existing_installation()
        before = self.snapshot()
        runner = FakeRunner(active=True, installed=False,
                            failures=[('apt-get', 'install', '-y', '--no-remove', 'python3')])
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(Settings(), runner)
        after = self.snapshot()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(set(after)-set(before), {str(self.paths.manifest.relative_to(self.root))})
        receipt = provisioning.install_state.load(self.paths)
        self.assertFalse(receipt['completed'])
        self.assertEqual(receipt['packages'], [])
        self.assertTrue(runner.active)
        self.assertIn(('apt-get', 'update'), runner.calls)
        self.assertNotIn(('systemctl', 'stop', provisioning.SERVICE), runner.calls)

    def test_install_success_deploys_app_config_wrapper_and_service(self):
        settings = provisioning.candidate_settings(self.arguments('install'), self.paths)
        runner = FakeRunner()
        self.apply(settings, runner)
        self.assertEqual(load_settings(self.paths.config), settings)
        self.assertEqual((self.paths.app/'qmi_supervisor.py').read_text(), 'new application: qmi_supervisor.py')
        self.assertEqual((self.paths.app/'modules/__init__.py').read_text(), '# new modules\n')
        self.assertEqual(self.paths.unit.read_text(),
                         (self.paths.source/'tools/deployment/qmi-supervisor.service').read_text())
        self.assertIn(str(self.paths.app/'tools/deployment/setup.py'), self.paths.command.read_text())
        self.assertIn('uninstall "$@"', self.paths.uninstall_command.read_text())
        self.assertEqual(self.paths.uninstall_command.stat().st_mode & 0o777, 0o755)
        for relative in ('modules/telemetry/metrics.py', 'modules/network/dhcp_hook.py',
                         'tools/deployment/install.sh', 'config/qmi-supervisor.ini.example'):
            self.assertEqual((self.paths.app/relative).read_bytes(), (self.paths.source/relative).read_bytes())
        self.assertEqual(self.paths.command.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.paths.config.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.paths.state.is_dir())
        self.assertTrue(runner.active)
        self.assertTrue(runner.enabled)
        self.assertEqual(self.paths.rules.read_text(), provisioning.QMI_NETWORK_RULE)
        self.assertIn(('udevadm', 'control', '--reload-rules'), runner.calls)
        self.assertIn(('modprobe', 'qmi_wwan'), runner.calls)
        self.assertIn(('systemctl', 'enable', provisioning.SERVICE), runner.calls)
        self.assertIn(('systemctl', 'restart', provisioning.SERVICE), runner.calls)
        self.assertFalse(list(self.root.glob('.qmi-supervisor-stage-*')))

    def test_install_deploys_optional_web_assets_without_extra_packages(self):
        settings = provisioning.candidate_settings(self.arguments('install', '--web', 'on'), self.paths)
        runner = FakeRunner()
        self.apply(settings, runner)
        self.assertTrue(load_settings(self.paths.config).web.enabled)
        for relative in ('modules/web/__main__.py', 'frontend/out/index.html',
                         'frontend/out/dashboard/index.html', 'frontend/out/_next/static/chunks/0123456789abcdef.js'):
            self.assertEqual((self.paths.app/relative).read_bytes(), (self.paths.source/relative).read_bytes())
        self.assertFalse((self.paths.app/'legacy').exists(), 'only declared source trees are deployed')
        self.assertFalse((self.paths.app/'frontend/node_modules').exists())
        connection = replace(settings, network=replace(settings.network, dns='none'))
        without_web = replace(connection, web=replace(connection.web, enabled=False))
        self.assertEqual(set(provisioning.packages_for(connection, runner=Mock(), which=Mock())) -
                         set(provisioning.packages_for(without_web, runner=Mock(), which=Mock())), {'udhcpc'})

    def test_missing_or_incomplete_web_export_fails_before_packages_receipts_or_service_commands(self):
        settings = replace(Settings(), web=replace(Settings().web, enabled=True))
        (self.paths.source/'frontend/out/configuration/web/index.html').unlink()
        before = self.snapshot()
        runner = FakeRunner(installed=False)
        with self.assertRaisesRegex(ValueError, 'frontend export'):
            self.apply(settings, runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.paths.manifest.exists())
        shutil.rmtree(self.paths.source/'frontend/out')
        with self.assertRaisesRegex(ValueError, 'npm ci'):
            self.apply(settings, runner)
        self.assertEqual(runner.calls, [])

    def test_configure_web_checks_deployed_export_and_keeps_headless_install_supported(self):
        self.existing_installation()
        settings = replace(Settings(), web=replace(Settings().web, enabled=True))
        runner = FakeRunner()
        with self.assertRaisesRegex(ValueError, 'frontend export'):
            self.apply(settings, runner, 'configure')
        self.assertEqual(runner.calls, [])
        shutil.rmtree(self.paths.source/'frontend/out')
        stage = provisioning.stage_application(self.paths)
        try:
            self.assertFalse((stage/'frontend').exists())
            self.assertTrue((stage/'qmi_supervisor.py').is_file())
        finally:
            shutil.rmtree(stage)

    def test_staging_contains_only_export_not_node_dependencies_sources_or_build_cache(self):
        for name in ('README.md', 'INSTALLATION.md', 'CONFIGURATION.md', 'LICENSE'):
            (self.paths.source/name).write_text('Retain ' + name)
        for relative in ('frontend/node_modules/dependency/index.js', 'frontend/.next/cache/private',
                         'frontend/src/app/page.tsx', 'frontend/package.json'):
            target = self.paths.source/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('build-time files must remain on the PC')
        stage = provisioning.stage_application(self.paths)
        try:
            self.assertEqual(sorted(path.name for path in (stage/'frontend').iterdir()), ['out'])
            self.assertTrue((stage/'frontend/out/dashboard/index.html').is_file())
            self.assertFalse((stage/'web').exists())
            for name in ('README.md', 'INSTALLATION.md', 'CONFIGURATION.md', 'LICENSE'):
                self.assertEqual((stage/name).read_text(), 'Retain ' + name)
        finally:
            shutil.rmtree(stage)

    def test_no_start_leaves_previously_active_service_stopped(self):
        self.existing_installation()
        runner = FakeRunner(active=True)
        self.apply(Settings(), runner, 'install', '--no-start')
        self.assertFalse(runner.active)
        self.assertIn(('systemctl', 'stop', provisioning.SERVICE), runner.calls)
        self.assertNotIn(('systemctl', 'enable', provisioning.SERVICE), runner.calls)
        self.assertNotIn(('systemctl', 'restart', provisioning.SERVICE), runner.calls)
        self.assertNotIn(('systemctl', 'start', provisioning.SERVICE), runner.calls)

    def test_configuration_of_installed_manager_applies_without_a_whole_service_restart(self):
        settings = self.existing_installation()
        shutil.copytree(self.paths.source/'tools', self.paths.app/'tools')
        unit = '[Service]\nType=notify\n'
        (self.paths.app/'tools/deployment/qmi-supervisor.service').write_text(unit)
        self.paths.unit.write_text(unit)
        (self.paths.app/'qmi_supervisor.py').write_text('from modules.runtime.processes import ProcessManager\n')
        runner = FakeRunner(active=True)
        candidate = replace(settings, telemetry=replace(settings.telemetry, enabled=False))
        self.apply(candidate, runner, 'configure')
        self.assertTrue(runner.active)
        self.assertEqual(load_settings(self.paths.config), candidate)
        self.assertNotIn(('systemctl', 'stop', provisioning.SERVICE), runner.calls)
        self.assertNotIn(('systemctl', 'restart', provisioning.SERVICE), runner.calls)
        self.assertTrue(list(self.paths.config.parent.glob(self.paths.config.name+'.bak-*')))

    def test_restart_failure_restores_previous_application_and_configuration(self):
        old_settings = self.existing_installation()
        old_config = self.paths.config.read_bytes()
        runner = FakeRunner(active=True, failures=[('systemctl', 'restart', provisioning.SERVICE)])
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(Settings(), runner)
        self.assertEqual(self.paths.config.read_bytes(), old_config)
        self.assertEqual(load_settings(self.paths.config), old_settings)
        self.assertEqual((self.paths.app/'old-version').read_text(), 'old application')
        self.assertFalse((self.paths.app/'qmi_supervisor.py').exists())
        self.assertEqual(self.paths.unit.read_text(), 'old unit')
        self.assertEqual(self.paths.command.read_text(), 'old command')
        self.assertFalse(self.paths.rules.exists())
        self.assertTrue(runner.active)
        self.assertFalse(runner.enabled)
        self.assertEqual(runner.calls[-1], ('systemctl', 'start', provisioning.SERVICE))
        self.assertTrue(list(self.root.glob('app root.failed-*')))
        self.assertFalse(list(self.root.glob('.qmi-supervisor-stage-*')))

    def test_rollback_to_a_legacy_binary_keeps_pending_physical_ownership_stopped_and_intact(self):
        self.existing_installation()
        checkpoint = self.paths.runtime/'ownership/physical-ue/connection-state.json'
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text('{"version":1,"identity":"ue-1","cid":4,"handle":42}')
        runner = FakeRunner(active=True, failures=[('systemctl', 'restart', provisioning.SERVICE)])
        with redirect_stderr(self.output), self.assertRaises(subprocess.CalledProcessError):
            self.apply(Settings(), runner)
        self.assertFalse(runner.active)
        self.assertNotIn(('systemctl', 'start', provisioning.SERVICE), runner.calls)
        self.assertTrue(checkpoint.exists())
        self.assertIn('cannot recover pending physical-modem ownership', self.output.getvalue())
        self.assertTrue((self.paths.app/'old-version').exists())

    def test_rollback_compatibility_checks_pending_transactions_and_actual_recovery_features(self):
        self.existing_installation()
        transaction = self.paths.runtime/'ownership/physical-ue/migration.json'
        transaction.parent.mkdir(parents=True)
        transaction.write_text('{"version":1,"files":[]}')
        self.assertFalse(provisioning.can_restart_rollback(self.paths))
        for name, source in (
                ('qmi_supervisor.py', 'from modules.runtime.fleet import FleetManager'),
                ('modules/runtime/fleet.py', 'from modules.runtime.ownership import OwnershipStore'),
                ('modules/runtime/ownership.py', '# source_record and relocated_device'),
                ('modules/device/backend.py', '# legacy_transport_unverified'),
                ('modules/network/manager.py', '# dns_record')):
            path = self.paths.app/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        self.assertTrue(provisioning.can_restart_rollback(self.paths))
        (self.paths.app/'modules/device/backend.py').write_text('# older recovery implementation')
        self.assertFalse(provisioning.can_restart_rollback(self.paths))

    def test_active_modem_manager_requires_explicit_disable(self):
        runner = FakeRunner(manager_active=True, manager_enabled=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'ModemManager is active'):
            self.apply(Settings(), runner)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(runner.manager_active)
        self.assertTrue(runner.manager_enabled)
        self.assertFalse(any(command[0] == 'apt-get' for command in runner.calls))

    def test_explicit_modem_manager_disable_stops_and_disables_it(self):
        runner = FakeRunner(manager_active=True, manager_enabled=True)
        self.apply(Settings(), runner, 'install', '--modem-manager', 'disable')
        self.assertFalse(runner.manager_active)
        self.assertFalse(runner.manager_enabled)
        self.assertTrue(runner.active)
        self.assertIn(('systemctl', 'mask', '--now', 'ModemManager.service'), runner.calls)

    def test_restart_failure_restores_modem_manager_and_enabled_service(self):
        self.existing_installation()
        self.paths.rules.parent.mkdir(parents=True)
        self.paths.rules.write_text('# previous custom rule\n')
        runner = FakeRunner(active=True, enabled=True, manager_active=True, manager_enabled=True,
                            failures=[('systemctl', 'restart', provisioning.SERVICE)])
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(Settings(), runner, 'install', '--modem-manager', 'disable')
        self.assertTrue(runner.active)
        self.assertTrue(runner.enabled)
        self.assertTrue(runner.manager_active)
        self.assertTrue(runner.manager_enabled)
        self.assertEqual(self.paths.rules.read_text(), '# previous custom rule\n')

    def test_keep_network_manager_preserves_custom_rules(self):
        self.paths.rules.parent.mkdir(parents=True)
        self.paths.rules.write_text('# custom administrator rule\n')
        runner = FakeRunner()
        self.apply(Settings(), runner, 'install', '--network-manager', 'keep')
        self.assertEqual(self.paths.rules.read_text(), '# custom administrator rule\n')
        self.assertFalse(any(command[:2] == ('udevadm', 'control') for command in runner.calls))

    def test_dry_run_prints_plan_without_applying_or_writing(self):
        before = self.snapshot()
        with patch.object(provisioning, 'Paths', return_value=self.paths), \
                patch.object(provisioning, 'supported_system', return_value='Debian test'), \
                patch.object(provisioning, 'confirm_network_changes', return_value=[]), \
                patch.object(provisioning, 'packages_for', return_value=['python3']) as packages, \
                patch.object(provisioning, 'apply') as apply, redirect_stdout(self.output):
            result = provisioning.main(['install', '--dry-run', '--apn', 'private.example'])
        self.assertEqual(result, 0)
        apply.assert_not_called()
        packages.assert_called_once()
        self.assertIn('automatic, accepts replacement devices', self.output.getvalue())
        self.assertIn('private.example', self.output.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_web_plan_redacts_credentials_and_private_config_roundtrips(self):
        from modules.web.auth import hash_password
        encoded = hash_password('provisioning test password')
        settings = replace(Settings(), web=replace(Settings().web, enabled=True, username='operator',
                                                   password_hash=encoded))
        with redirect_stdout(self.output):
            provisioning.print_plan(settings, self.paths, ['python3'], self.arguments('install', '--dry-run'))
        plan = self.output.getvalue()
        self.assertNotIn(encoded, plan)
        self.assertNotIn('provisioning test password', plan)
        self.assertNotIn('[modem]', plan)
        self.assertNotIn('[web]', plan)
        self.assertNotIn('password_hash =', plan)
        self.assertIn(str(self.paths.config), plan)
        self.save(settings)
        self.assertEqual(load_settings(self.paths.config), settings)
        self.assertIn(encoded, self.paths.config.read_text())

    def test_password_stdin_is_hashed_without_terminal_or_password_output(self):
        from modules.web.auth import verify_password
        args = self.arguments('configure', '--web-user', 'operator', '--web-password-stdin')
        settings = provisioning.candidate_settings(args, self.paths)
        stdin = StringIO('  provisioning secret  \n')
        with patch.object(provisioning.sys, 'stdin', stdin), \
                patch.object(provisioning.getpass, 'getpass') as getpass, redirect_stdout(self.output):
            configured = provisioning.web_credentials(settings, args)
        getpass.assert_not_called()
        self.assertEqual(configured.web.username, 'operator')
        self.assertTrue(verify_password('  provisioning secret  ', configured.web.password_hash))
        self.assertFalse(verify_password('provisioning secret', configured.web.password_hash))
        self.assertNotIn('provisioning secret', self.output.getvalue())
        self.save(configured)
        preserved = provisioning.candidate_settings(self.arguments('configure', '--apn', 'private.example'), self.paths)
        self.assertEqual(preserved.web, configured.web)

    def test_cli_passes_only_a_password_hash_to_apply_and_redacts_the_plan(self):
        from modules.web.auth import verify_password
        with patch.object(provisioning, 'Paths', return_value=self.paths), \
                patch.object(provisioning, 'supported_system', return_value='Debian test'), \
                patch.object(provisioning, 'packages_for', return_value=['python3']), \
                patch.object(provisioning, 'apply') as apply, \
                patch.object(provisioning.sys, 'stdin', StringIO('CLI-private-password\n')), \
                patch('modules.deployment.diagnostics.diagnose', return_value=[]), \
                patch('modules.deployment.diagnostics.print_report', return_value=True), redirect_stdout(self.output):
            result = provisioning.main(['install', '--web', 'on', '--web-user', 'operator', '--web-password-stdin'])
        self.assertEqual(result, 0)
        configured = apply.call_args.args[0]
        self.assertEqual(configured.web.username, 'operator')
        self.assertTrue(verify_password('CLI-private-password', configured.web.password_hash))
        self.assertNotIn('CLI-private-password', self.output.getvalue())
        self.assertNotIn(configured.web.password_hash, self.output.getvalue())

    def test_terminal_account_setup_asks_once_and_preserves_existing_credentials(self):
        from modules.web.auth import verify_password
        args = self.arguments('install', '--web', 'on')
        settings = provisioning.candidate_settings(args, self.paths)
        with patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                patch.object(provisioning, 'ask', return_value='operator') as ask, \
                patch.object(provisioning.getpass, 'getpass', side_effect=['terminal-password', 'terminal-password']) as password:
            configured = provisioning.web_credentials(settings, args)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(password.call_count, 2)
        self.assertEqual(configured.web.username, 'operator')
        self.assertTrue(verify_password('terminal-password', configured.web.password_hash))
        with patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                patch.object(provisioning.getpass, 'getpass') as password:
            self.assertEqual(provisioning.web_credentials(configured, args), configured)
        password.assert_not_called()

    def test_explicit_password_reset_does_not_duplicate_automatic_prompts(self):
        args = self.arguments('install', '--web', 'on', '--web-user', 'operator', '--set-web-password')
        settings = provisioning.candidate_settings(args, self.paths)
        with patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                patch.object(provisioning, 'ask') as ask, \
                patch.object(provisioning.getpass, 'getpass', side_effect=['reset-password', 'reset-password']) as password:
            configured = provisioning.web_credentials(settings, args)
        ask.assert_not_called()
        self.assertEqual(password.call_count, 2)
        self.assertTrue(configured.web.password_hash)
        with patch.object(provisioning.sys.stdin, 'isatty', return_value=False), \
                self.assertRaisesRegex(ValueError, 'requires a terminal'):
            provisioning.web_credentials(settings, args)

    def test_password_inputs_are_mutually_exclusive_and_passwords_are_never_argv(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            self.arguments('install', '--set-web-password', '--web-password-stdin')
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            self.arguments('install', '--web-password', 'not-accepted')

    def test_invalid_passwords_and_usernames_do_not_change_any_configuration(self):
        args = self.arguments('configure', '--web-password-stdin')
        settings = provisioning.candidate_settings(args, self.paths)
        before = self.snapshot()
        for password in ('', 'short', 'x' * 257, 'é' * 129, 'line\x00hidden-password'):
            with self.subTest(password_length=len(password)), \
                    patch.object(provisioning.sys, 'stdin', StringIO(password + '\n')), \
                    self.assertRaises(ValueError):
                provisioning.web_credentials(settings, args)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(ValueError):
            provisioning.candidate_settings(self.arguments('configure', '--web-user', 'bad\nusername'), self.paths)

    def test_mismatched_confirmation_stops_before_hash_or_any_changes(self):
        args = self.arguments('configure', '--set-web-password')
        settings = provisioning.candidate_settings(args, self.paths)
        with patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                patch.object(provisioning.getpass, 'getpass', side_effect=['first-password', 'other-password']), \
                patch('modules.web.auth.hash_password') as hashing, \
                self.assertRaisesRegex(ValueError, 'do not match'):
            provisioning.web_credentials(settings, args)
        hashing.assert_not_called()

    def test_stdin_password_read_is_bounded_even_for_an_unbounded_producer(self):
        args = self.arguments('configure', '--web-password-stdin')
        settings = provisioning.candidate_settings(args, self.paths)
        stdin = Mock()
        stdin.readline.return_value = 'x' * 258
        with patch.object(provisioning.sys, 'stdin', stdin), self.assertRaises(ValueError):
            provisioning.web_credentials(settings, args)
        stdin.readline.assert_called_once_with(258)

    def test_dry_run_doctor_and_help_never_read_or_hash_a_password(self):
        for flag, password_flag in (('--dry-run', '--web-password-stdin'), ('--doctor', '--web-password-stdin'),
                                     ('--dry-run', '--set-web-password'), ('--doctor', '--set-web-password')):
            with self.subTest(flag=flag, password_flag=password_flag), patch.object(provisioning, 'Paths', return_value=self.paths), \
                    patch.object(provisioning, 'supported_system', return_value='Debian test'), \
                    patch.object(provisioning, 'confirm_network_changes', return_value=[]), \
                    patch.object(provisioning, 'packages_for', return_value=['python3']), \
                    patch.object(provisioning, 'apply') as apply, \
                    patch.object(provisioning.sys.stdin, 'isatty', return_value=True), \
                    patch.object(provisioning.sys.stdin, 'readline') as reading, \
                    patch.object(provisioning.getpass, 'getpass') as password, \
                    patch('modules.web.auth.hash_password') as hashing, \
                    patch('modules.deployment.diagnostics.diagnose', return_value=[]), \
                    patch('modules.deployment.diagnostics.print_report', return_value=True), redirect_stdout(StringIO()):
                self.assertEqual(provisioning.main(['install', flag, '--web', 'on', password_flag]), 0)
                reading.assert_not_called(); password.assert_not_called(); hashing.assert_not_called(); apply.assert_not_called()
        with patch.object(provisioning.getpass, 'getpass') as password, \
                patch('modules.web.auth.hash_password') as hashing, redirect_stdout(StringIO()), self.assertRaises(SystemExit):
            provisioning.main(['--help'])
        password.assert_not_called(); hashing.assert_not_called()

    def test_noninteractive_web_flow_shows_account_creation_command(self):
        args = self.arguments('install', '--web', 'on')
        settings = provisioning.candidate_settings(args, self.paths)
        with patch.object(provisioning.sys.stdin, 'isatty', return_value=False), \
                patch.object(provisioning.getpass, 'getpass') as password, redirect_stdout(self.output):
            configured = provisioning.web_credentials(settings, args)
            provisioning.print_plan(configured, self.paths, [], args)
        password.assert_not_called()
        self.assertEqual(configured.web.password_hash, '')
        self.assertIn('--set-web-password --web-user admin', self.output.getvalue())

    def test_reconfiguration_preserves_previous_network_manager_policy(self):
        self.existing_installation()
        self.assertEqual(provisioning.network_manager_policy(self.arguments('configure'), self.paths), 'keep')
        self.paths.rules.parent.mkdir(parents=True)
        self.paths.rules.write_text(provisioning.QMI_NETWORK_RULE)
        self.assertEqual(provisioning.network_manager_policy(self.arguments('configure'), self.paths), 'exclude-qmi')

    def test_configure_requires_an_installation_before_running_commands(self):
        runner = FakeRunner(installed=False)
        with self.assertRaisesRegex(ValueError, 'not installed'):
            self.apply(Settings(), runner, 'configure')
        self.assertEqual(runner.calls, [])

    def test_unsupported_system_is_rejected(self):
        for release, systemd in (({'ID': 'openwrt'}, True), ({'ID': 'ubuntu'}, False)):
            with self.assertRaises(ValueError):
                provisioning.supported_system(release=release, systemd=systemd)

    def test_apt_simulation_captures_machine_readable_output_in_fixed_locale(self):
        output = subprocess.CompletedProcess([], 0, 'Purg qmi-dependency [1.0]\n', '')
        with patch.object(provisioning.subprocess, 'run', return_value=output) as run:
            self.assertIs(provisioning.run(['apt-get', '--simulate', 'purge', 'qmi-dependency']), output)
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_receipt_records_direct_packages_and_dependencies_without_preexisting_packages(self):
        runner = FakeRunner(packages={'preexisting:amd64'}, install_dependencies={'new-dependency:arm64'})
        settings = provisioning.candidate_settings(self.arguments('install'), self.paths)
        self.apply(settings, runner)
        receipt = provisioning.install_state.load(self.paths)
        self.assertEqual(set(receipt['packages']), {'python3', 'new-dependency:arm64'})
        self.assertTrue(receipt['completed'])
        self.assertEqual(receipt['data_directories'], [{'path': str(self.paths.state), 'existed': False}])

    def test_partial_apt_install_failure_keeps_new_dependency_ownership(self):
        runner = FakeRunner(packages={'preexisting'}, partial_packages={'unpacked-dependency:arm64'},
                            failures=[('apt-get', 'install', '-y', '--no-remove', 'python3')])
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(provisioning.candidate_settings(self.arguments('install'), self.paths), runner)
        receipt = provisioning.install_state.load(self.paths)
        self.assertEqual(receipt['packages'], ['unpacked-dependency:arm64'])
        self.assertFalse(receipt['completed'])
        self.assertFalse(self.paths.app.exists())

    def test_reconfigure_retains_initial_baselines_without_claiming_unrelated_new_packages(self):
        self.paths.rules.parent.mkdir(parents=True)
        self.paths.rules.write_text('# original administrator rule\n')
        self.paths.rules.chmod(0o640)
        runner = FakeRunner(packages={'preexisting'}, manager_active=True, manager_enabled=True,
                            install_dependencies={'first-dependency'})
        settings = provisioning.candidate_settings(self.arguments('install'), self.paths)
        self.apply(settings, runner, 'install', '--modem-manager', 'disable')
        original = provisioning.install_state.load(self.paths)
        runner.packages.add('installed-by-administrator')
        self.apply(settings, runner, 'configure')
        receipt = provisioning.install_state.load(self.paths)
        self.assertEqual(set(receipt['packages']), {'python3', 'first-dependency'})
        self.assertEqual(receipt['modem_manager'], original['modem_manager'])
        self.assertEqual({key: receipt['modem_manager'][key] for key in ('active', 'enabled', 'masked', 'changed')},
                         {'active': True, 'enabled': True, 'masked': False, 'changed': True})
        self.assertEqual(receipt['network_rule'],
                         {'existed': True, 'content': '# original administrator rule\n',
                          'mode': 0o640, 'changed': True})
        self.assertEqual(receipt['data_directories'], [{'path': str(self.paths.state), 'existed': False}])

    def test_preexisting_legacy_generated_rule_is_owned_and_removed_on_uninstall(self):
        from modules.deployment import uninstall
        self.paths.rules.parent.mkdir(parents=True)
        self.paths.rules.write_text(provisioning.QMI_NETWORK_RULE)
        runner = FakeRunner()
        settings = provisioning.candidate_settings(self.arguments('install'), self.paths)
        self.apply(settings, runner)
        with patch.object(uninstall.os, 'geteuid', return_value=0), \
                patch.object(uninstall, 'scan_devices', return_value=[]), redirect_stdout(self.output):
            self.assertEqual(uninstall.uninstall(self.paths, self.arguments('uninstall'), runner=runner), 0)
        self.assertFalse(self.paths.rules.exists())

    def test_rollback_preserves_receipt_and_new_packages_for_later_purge(self):
        self.existing_installation()
        runner = FakeRunner(active=True, packages={'preexisting'}, install_dependencies={'new-dependency'},
                            failures=[('systemctl', 'restart', provisioning.SERVICE)])
        settings = replace(Settings(), telemetry=replace(Settings().telemetry, directory=str(self.paths.state)))
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(settings, runner)
        receipt = provisioning.install_state.load(self.paths)
        self.assertEqual(set(receipt['packages']), {'python3', 'new-dependency'})
        self.assertFalse(receipt['completed'])
        self.assertEqual((self.paths.app/'old-version').read_text(), 'old application')
        recorded = receipt['artifacts']
        failed_apps = list(self.root.glob('app root.failed-*'))
        self.assertTrue(failed_apps)
        self.assertTrue(set(map(str, failed_apps)) <= set(recorded['app_backups']))
        self.assertTrue(recorded['config_backups'])

    def test_prepared_application_is_self_contained_for_configuration(self):
        # Exercise real Python/tools code with a fixture export, regardless of
        # whether a developer's ignored frontend/out is absent or stale.
        project = Path(__file__).resolve().parents[2]
        source = self.root/'real-source'
        source.mkdir()
        shutil.copy2(project/'qmi_supervisor.py', source/'qmi_supervisor.py')
        for name in ('modules', 'tools', 'config'):
            shutil.copytree(project/name, source/name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        make_export(source/'frontend/out')
        real_paths = replace(self.paths, source=source)
        stage = provisioning.stage_application(real_paths)
        try:
            config = self.root/'staged.ini'
            config.write_text(provisioning.render_config(Settings()))
            result = subprocess.run([sys.executable, str(stage/'qmi_supervisor.py'), '--config', str(config),
                                     '--check-config'], capture_output=True, text=True, check=True,
                                    cwd=self.root, timeout=10)
            self.assertEqual(json.loads(result.stdout)['modem']['serial'], '')
            self.assertTrue((stage/'tools/deployment/setup.py').exists())
            self.assertFalse((stage/'web').exists())
            self.assertFalse((stage/'frontend/node_modules').exists())
            self.assertEqual((stage/'frontend/out/index.html').is_file(),
                             (real_paths.source/'frontend/out/index.html').is_file())
            setup_result = subprocess.run([sys.executable, str(stage/'tools/deployment/setup.py'), '--help'],
                                          capture_output=True, text=True, check=True, cwd=self.root, timeout=10)
            self.assertIn('install', setup_result.stdout)
            uninstall_result = subprocess.run(['/bin/sh', str(stage/'tools/deployment/uninstall.sh'), '--help'],
                                              capture_output=True, text=True, check=True, cwd=self.root, timeout=10)
            self.assertIn('--purge', uninstall_result.stdout)
            web_result = subprocess.run([sys.executable, '-m', 'modules.web', '--config', str(config)],
                                        capture_output=True, text=True, check=True, cwd=stage, timeout=10)
            self.assertEqual(web_result.stdout, '')  # disabled by default, so no socket is opened
        finally:
            shutil.rmtree(stage)


if __name__ == '__main__':
    unittest.main()
