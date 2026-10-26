from pathlib import Path
from dataclasses import replace
import tempfile
import unittest

from modules.runtime.settings import Settings, load_settings, validate_settings
from modules.device.device import discover, scan_devices, DiscoveryError


class SettingsTests(unittest.TestCase):
    def config(self, contents):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.ini'
            path.write_text(contents)
            return load_settings(path)

    def test_minimal_config(self):
        settings = self.config('[modem]\napn = private.example\n')
        self.assertEqual(settings.modem.apn, 'private.example')
        self.assertEqual(settings.modem.device, 'auto')
        self.assertEqual(settings.network.method, 'qmi')
        self.assertEqual(settings.network.route_policy, 'fixed')
        self.assertFalse(settings.telemetry.collector_host)

    def test_rejects_typos_and_invalid_values(self):
        for text in ('[modem]\napnn=internet', '[recover]\nretry_initial=2',
                     '[network]\nroute_metric=-1', '[modem]\nmode=manual',
                     '[recovery]\nretry_initial=nan', '[recovery]\nretry_max=1',
                     '[telemetry]\ninterval=0', '[telemetry]\ncsv=maybe',
                     '[telemetry]\nudp_port=65536', '[DEFAULT]\napn=private',
                     '[modem]\ndevice=relative', '[modem]\napn='):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.config(text)

    def test_missing_explicit_config_is_error(self):
        with self.assertRaises(ValueError):
            load_settings('/missing/qmi-settings.ini')

    def test_plmn_preserves_leading_zero_and_rejects_invalid_identifiers(self):
        self.assertEqual(self.config('[modem]\nplmn=999010').modem.plmn, '999010')
        self.assertEqual(self.config('[modem]\nplmn=auto').modem.plmn, 'auto')
        for value in ('9991', '9991010', 'abcde', '９９９１０'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config('[modem]\nplmn='+value)

    def test_cli_device_overrides_config(self):
        self.assertEqual(load_settings(device='/dev/cdc-wdm3').modem.device, '/dev/cdc-wdm3')

    def test_legacy_metrics_remain_exact_fixed_values_without_policy_inference(self):
        for metric in (0, 100, 700, 925, 4294967295):
            with self.subTest(metric=metric):
                network = self.config('[network]\nroute_metric='+str(metric)).network
                self.assertEqual(network.route_metric, metric)
                self.assertEqual(network.route_policy, 'fixed')

    def test_route_policies_and_uint32_boundaries_are_validated(self):
        for policy in ('fixed', 'primary', 'backup'):
            for metric in (0, 4294967295):
                with self.subTest(policy=policy, metric=metric):
                    network = self.config(f'[network]\nroute_policy={policy}\nroute_metric={metric}').network
                    self.assertEqual((network.route_policy, network.route_metric), (policy, metric))
        for values in ('route_policy=automatic', 'route_policy=PRIMARY', 'route_policy=',
                       'route_metric=-1', 'route_metric=4294967296', 'route_metric=1.5'):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.config('[network]\n'+values)
        for metric in (True, False, 1.0, '100', None):
            with self.subTest(metric=metric), self.assertRaises(ValueError):
                validate_settings(replace(Settings(), network=replace(Settings().network, route_metric=metric)))

    def test_connectivity_defaults_apply_to_legacy_ini_and_allow_private_tcp_targets(self):
        network = self.config('[network]\nroute_metric=100\n').network
        self.assertEqual((network.connectivity_check, network.connectivity_targets,
                          network.connectivity_interval, network.connectivity_timeout,
                          network.connectivity_failures, network.connectivity_successes),
                         (True, '1.1.1.1:443,8.8.8.8:53', 3.0, 1.0, 2, 2))
        configured = self.config('[network]\nconnectivity_check=false\n'
                                 'connectivity_targets=10.0.0.1:8443,192.168.1.255:65535\n')
        self.assertFalse(configured.network.connectivity_check)
        self.assertEqual(configured.network.connectivity_targets, '10.0.0.1:8443,192.168.1.255:65535')

    def test_connectivity_targets_reject_invalid_unusable_and_duplicate_destinations(self):
        for targets in ('', 'example.com:443', '1.1.1.1', '::1:443', '127.0.0.1:443', '0.0.0.0:443',
                        '0.1.2.3:443', '224.0.0.1:443', '239.1.1.1:443', '255.255.255.255:443',
                        '256.1.1.1:443', '01.1.1.1:443', '1.1.1.1:0', '1.1.1.1:65536',
                        '1.1.1.1:443,1.1.1.1:0443', '1.1.1.1:443,',
                        '1.1.1.1:1,1.1.1.1:2,1.1.1.1:3,1.1.1.1:4,1.1.1.1:5'):
            with self.subTest(targets=targets), self.assertRaisesRegex(ValueError, 'connectivity_targets'):
                self.config('[network]\nconnectivity_targets='+targets+'\n')

    def test_connectivity_numeric_limits_types_and_timing_relationship(self):
        for key, values in (('connectivity_interval', (1, 60)), ('connectivity_timeout', (0.1, 3)),
                            ('connectivity_failures', (1, 10)), ('connectivity_successes', (1, 10))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(getattr(self.config(f'[network]\n{key}={value}').network, key), value)
        for text in ('connectivity_interval=0.9', 'connectivity_interval=61', 'connectivity_interval=inf',
                     'connectivity_timeout=0.09', 'connectivity_timeout=5.1', 'connectivity_timeout=nan',
                     'connectivity_interval=1\nconnectivity_timeout=2', 'connectivity_failures=0',
                     'connectivity_failures=11', 'connectivity_successes=1.5', 'connectivity_check=maybe'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'connectivity'):
                self.config('[network]\n'+text)
        for key, value in (('connectivity_check', 1), ('connectivity_interval', True),
                           ('connectivity_timeout', None), ('connectivity_failures', 1.0),
                           ('connectivity_successes', True)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_settings(replace(Settings(), network=replace(Settings().network, **{key: value})))
        self.assertEqual(self.config('[network]\nconnectivity_interval=60\nconnectivity_timeout=5').network.connectivity_timeout, 5)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'dev').mkdir()

    def modem(self, index, serial, driver='qmi_wwan'):
        usb = self.root / f'sys/devices/usb1/1-{index+1}'
        function = usb / f'1-{index+1}:1.4'
        (function / 'net' / f'wwan{index}').mkdir(parents=True)
        (usb / 'idVendor').write_text('2cb7')
        (usb / 'serial').write_text(serial)
        (usb / 'product').write_text('Fibocom FM160')
        (usb / 'manufacturer').write_text('Fibocom')
        (function / 'driver').symlink_to(self.root / 'sys/bus/usb/drivers' / driver)
        entry = self.root / f'sys/class/usbmisc/cdc-wdm{index}'
        entry.mkdir(parents=True)
        (entry / 'device').symlink_to(function)
        (self.root / f'dev/cdc-wdm{index}').touch()

    def find(self, device='auto', serial=''):
        return discover(device, serial, sys_root=self.root/'sys', dev_root=self.root/'dev')

    def test_absent_then_hotplug(self):
        self.assertIsNone(self.find())
        self.modem(0, '612EDE63')
        device = self.find()
        self.assertEqual((device.serial, device.interface), ('612EDE63', 'wwan0'))

    def test_scan_reads_usb_manufacturer_and_tolerates_its_absence(self):
        self.modem(0, 'first')
        device = self.find()
        assert device is not None
        self.assertEqual(device.manufacturer, 'Fibocom')
        (self.root/'sys/devices/usb1/1-1/manufacturer').unlink()
        device = self.find()
        assert device is not None
        self.assertEqual(device.manufacturer, '', 'a vendor-less USB device keeps detection working')

    def test_ambiguous_and_serial_selection(self):
        self.modem(0, 'first')
        self.modem(2, '612EDE63')
        with self.assertRaises(DiscoveryError):
            self.find()
        self.assertTrue(self.find(serial='612EDE63').path.endswith('cdc-wdm2'))
        self.assertIsNone(self.find(serial='missing'))

    def test_scan_lists_all_candidates_without_selecting(self):
        self.modem(0, 'first')
        self.modem(1, 'mbim', driver='cdc_mbim')
        self.modem(2, 'second')
        devices = scan_devices(sys_root=self.root/'sys', dev_root=self.root/'dev')
        self.assertEqual([device.serial for device in devices], ['first', 'second'])
        self.assertEqual([device.interface for device in devices], ['wwan0', 'wwan2'])

    def test_replacement_is_rescanned_and_explicit_serial_is_respected(self):
        self.modem(0, 'first')
        self.assertEqual(self.find().serial, 'first')
        (self.root/'dev/cdc-wdm0').unlink()
        self.modem(2, 'second')
        self.assertEqual(self.find().serial, 'second')
        self.assertIsNone(self.find(serial='first'))

    def test_duplicate_serial_is_still_ambiguous(self):
        self.modem(0, 'same')
        self.modem(1, 'same')
        with self.assertRaises(DiscoveryError):
            self.find(serial='same')

    def test_explicit_device_and_serial_must_both_match(self):
        self.modem(0, 'first')
        self.modem(1, 'second')
        self.assertIsNone(self.find(str(self.root/'dev/cdc-wdm1'), serial='first'))

    def test_mbim_is_not_accepted(self):
        self.modem(0, '612EDE63', driver='cdc_mbim')
        self.assertIsNone(self.find())

    def test_explicit_device(self):
        self.modem(0, 'first')
        self.modem(1, 'second')
        self.assertEqual(self.find(str(self.root/'dev/cdc-wdm1')).serial, 'second')
