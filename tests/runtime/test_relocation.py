"""USB-port recovery must retire host ownership without querying stale WDS CIDs."""
from dataclasses import replace
import fcntl
import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from modules.deployment.provisioning import write_atomic
from modules.runtime.fleet import FleetManager
from modules.runtime.fleet_config import Configuration
from modules.runtime.ownership import OwnershipStore, usb_key
from modules.runtime.relocation import relocated_device, retire_host_resources
from modules.runtime.settings import Settings
from modules.runtime.state import RuntimeState
from modules.runtime.workers import modem_worker
from tests.runtime.test_fleet import device
from tests.runtime.test_process_manager import Process


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root/'run'
        self.store = OwnershipStore(self.runtime)
        self.original = device(1)
        self.moved = replace(self.original, path='/dev/cdc-wdm8', interface='wwan8', usb_path='/sys/usb/8')
        self.settings, self.stop = Settings(), threading.Event()
        self.checkpoint = self.store.prepare('default', self.original, self.settings, [self.original], {})
        RuntimeState(self.checkpoint).save(dict(identity=self.original.identity, usb_path=usb_key(self.original),
            enumeration='7', cid=4, handle=42, network={'interface': self.original.interface, 'ifindex': 11}))
        self.owner = json.loads(self.checkpoint.with_name('owner.json').read_text())

    def test_unique_serial_proves_relocation_but_missing_or_duplicate_serial_does_not(self):
        self.assertEqual(relocated_device(self.owner, [self.moved]), self.moved)
        self.assertIsNone(relocated_device(self.owner, [self.original]))
        self.assertIsNone(relocated_device(self.owner, [self.original, self.moved]))
        self.assertIsNone(relocated_device(self.owner, [replace(self.moved, serial='')]))
        # Two QMI functions on the same physical USB UE remain one device.
        self.assertIsNotNone(relocated_device(self.owner, [self.moved, replace(self.moved, path='/dev/cdc-wdm9')]))

    def test_new_transport_is_blocked_until_the_original_host_state_is_retired(self):
        with self.assertRaisesRegex(RuntimeError, 'Previous USB transport cleanup'):
            self.store.prepare('default', self.moved, self.settings, [self.moved], {})
        self.assertEqual(RuntimeState(self.checkpoint).load()['handle'], 42)
        self.assertFalse(self.store.checkpoint(self.moved).exists())

    def test_upgrade_after_port_change_imports_a_known_legacy_origin_before_starting(self):
        saved = RuntimeState(self.checkpoint).load()
        shutil.rmtree(self.store.root)
        legacy = self.runtime.parent/'qmi-supervisor-state.json'
        RuntimeState(legacy).save(saved)
        with self.assertRaisesRegex(RuntimeError, 'Previous USB transport cleanup'):
            self.store.prepare('default', self.moved, self.settings, [self.moved], {})
        self.assertFalse(legacy.exists())
        self.assertTrue(self.checkpoint.exists())
        self.assertEqual(RuntimeState(self.checkpoint).load()['usb_path'], usb_key(self.original))
        self.assertFalse(self.store.checkpoint(self.moved).exists())

    def test_retirement_restores_and_closes_only_saved_network_state_and_never_uses_cid(self):
        network = Mock()
        factory = Mock(return_value=network)
        retire_host_resources(self.checkpoint, self.settings, self.stop, expected_usb=self.moved.usb_path,
                              discovery=lambda: [self.moved], network_factory=factory)
        network.restore.assert_called_once_with({'interface': self.original.interface, 'ifindex': 11})
        network.close.assert_called_once_with()
        self.assertIsNone(RuntimeState(self.checkpoint).load())

    def test_failed_host_cleanup_preserves_the_old_bearer_and_network_evidence(self):
        network = Mock()
        network.close.side_effect = OSError('Synthetic DNS cleanup failure')
        with self.assertRaisesRegex(OSError, 'DNS cleanup'):
            retire_host_resources(self.checkpoint, self.settings, self.stop, expected_usb=self.moved.usb_path,
                                  discovery=lambda: [self.moved], network_factory=lambda *_: network)
        self.assertEqual(RuntimeState(self.checkpoint).load()['handle'], 42)

    def test_changed_or_ambiguous_discovery_never_starts_cleanup(self):
        for devices in ([self.original], [self.original, self.moved], []):
            factory = Mock()
            with self.subTest(devices=devices), self.assertRaisesRegex(RuntimeError, 'could not be confirmed'):
                retire_host_resources(self.checkpoint, self.settings, self.stop, expected_usb=self.moved.usb_path,
                                      discovery=lambda: devices, network_factory=factory)
            factory.assert_not_called()
            self.assertTrue(self.checkpoint.exists())

    def attach_journal(self):
        path = self.store.attach_path(self.checkpoint)
        RuntimeState(path).save(dict(kind='attach_apn', identity=self.original.identity,
            usb_path=usb_key(self.original), enumeration='7', token='old-transaction',
            requested_apn='tenten', profile_id=1))
        return path

    def test_persistent_attach_intent_moves_only_after_host_cleanup_and_is_rechecked_on_new_transport(self):
        old_attach = self.attach_journal()
        RuntimeState(self.checkpoint).clear()
        usb = self.root/'new-usb'
        usb.mkdir()
        (usb/'devnum').write_text('19')
        moved = replace(self.moved, usb_path=str(usb))
        target = self.store.prepare('private', moved, self.settings, [moved], {})
        self.assertFalse(old_attach.exists())
        saved = RuntimeState(self.store.attach_path(target)).load()
        self.assertEqual(saved['enumeration'], '19')
        self.assertEqual(saved['usb_path'], usb_key(moved))
        self.assertEqual(saved['relocated_from_usb'], usb_key(self.original))
        self.assertEqual(saved['requested_apn'], 'tenten')
        self.assertEqual(saved['token'], 'old-transaction')
        self.assertNotIn('cid', saved)
        self.assertNotIn('handle', saved)
        self.assertFalse(target.exists())

    def test_monitor_mode_preserves_pending_attach_intent_without_changing_it(self):
        old_attach = self.attach_journal()
        before = old_attach.read_bytes()
        RuntimeState(self.checkpoint).clear()
        monitor = replace(self.settings, modem=replace(self.settings.modem, mode='monitor'))
        target = self.store.prepare('private', self.moved, monitor, [self.moved], {})
        self.assertEqual(old_attach.read_bytes(), before)
        self.assertFalse(self.store.attach_path(target).exists())

    def test_interrupted_attach_relocation_resumes_with_its_original_attribution(self):
        old_attach = self.attach_journal()
        RuntimeState(self.checkpoint).clear()
        usb = self.root/'new-usb'
        usb.mkdir()
        (usb/'devnum').write_text('19')
        moved = replace(self.moved, usb_path=str(usb))
        with patch.object(self.store, '_move', side_effect=OSError('Synthetic interrupted attach relocation')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.store.prepare('private', moved, self.settings, [moved], {})
        self.assertTrue(old_attach.exists())
        target = self.store.prepare('private', moved, self.settings, [moved], {})
        self.assertFalse(old_attach.exists())
        self.assertEqual(RuntimeState(self.store.attach_path(target)).load()['token'], 'old-transaction')
        self.assertFalse(target.with_name('migration.json').exists())

    def test_retirement_worker_does_not_import_gi_and_keeps_the_inherited_ownership_lease(self):
        lock_path = self.runtime.parent/'qmi-supervisor.lock'
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict('sys.modules', {'gi': None}), \
                    patch('modules.runtime.relocation.retire_host_resources') as retire:
                result = modem_worker(None, self.runtime/'cleanup', 'test-worker', self.settings, self.stop,
                    owner_lock_fd=lock.fileno(), owner_lock_path=lock_path, state_path=self.checkpoint,
                    device_lock_dir=self.runtime/'locks', usb_path=self.moved.usb_path, retire_relocated=True)
                self.assertEqual(result, 0)
                retire.assert_called_once()
            # Closing the child's duplicate must not release the parent's lease.
            with lock_path.open('a') as competing:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_retirement_does_not_take_a_replacement_modems_physical_usb_lease(self):
        lock_path = self.runtime.parent/'qmi-supervisor.lock'
        directory = self.runtime/'locks'
        directory.mkdir()
        key = hashlib.sha256(usb_key(self.original).encode()).hexdigest()
        network = Mock()
        original_retire = retire_host_resources
        peer = replace(self.original, serial='replacement-peer')
        def retire(path, settings, stop, *, expected_usb):
            return original_retire(path, settings, stop, expected_usb=expected_usb,
                discovery=lambda: [self.moved, peer], network_factory=lambda *_: network)
        with lock_path.open('a') as owner, (directory/(key+'.lock')).open('a') as peer_lease:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(peer_lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict('sys.modules', {'gi': None}), \
                    patch('modules.runtime.relocation.retire_host_resources', side_effect=retire):
                result = modem_worker(None, self.runtime/'cleanup', 'retirement', self.settings, self.stop,
                    owner_lock_fd=owner.fileno(), owner_lock_path=lock_path, state_path=self.checkpoint,
                    device_lock_dir=directory, usb_path=self.moved.usb_path, retire_relocated=True)
            self.assertEqual(result, 0)
            self.assertFalse(self.checkpoint.exists())
            network.close.assert_called_once_with()
            with (directory/(key+'.lock')).open('a') as competing:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_duplicate_retirement_of_one_journal_is_blocked_until_the_first_actor_finishes(self):
        lock_path = self.runtime.parent/'qmi-supervisor.lock'
        started, release = threading.Event(), threading.Event()
        failures, results = [], []
        def retire(*_, **__):
            started.set()
            if not release.wait(3):
                raise RuntimeError('Synthetic retirement was not released')
        with lock_path.open('a') as owner, patch('modules.runtime.relocation.retire_host_resources', side_effect=retire):
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def run(identifier):
                return modem_worker(None, self.runtime/'cleanup', identifier, self.settings, self.stop,
                    owner_lock_fd=owner.fileno(), owner_lock_path=lock_path, state_path=self.checkpoint,
                    device_lock_dir=self.runtime/'locks', usb_path=self.moved.usb_path, retire_relocated=True)
            def first():
                try:
                    results.append(run('first'))
                except Exception as error:
                    failures.append(error)
            actor = threading.Thread(target=first)
            actor.start()
            try:
                self.assertTrue(started.wait(2))
                with self.assertRaises(BlockingIOError):
                    run('duplicate')
            finally:
                release.set()
                actor.join(3)
            self.assertFalse(actor.is_alive())
            self.assertFalse(failures)
            self.assertEqual(results, [0])


class FleetRelocationTests(unittest.TestCase):
    def test_relocated_ue_waits_for_host_cleanup_and_reaping_while_its_peer_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            now, devices = [0], [device(1), device(2)]
            base = replace(Settings(), modem=replace(Settings().modem, serial='ue-1'))
            private = replace(base, modem=replace(base.modem, serial='ue-2'))
            write_atomic(path, Configuration(base, {'private': private}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                discovery=lambda: devices, clock=lambda: now[0])
            try:
                manager.reconcile()
                peer = manager.members['private'].children['modem'].process
                original = devices[0]
                checkpoint = Path(manager.members['default'].worker_options['state_path'])
                RuntimeState(checkpoint).save(dict(identity=original.identity, usb_path=usb_key(original),
                    enumeration='7', handle=42, cid=4, network={'interface': original.interface, 'ifindex': 11}))
                devices[0] = replace(original, usb_path='/sys/usb/8', path='/dev/cdc-wdm8', interface='wwan8')
                now[0] = 1
                with patch.object(manager, 'close_member', return_value=False):
                    manager.reconcile()
                self.assertIn('default', manager.members)
                self.assertFalse(manager.cleanup_members)
                self.assertEqual(manager.ownership_cleanup[0]['state'], 'STOPPING')
                self.assertTrue(checkpoint.exists())
                now[0] = 4
                manager.reconcile()
                self.assertNotIn('default', manager.members)
                cleanup = next(iter(manager.cleanup_members.values()))
                self.assertTrue(cleanup.worker_options['retire_relocated'])
                self.assertIn('--retire-relocated', cleanup.children['modem'].process.command)
                self.assertIsNone(cleanup.children['telemetry'].process)
                self.assertIs(manager.members['private'].children['modem'].process, peer)
                retire_host_resources(checkpoint, base, manager.stop, expected_usb=devices[0].usb_path,
                                      discovery=lambda: devices, network_factory=lambda *_: Mock())
                now[0] = 5
                with patch.object(manager, 'close_member', wraps=manager.close_member) as close:
                    manager.reconcile()
                self.assertTrue(any(call.args[0] is cleanup for call in close.call_args_list))
                self.assertFalse(cleanup.children['modem'].alive)
                self.assertFalse(manager.cleanup_members)
                self.assertEqual(manager.members['default'].settings.modem.device, '/dev/cdc-wdm8')
                self.assertIs(manager.members['private'].children['modem'].process, peer)
                self.assertFalse(Path(manager.members['default'].worker_options['state_path']).exists())
            finally:
                for member in [*manager.members.values(), *manager.cleanup_members.values()]:
                    manager.close_member(member)
