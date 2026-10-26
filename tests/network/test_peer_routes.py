"""Cellular peers must not race each other or overwrite management routes."""
import unittest
import json
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from modules.network.manager import Network
from modules.network.peers import coordinated_metric
from modules.runtime.settings import NetworkSettings
from modules.runtime.state import RuntimeState


class PeerRouteTests(unittest.TestCase):
    def select(self, peers, interface, policy, metric, routes=(), allow_no_route=False):
        own = next(entry.get('route') for entry in peers if entry['interface'] == interface)
        return coordinated_metric(list(routes), peers, interface, policy, metric, own, Network._matches_route,
                                  allow_no_route=allow_no_route)

    def peers(self, policy='fixed', metric=700):
        return [dict(id=name, interface=name, enabled=True, metric=metric, policy=policy, route=None)
                for name in ('wwan0', 'wwan1')]

    def test_fixed_metrics_are_distinct_without_modifying_requested_settings(self):
        peers = self.peers()
        self.assertEqual(self.select(peers, 'wwan0', 'fixed', 700), 700)
        self.assertEqual(self.select(peers, 'wwan1', 'fixed', 700), 701)
        self.assertEqual([peer['metric'] for peer in peers], [700, 700])

    def test_primary_metrics_stabilise_against_foreign_management_route(self):
        peers = self.peers('primary', 100)
        routes = [dict(dst='default', dev='eth0', gateway='10.0.0.1', metric=100)]
        expected = [98, 99]
        for _ in range(20):
            values = [self.select(peers, name, 'primary', 100, routes) for name in ('wwan0', 'wwan1')]
            self.assertEqual(values, expected)
            for peer, metric in zip(peers, values):
                peer['route'] = ['default', 'via', '192.0.2.1', 'onlink', 'dev', peer['interface'], 'metric', str(metric)]
            routes = routes[:1]+[dict(dst='default', dev=peer['interface'], gateway='192.0.2.1', metric=value)
                                for peer, value in zip(peers, values)]
        self.assertEqual(routes[0]['metric'], 100)

    def test_failed_primary_moves_below_healthy_peer_and_management(self):
        peers = self.peers('primary', 100)
        peers[1]['fallback'] = True
        routes = [dict(dst='default', dev='eth0', metric=500)]
        self.assertEqual(self.select(peers, 'wwan0', 'primary', 100, routes), 100)
        self.assertEqual(self.select(peers, 'wwan1', 'backup', 100, routes), 501)

    def test_backup_metrics_are_stable_and_uint32_exhaustion_is_explicit(self):
        peers = self.peers('backup', 700)
        self.assertEqual(self.select(peers, 'wwan0', 'backup', 700), 700)
        self.assertEqual(self.select(peers, 'wwan1', 'backup', 700), 701)
        routes = [dict(dst='default', dev='eth0', metric=4294967295)]
        self.assertIsNone(self.select(peers, 'wwan0', 'backup', 700, routes, True))
        with self.assertRaises(RuntimeError):
            self.select(peers, 'wwan0', 'backup', 700, routes)

    def test_impossible_promotion_raises_so_existing_network_fallback_is_retained(self):
        peers = self.peers('primary', 100)
        routes = [dict(dst='default', dev='eth0', metric=0)]
        with self.assertRaises(RuntimeError):
            self.select(peers, 'wwan0', 'primary', 100, routes, True)
        self.assertEqual(self.select(peers, 'wwan0', 'backup', 100, routes, True), 100)

    def test_delayed_peer_snapshot_uses_exact_current_journal_without_metric_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            peers = self.peers('backup', 700)
            current = ['default', 'via', '192.0.2.1', 'onlink', 'dev', 'wwan1', 'metric', '701']
            peers[1].update(route=current[:-1]+['705'], checkpoint=str(root/'peer.json'),
                identity='peer', usb_path='/sys/usb/peer', enumeration='7')
            RuntimeState(root/'peer.json').save(dict(identity='peer', usb_path='/sys/usb/peer', enumeration='7',
                network=dict(interface='wwan1', route=current)))
            manifest = root/'peers.json'
            manifest.write_text(json.dumps({'peers':peers}))
            network = Network(replace(NetworkSettings(),route_policy='backup'), 1, threading.Event())
            network.interface, network.peer_routes = 'wwan0', manifest
            routes = [dict(dst='default', dev='eth0', gateway='10.0.0.1', metric=100),
                      dict(dst='default', dev='wwan1', gateway='192.0.2.1', metric=701)]
            for _ in range(20):
                self.assertEqual(network._select_metric(routes), 700)
            # Ownership of one tuple must not hide another route on that interface.
            routes.append(dict(dst='default', dev='wwan1', gateway='192.0.2.1', metric=900))
            self.assertEqual(network._select_metric(routes), 901)
            saved = RuntimeState(root/'peer.json').load()
            RuntimeState(root/'peer.json').save(dict(saved, enumeration='8'))
            with self.assertRaisesRegex(RuntimeError, 'another modem transport'):
                network._select_metric(routes)

    def test_concurrent_route_add_and_journal_publication_are_serialised(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            peers = self.peers('backup', 700)
            for entry in peers:
                entry.update(checkpoint=str(root/(entry['id']+'.json')), identity=entry['id'],
                             usb_path='/sys/usb/'+entry['id'], enumeration='7')
            manifest = root/'peers.json'
            manifest.write_text(json.dumps({'peers':peers}))
            routes = [dict(dst='default', dev='eth0', gateway='10.0.0.1', metric=100)]
            first_added, release_first, second_read = threading.Event(), threading.Event(), threading.Event()
            networks = []
            for i, entry in enumerate(peers):
                network = Network(replace(NetworkSettings(),route_policy='backup'), 2, threading.Event())
                network.interface, network.peer_routes = entry['interface'], manifest
                def changed(n=network, e=entry):
                    RuntimeState(e['checkpoint']).save(dict(identity=e['identity'], usb_path=e['usb_path'],
                        enumeration=e['enumeration'], network=dict(interface=e['interface'],route=n.route)))
                network.changed = changed
                def command(*args, index=i):
                    if args[:3] == ('ip','-j','-4'):
                        if index==1:second_read.set()
                        return json.dumps(routes)
                    self.assertEqual(args[:3], ('ip','route','add'))
                    routes.append(dict(dst='default',dev=args[args.index('dev')+1],
                        gateway=args[args.index('via')+1],metric=int(args[args.index('metric')+1])))
                    if index==0:
                        first_added.set()
                        if not release_first.wait(1):raise TimeoutError('Test did not release route writer')
                    return ''
                network.command=command
                networks.append(network)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(networks[0]._configure_route,'192.0.2.1')
                self.assertTrue(first_added.wait(1))
                second=pool.submit(networks[1]._configure_route,'192.0.2.1')
                try:
                    self.assertFalse(second_read.wait(.05),'Peer inspected an unjournaled route')
                finally:release_first.set()
                first.result(timeout=2);second.result(timeout=2)
            self.assertEqual([n.route_metric for n in networks],[700,701])
            # Stale manifest route=None remains safe on repeated maintenance.
            for _ in range(5):
                for network in networks:network._configure_route('192.0.2.1')
            self.assertEqual(len(routes),3)

    def test_busy_route_coordinator_has_a_bounded_wait(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            manifest=Path(directory)/'peers.json'
            network=Network(NetworkSettings(),.03,threading.Event())
            network.peer_routes=manifest
            with manifest.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                start=time.monotonic()
                with self.assertRaisesRegex(TimeoutError,'coordinated route'):
                    network._configure_route('192.0.2.1')
                self.assertLess(time.monotonic()-start,.3)
