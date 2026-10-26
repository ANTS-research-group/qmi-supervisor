from dataclasses import replace
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from modules.deployment.provisioning import render_config
from modules.runtime.settings import Settings, validate_settings
from modules.web.auth import hash_password
from modules.web.server import serve
from modules.web.bind import default_interface_address, management_interface_address, management_interface_eligible
from tests.web.static_fixture import make_export


def fixture_interface(root, name, driver=None, *, up=True):
    interface = root / 'class' / 'net' / name
    interface.mkdir(parents=True)
    (interface / 'operstate').write_text('up' if up else 'down')
    (interface / 'flags').write_text('0x1003' if up else '0x1002')
    (interface / 'carrier').write_text('1' if up else '0')
    if driver:
        (interface / 'device').mkdir()
        target = root / 'drivers' / driver
        target.mkdir(parents=True, exist_ok=True)
        (interface / 'device' / 'driver').symlink_to(target, target_is_directory=True)
    return interface


def fixture_routes(path, *routes):
    path.write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask\n' + ''.join(
        f'{interface} 00000000 0100000A 0003 0 0 {metric} 00000000\n' for interface, metric in routes))


class ManagementInterfaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys = self.root / 'sys'
        self.route = self.root / 'route'
        self.addresses = {}

    def select(self):
        return management_interface_address(self.route, self.addresses.__getitem__, sys_root=self.sys)

    def test_wwan_becoming_the_preferred_route_does_not_move_management_off_ethernet(self):
        fixture_interface(self.sys, 'eth0', 'igc')
        fixture_interface(self.sys, 'wwan0', 'qmi_wwan')
        self.addresses.update(eth0='192.168.10.2', wwan0='192.168.3.2')
        fixture_routes(self.route, ('eth0', 100), ('wwan0', 700))
        self.assertEqual(self.select(), ('eth0', '192.168.10.2'))
        fixture_routes(self.route, ('wwan0', 99), ('eth0', 100))
        self.assertEqual(default_interface_address(self.route, self.addresses.__getitem__), ('wwan0', '192.168.3.2'))
        self.assertEqual(self.select(), ('eth0', '192.168.10.2'))

    def test_renamed_qmi_or_mbim_modem_is_excluded_by_driver(self):
        fixture_interface(self.sys, 'eth0', 'mvpp2')
        fixture_interface(self.sys, 'enxmodem', 'qmi_wwan')
        fixture_interface(self.sys, 'renamedmodem', 'cdc_mbim')
        self.addresses.update(eth0='192.168.10.2', enxmodem='192.168.3.2', renamedmodem='192.168.4.2')
        fixture_routes(self.route, ('enxmodem', 1), ('renamedmodem', 2), ('eth0', 100))
        self.assertEqual(self.select(), ('eth0', '192.168.10.2'))
        self.assertFalse(management_interface_eligible('enxmodem', self.sys))
        self.assertFalse(management_interface_eligible('renamedmodem', self.sys))

    def test_only_cellular_interfaces_leave_the_web_localhost_only(self):
        fixture_interface(self.sys, 'wwan0', 'qmi_wwan')
        fixture_interface(self.sys, 'wwp0s20u1', 'cdc_ether')
        self.addresses.update(wwan0='192.168.3.2', wwp0s20u1='192.168.4.2')
        fixture_routes(self.route, ('wwan0', 99), ('wwp0s20u1', 100))
        self.assertIsNone(self.select())

    def test_physical_lan_without_gateway_is_selected_even_when_only_wwan_has_default(self):
        fixture_interface(self.sys, 'eth0', 'mvpp2')
        fixture_interface(self.sys, 'wwan0', 'qmi_wwan')
        self.addresses.update(eth0='192.168.10.1', wwan0='192.168.3.2')
        fixture_routes(self.route, ('wwan0', 99))
        self.assertEqual(self.select(), ('eth0', '192.168.10.1'))
        self.route.unlink()
        self.assertEqual(self.select(), ('eth0', '192.168.10.1'))

    def test_bridge_backed_by_physical_lan_is_eligible_but_container_bridges_are_not(self):
        physical = fixture_interface(self.sys, 'eth0', 'mvpp2')
        virtual = fixture_interface(self.sys, 'veth42')
        for name, member in [('br-lan', physical), ('docker0', virtual), ('internal0', virtual)]:
            bridge = fixture_interface(self.sys, name)
            (bridge / 'bridge').mkdir()
            (bridge / 'brif').mkdir()
            (bridge / 'brif' / member.name).symlink_to(member, target_is_directory=True)
        self.addresses.update({'br-lan': '192.168.10.1', 'docker0': '172.17.0.1', 'internal0': '172.18.0.1'})
        fixture_routes(self.route, ('docker0', 1), ('internal0', 2))
        self.assertEqual(self.select(), ('br-lan', '192.168.10.1'))
        self.assertFalse(management_interface_eligible('internal0', self.sys))
        self.assertFalse(management_interface_eligible('docker0', self.sys))

    def test_physical_bridge_with_a_cellular_member_is_not_a_management_lan(self):
        physical = fixture_interface(self.sys, 'eth0', 'igc')
        cellular = fixture_interface(self.sys, 'enxmodem', 'qmi_wwan')
        bridge = fixture_interface(self.sys, 'br-mixed')
        (bridge / 'bridge').mkdir()
        (bridge / 'brif').mkdir()
        for member in (physical, cellular):
            (bridge / 'brif' / member.name).symlink_to(member, target_is_directory=True)
        self.addresses['br-mixed'] = '192.168.10.1'
        fixture_routes(self.route, ('br-mixed', 1))
        self.assertIsNone(self.select())

    def test_lan_address_change_is_observed_and_disconnected_lan_never_falls_back_to_wwan(self):
        lan = fixture_interface(self.sys, 'eth0', 'igc')
        fixture_interface(self.sys, 'wwan0', 'qmi_wwan')
        self.addresses.update(eth0='192.168.10.2', wwan0='192.168.3.2')
        fixture_routes(self.route, ('wwan0', 99), ('eth0', 100))
        self.addresses['eth0'] = '192.168.10.3'
        self.assertEqual(self.select(), ('eth0', '192.168.10.3'))
        (lan / 'carrier').write_text('0')
        self.assertIsNone(self.select())
        (lan / 'carrier').write_text('1')
        (lan / 'operstate').write_text('down')
        self.assertIsNone(self.select())
        (lan / 'operstate').write_text('up')
        self.addresses.pop('eth0')
        self.assertIsNone(self.select())

    def test_classification_is_injectable_without_network_commands(self):
        fixture_routes(self.route, ('test-lan', 100), ('test-wan', 1))
        self.addresses.update({'test-lan': '192.168.10.1', 'test-wan': '192.168.3.2'})
        self.assertEqual(management_interface_address(self.route, self.addresses.__getitem__, sys_root=self.sys,
            interface_eligible=lambda name: name == 'test-lan'), ('test-lan', '192.168.10.1'))

    def test_invalid_ipv4_candidates_and_virtual_only_networks_yield_local_only(self):
        fixture_interface(self.sys, 'eth0', 'igc')
        fixture_interface(self.sys, 'docker0')
        fixture_interface(self.sys, 'veth1')
        fixture_routes(self.route, ('docker0', 1), ('veth1', 2), ('eth0', 100))
        self.addresses.update(docker0='172.17.0.1', veth1='172.17.0.2')
        for address in ('0.0.0.0', '127.0.0.2', '224.0.0.1', 'not-an-ip'):
            self.addresses['eth0'] = address
            self.assertIsNone(self.select())


class DefaultInterfaceTests(unittest.TestCase):
    def test_lowest_metric_usable_default_wins_and_missing_address_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'route'
            path.write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask\n'
                            'wwan0 00000000 0103A8C0 0003 0 0 700 00000000\n'
                            'eth0 00000000 0134CD0A 0003 0 0 100 00000000\n'
                            'other 0034CD0A 00000000 0001 0 0 0 00FFFFFF\n'
                            'reject 00000000 00000000 0201 0 0 1 00000000\n')
            addresses={'eth0':'10.205.52.160','wwan0':'192.168.3.2'}
            self.assertEqual(default_interface_address(path,addresses.__getitem__),('eth0','10.205.52.160'))
            def address_for(interface):
                if interface=='eth0':
                    raise OSError('address changed')
                return addresses[interface]
            self.assertEqual(default_interface_address(path,address_for),('wwan0','192.168.3.2'))

    def test_missing_routes_yield_local_only_and_auto_requires_remote_auth(self):
        self.assertIsNone(default_interface_address('/missing/routes'))
        settings=replace(Settings(),web=replace(Settings().web,enabled=True,host='auto'))
        with self.assertRaises(ValueError):
            validate_settings(settings)
        validate_settings(replace(settings,web=replace(settings.web,password_hash=hash_password('listener test password'))))


class AutomaticListenerTests(unittest.TestCase):
    password = 'listener test password'

    def setUp(self):
        self.encoded = hash_password(self.password)
        self.cookies = {}

    def login(self, address, port):
        body = json.dumps({'username': 'admin', 'password': self.password})
        connection = http.client.HTTPConnection(address, port, timeout=3)
        try:
            connection.request('POST', '/api/login', body, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return (response.getheader('Set-Cookie') or '').split(';', 1)[0]
        finally:
            connection.close()

    def get(self,address,port):
        key=(address,port)
        if key not in self.cookies:
            self.cookies[key] = self.login(address,port)
        connection=http.client.HTTPConnection(address,port,timeout=.3)
        try:
            connection.request('GET','/api/config',headers={'Cookie':self.cookies[key]})
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            return json.loads(response.read())
        finally:
            connection.close()

    def wait_for(self,callback):
        end=time.monotonic()+3
        while time.monotonic()<end:
            try:
                value=callback()
                if value:
                    return value
            except OSError:
                pass
            threading.Event().wait(.01)
        self.fail('Automatic listeners did not reach the expected state')

    def test_lan_http_access_survives_wwan_becoming_the_preferred_default(self):
        # The resolver uses a realistic sysfs/route fixture. Its chosen LAN IP
        # is mapped to an extra loopback address only for the HTTP listener.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sys_root = root / 'sys'
            fixture_interface(sys_root, 'eth0', 'igc')
            fixture_interface(sys_root, 'wwan0', 'qmi_wwan')
            routes = root / 'route'
            fixture_routes(routes, ('eth0', 100), ('wwan0', 700))
            addresses = {'eth0': '192.168.10.2', 'wwan0': '192.168.3.2'}
            resolved = []
            def resolver():
                selected = management_interface_address(routes, addresses.__getitem__, sys_root=sys_root)
                resolved.append(selected)
                if selected:
                    return selected[0], '127.0.0.2' if selected[0] == 'eth0' else '127.0.0.3'
                return None
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            settings = replace(Settings(), web=replace(Settings().web, enabled=True, host='auto',
                                                       password_hash=self.encoded, port=port))
            config = root / 'config.ini'
            config.write_text(render_config(settings))
            stop = threading.Event()
            thread = threading.Thread(target=serve, args=(settings, config, root / 'status.json', stop),
                kwargs={'address_resolver': resolver, 'refresh_interval': .03,
                        'static_root': make_export(root / 'frontend/out')}, daemon=True)
            thread.start()
            try:
                initial = self.wait_for(lambda: self.get('127.0.0.2', port))
                expected = {f'http://127.0.0.1:{port}', f'http://127.0.0.2:{port}'}
                self.assertEqual(set(initial['listen_urls']), expected)
                refreshes = len(resolved)
                fixture_routes(routes, ('wwan0', 99), ('eth0', 100))
                self.wait_for(lambda: len(resolved) >= refreshes + 3)
                self.assertEqual(resolved[-1], ('eth0', '192.168.10.2'))
                self.assertEqual(set(self.get('127.0.0.2', port)['listen_urls']), expected)
                self.assertEqual(set(self.get('127.0.0.1', port)['listen_urls']), expected)
                with self.assertRaises(OSError):
                    self.get('127.0.0.3', port)
            finally:
                stop.set()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_localhost_survives_primary_address_changes_and_lists_both_urls(self):
        # Two additional loopback addresses stand in for LAN addresses without
        # modifying the host network or binding a public interface in tests.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            static_root=make_export(root/'frontend/out')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1',0))
                port=probe.getsockname()[1]
            settings=replace(Settings(),web=replace(Settings().web,enabled=True,host='auto',
                                                    password_hash=self.encoded,port=port))
            config=root/'config.ini';config.write_text(render_config(settings))
            current=[None]
            stop=threading.Event()
            thread=threading.Thread(target=serve,args=(settings,config,root/'status.json',stop),
                kwargs={'address_resolver':lambda:current[0],'refresh_interval':.03,
                        'static_root':static_root},daemon=True)
            thread.start()
            try:
                initial=self.wait_for(lambda:self.get('127.0.0.1',port))
                self.assertEqual(initial['listen_urls'],[f'http://127.0.0.1:{port}'])
                current[0]=('testnet','127.0.0.2')
                remote=self.wait_for(lambda:self.get('127.0.0.2',port))
                expected={f'http://127.0.0.1:{port}',f'http://127.0.0.2:{port}'}
                self.assertEqual(set(remote['listen_urls']),expected)
                # Repeated refreshes must not replace the LAN URL with a
                # duplicate localhost URL.
                self.wait_for(lambda:set(self.get('127.0.0.1',port)['listen_urls'])==expected)
                current[0]=('testnet','127.0.0.3')
                changed=self.wait_for(lambda:self.get('127.0.0.3',port))
                self.assertIn(f'http://127.0.0.3:{port}',changed['listen_urls'])
                self.assertNotIn(f'http://127.0.0.2:{port}',changed['listen_urls'])
                with self.assertRaises(OSError):
                    self.get('127.0.0.2',port)
                self.assertEqual(self.get('127.0.0.1',port)['config']['web']['host'],'auto')
            finally:
                stop.set();thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
