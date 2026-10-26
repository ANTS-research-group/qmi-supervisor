from dataclasses import replace
import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from modules.device.device import Device
from modules.runtime.settings import NetworkSettings, TelemetrySettings
from modules.telemetry.collector import Telemetry
from modules.network.manager import Network


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.network = Network(NetworkSettings(dns='none', connectivity_check=False), 5, threading.Event())
        self.network.interface = 'wwan0'
        self.network.ifindex = 2
        self.commands = []
        def command(*args, input=None):
            self.commands.append(args)
            if args[:4] == ('ip', '-j', 'link', 'show'):
                return json.dumps([{'ifindex': 2, 'mtu': 1500, 'flags': ['UP']}])
            if 'addr' in args and '-j' in args:
                return json.dumps([{'addr_info': [{'local': '10.42.0.2', 'prefixlen': 30}]}])
            if 'route' in args and '-j' in args:
                return json.dumps([{'dst': 'default', 'gateway': '10.42.0.1', 'dev': 'wwan0', 'metric': 700}])
            return ''
        self.network.command = command
        self.values = {'address': '10.42.0.2', 'netmask': '255.255.255.252',
                       'gateway': '10.42.0.1', 'dns': ['1.1.1.1'], 'mtu': 1428}

    def test_preserves_gateway_metric_and_other_interfaces(self):
        self.network.configure(self.values)
        self.assertIn(('ip', 'route', 'add', 'default', 'via', '10.42.0.1', 'onlink',
                       'dev', 'wwan0', 'metric', '700'), self.commands)
        self.assertTrue(self.network.healthy())
        self.network.close()
        self.assertTrue(all('eth0' not in command for command in self.commands))
        self.assertFalse(any('flush' in command or 'replace' in command for command in self.commands))

    def test_configure_is_idempotent_and_no_default_route_option(self):
        self.network.settings = replace(self.network.settings, default_route=False)
        self.network.configure(self.values)
        self.network.configure(self.values)
        self.assertEqual(sum(command[:3] == ('ip', 'addr', 'add') for command in self.commands), 1)
        self.assertFalse(any('route' in command for command in self.commands))

    def test_missing_address_cannot_pass(self):
        with self.assertRaises(RuntimeError):
            self.network.configure({})
        self.assertFalse(self.commands)

    def test_dns_auto_fallback(self):
        self.network.settings = replace(self.network.settings, dns='auto')
        real_command = self.network.command
        def command(*args, input=None):
            if args[0] == 'resolvectl':
                raise subprocess.CalledProcessError(1, args)
            return real_command(*args, input=input)
        self.network.command = command
        with patch('modules.network.manager.shutil.which', return_value='/usr/bin/tool'):
            self.network.configure(self.values)
        self.assertEqual(self.network.dns_backend, 'resolvconf')
        self.assertTrue(self.network.dns_record.startswith('wwan0.qmi-supervisor-'))
        self.assertIn(('resolvconf', '-a', self.network.dns_record), self.commands)

    def test_refuses_existing_ipv4_configuration(self):
        self.network.command = Mock(side_effect=[json.dumps([{'mtu': 1500, 'ifindex': 2, 'flags': ['UP']}]),
                                                 json.dumps([{'addr_info': [{'scope': 'global'}]}])])
        with self.assertRaises(RuntimeError):
            self.network.attach('wwan0')
        self.assertIsNone(self.network.interface)

    def test_partial_dns_update_is_kept_for_cleanup(self):
        self.network.settings = replace(self.network.settings, dns='auto')
        real_command = self.network.command
        def command(*args, input=None):
            if args[:2] == ('resolvectl', 'domain'):
                raise subprocess.CalledProcessError(1, args)
            return real_command(*args, input=input)
        self.network.command = command
        with patch('modules.network.manager.shutil.which', return_value='/usr/bin/tool'):
            with self.assertRaises(subprocess.CalledProcessError):
                self.network.configure(self.values)
        self.assertEqual(self.network.dns_backend, 'resolvectl')
        self.network.close()
        self.assertIn(('resolvectl', 'revert', 'wwan0'), self.commands)
        self.assertNotIn(('resolvconf', '-a', 'wwan0.qmi-supervisor'), self.commands)

    def test_reused_interface_is_not_modified_on_cleanup(self):
        self.network.configure(self.values)
        self.network.command = Mock(return_value=json.dumps([{'ifindex': 99}]))
        self.network.close()
        self.assertEqual(self.network.command.call_count, 1)

    def test_resolvconf_cleanup_cannot_remove_a_peers_record_after_interface_name_reuse(self):
        records = {}
        def command(*args, input=None):
            if args[:2] == ('resolvconf', '-a'):
                records[args[2]] = input
            elif args[:2] == ('resolvconf', '-d'):
                records.pop(args[2], None)
            elif args[:4] == ('ip', '-j', 'link', 'show'):
                return json.dumps([{'ifindex': 99}])
            return ''
        old = self.network
        old.settings = replace(old.settings, dns='resolvconf')
        old.command = command
        peer = Network(old.settings, 5, threading.Event())
        peer.interface, peer.ifindex, peer.command = 'wwan0', 99, command
        with patch('modules.network.manager.shutil.which', return_value='/usr/bin/resolvconf'):
            old.configure_dns(['1.1.1.1'])
            checkpoint = old.checkpoint()
            peer.configure_dns(['8.8.8.8'])
        recovered = Network(old.settings, 5, threading.Event())
        recovered.command = command
        recovered.restore(checkpoint)
        recovered.close()
        self.assertIn('nameserver 8.8.8.8\n', records.values())
        self.assertNotIn('nameserver 1.1.1.1\n', records.values())

    def test_resolvconf_intent_is_checkpointed_before_the_command_and_partial_failure_can_be_recovered(self):
        self.network.settings = replace(self.network.settings, dns='resolvconf')
        recorded = []
        self.network.changed = lambda: recorded.append(self.network.checkpoint())
        def command(*args, input=None):
            self.assertEqual(recorded[-1]['dns_record'], args[2])
            raise subprocess.CalledProcessError(1, args)
        self.network.command = command
        with patch('modules.network.manager.shutil.which', return_value='/usr/bin/resolvconf'):
            with self.assertRaises(subprocess.CalledProcessError):
                self.network.configure_dns(['1.1.1.1'])
        self.assertEqual(recorded[-1]['dns_backend'], 'resolvconf')
        recovered = Network(self.network.settings, 5, threading.Event())
        recovered.restore(recorded[-1])
        recovered.command = Mock(return_value='')
        recovered._revoke_dns()
        recovered.command.assert_called_once_with('resolvconf', '-d', recorded[-1]['dns_record'])
        self.assertIsNone(recovered.dns_record)

    def test_legacy_resolvconf_checkpoint_uses_its_original_tag_without_guessing_a_new_one(self):
        recovered = Network(self.network.settings, 5, threading.Event())
        recovered.restore(dict(interface='wwan0', ifindex=2, dns_backend='resolvconf'))
        recovered.command = Mock(return_value='')
        recovered._revoke_dns()
        recovered.command.assert_called_once_with('resolvconf', '-d', 'wwan0.qmi-supervisor')

    def test_auto_dns_keeps_its_active_backend_when_an_alternative_becomes_available(self):
        self.network.settings = replace(self.network.settings, dns='resolvconf')
        with patch('modules.network.manager.shutil.which', return_value='/usr/bin/tool'):
            self.network.configure_dns(['1.1.1.1'])
            record = self.network.dns_record
            self.commands.clear()
            self.network.settings = replace(self.network.settings, dns='auto')
            self.network.configure_dns(['8.8.8.8'])
        self.assertEqual(self.network.dns_backend, 'resolvconf')
        self.assertEqual(self.network.dns_record, record)
        self.assertEqual(self.commands, [('resolvconf', '-a', record)])

    def test_explicit_dns_backend_change_revokes_its_old_record_even_with_unchanged_addresses(self):
        self.network.settings = replace(self.network.settings, dns='resolvconf')
        with patch('modules.network.manager.shutil.which', return_value='/usr/bin/tool'):
            self.network.configure_dns(['1.1.1.1'])
            record = self.network.dns_record
            self.commands.clear()
            self.network.settings = replace(self.network.settings, dns='resolvectl')
            self.network.configure_dns(['1.1.1.1'])
        self.assertEqual(self.commands[0], ('resolvconf', '-d', record))
        self.assertEqual(self.commands[1], ('resolvectl', 'dns', 'wwan0', '1.1.1.1'))
        self.assertIsNone(self.network.dns_record)

    def test_dhcp_hook_bound_renew_and_deconfig(self):
        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / 'lease.json'
            env = dict(os.environ, QMI_SUPERVISOR_LEASE=str(lease), ip='10.42.0.2',
                       subnet='255.255.255.252', router='10.42.0.1', dns='1.1.1.1 8.8.8.8')
            for event in ('bound', 'renew'):
                subprocess.run(['modules/network/dhcp_hook.py', event], env=env, check=True)
                self.assertEqual(json.loads(lease.read_text())['gateway'], '10.42.0.1')
            subprocess.run(['modules/network/dhcp_hook.py', 'deconfig'], env=env, check=True)
            self.assertFalse(lease.exists())


class TelemetryTests(unittest.TestCase):
    def test_missing_fields_and_zero_values_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = Telemetry(TelemetrySettings(directory=directory), threading.Event())
            backend = Mock()
            backend.metrics.return_value = ({'dms_value_imei': '123'}, {'wds_value_tx_bytes_ok': 0})
            device = Device('/dev/cdc-wdm0', 'wwan0', '/sys/usb/1-1', '612EDE63', 'FM160')
            telemetry.sample(backend, device)
            telemetry.sample(backend, device)
            self.assertTrue(telemetry.close())
            folder = Path(directory) / 'modem_123'
            self.assertEqual(json.loads((folder/'stats.json').read_text())['log_id'], 2)
            with next(folder.glob('dynamic*.csv')).open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['wds_value_tx_bytes_ok'], '0')
            self.assertEqual(rows[0]['nas_value_io'], '')

    def test_csv_failure_does_not_prevent_json(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = Telemetry(TelemetrySettings(directory=directory), threading.Event())
            telemetry.csv_row = Mock(side_effect=OSError('CSV storage failed'))
            backend = Mock()
            backend.metrics.return_value = ({'dms_value_imei': '123'}, {})
            telemetry.sample(backend, Device('/dev/cdc-wdm0','wwan0','/sys/1-1','','FM160'))
            telemetry.close()
            self.assertTrue((Path(directory)/'modem_123/stats.json').exists())
