"""Recovery journals follow physical UEs across profile changes and failures."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from modules.deployment.provisioning import write_atomic
from modules.runtime.fleet import FleetManager
from modules.runtime.fleet_config import Configuration
from modules.runtime.ownership import OwnershipStore, usb_key
from modules.runtime.settings import Settings
from modules.runtime.state import RuntimeState
from tests.runtime.test_fleet import device
from tests.runtime.test_process_manager import Process


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root/'run'
        self.runtime.mkdir()
        self.store = OwnershipStore(self.runtime)
        self.one, self.two = device(1), device(2)
        self.settings = Settings()

    def prepare(self, profile, device):
        return self.store.prepare(profile, device, self.settings, [self.one, self.two], {})

    def journal(self, path, device, **values):
        path.parent.mkdir(parents=True, exist_ok=True)
        RuntimeState(path).save(dict(identity=device.identity, enumeration='7', cid=4, handle=42,
                                     network={'interface': device.interface}, **values))

    def test_rebinding_profile_preserves_original_pending_handle_and_new_device_starts_empty(self):
        original = self.prepare('default', self.one)
        self.journal(original, self.one, cleanup_pending=True)
        replacement = self.prepare('default', self.two)
        self.assertNotEqual(original, replacement)
        self.assertEqual(RuntimeState(original).load()['handle'], 42)
        self.assertIsNone(RuntimeState(replacement).load())
        self.assertEqual(self.prepare('private', self.one), original)
        self.assertEqual(RuntimeState(original).load()['handle'], 42)
        owner = json.loads(original.with_name('owner.json').read_text())
        self.assertEqual(owner['profile'], 'private')

    def test_legacy_pair_moves_to_hardware_even_when_a_different_profile_claims_it(self):
        old = self.runtime/'modems/old-name/connection-state.json'
        self.journal(old, self.one, cleanup_pending=True)
        attach = self.store.attach_path(old)
        RuntimeState(attach).save(dict(identity=self.one.identity, enumeration='7', kind='attach_apn'))
        target = self.prepare('new-name', self.one)
        self.assertFalse(old.exists())
        self.assertFalse(attach.exists())
        self.assertEqual(RuntimeState(target).load()['handle'], 42)
        self.assertEqual(RuntimeState(self.store.attach_path(target)).load()['kind'], 'attach_apn')
        self.assertEqual(self.prepare('new-name', self.one), target)

    def test_duplicate_serial_legacy_journal_requires_topology_and_never_guesses(self):
        duplicate = replace(self.two, serial=self.one.serial)
        old = self.runtime.parent/'qmi-supervisor-state.json'
        self.journal(old, self.one)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.store.prepare('default', self.one, self.settings, [self.one, duplicate], {})
        self.assertTrue(old.exists())
        target = self.store.prepare('default', self.one, self.settings, [self.one, duplicate],
                                    {'default': usb_key(self.one)})
        self.assertEqual(RuntimeState(target).load()['handle'], 42)
        self.assertFalse(self.store.checkpoint(duplicate).exists())

    def test_conflicting_pair_keeps_both_journals_and_does_not_move_attach_file(self):
        target = self.prepare('default', self.one)
        self.journal(target, self.one)
        old = self.runtime/'modems/old/connection-state.json'
        self.journal(old, self.one, cleanup_pending=True)
        attach = self.store.attach_path(old)
        RuntimeState(attach).save(dict(identity=self.one.identity))
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.prepare('private', self.one)
        self.assertTrue(old.exists())
        self.assertTrue(attach.exists())
        self.assertEqual(RuntimeState(target).load()['handle'], 42)

    def test_legacy_journal_on_an_unassigned_present_device_still_migrates_for_cleanup(self):
        old = self.runtime.parent/'qmi-supervisor-state.json'
        self.journal(old, self.one, cleanup_pending=True)
        base = replace(self.settings, modem=replace(self.settings.modem, serial=self.two.serial))
        self.store.migrate_unassigned([self.one, self.two], {usb_key(self.two)}, {}, Configuration(base, {}))
        self.assertFalse(old.exists())
        self.assertEqual(RuntimeState(self.store.checkpoint(self.one)).load()['handle'], 42)
        self.assertTrue(self.store.pending_for('default'))

    def test_interrupted_pair_migration_is_retried_without_losing_either_record(self):
        old = self.runtime/'modems/private/connection-state.json'
        self.journal(old, self.one)
        RuntimeState(self.store.attach_path(old)).save(dict(identity=self.one.identity, kind='attach_apn'))
        original = self.store._move
        def interrupt(source, target):
            if source.name.endswith('-attach.json'):
                raise OSError('Synthetic interrupted migration')
            return original(source, target)
        with patch.object(self.store, '_move', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.prepare('new-name', self.one)
        self.assertFalse(old.exists())
        self.assertTrue(self.store.attach_path(old).exists())
        target = self.store.prepare('new-name', self.one, self.settings, [self.one, self.two],
                                    {'private': usb_key(self.two)})
        self.assertEqual(RuntimeState(target).load()['handle'], 42)
        self.assertEqual(RuntimeState(self.store.attach_path(target)).load()['kind'], 'attach_apn')

    def test_staged_snapshot_recovers_when_source_was_removed_before_target_commit(self):
        old = self.runtime/'modems/private/connection-state.json'
        self.journal(old, self.one)
        def interrupt(*_):
            raise OSError('Synthetic stop after migration journal commit')
        with patch.object(self.store, '_move', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.prepare('new-name', self.one)
        self.assertTrue(self.store.pending_for('new-name'))
        old.unlink()
        target = self.prepare('new-name', self.one)
        self.assertEqual(RuntimeState(target).load()['handle'], 42)
        self.assertFalse(target.with_name('migration.json').exists())


class FleetOwnershipTests(unittest.TestCase):
    def test_usb_reenumeration_restarts_both_workers_with_the_same_physical_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            now, devices = [0], [replace(device(1), enumeration='7')]
            write_atomic(path, Configuration(Settings(), {}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                discovery=lambda: devices, clock=lambda: now[0])
            try:
                manager.reconcile()
                previous = manager.members['default']
                workers = [previous.children[role].process for role in ('modem', 'telemetry')]
                checkpoint = Path(previous.worker_options['state_path'])
                RuntimeState(checkpoint).save(dict(identity=devices[0].identity, usb_path=usb_key(devices[0]),
                    enumeration='7', cid=4, handle=42, network={'interface': devices[0].interface}))
                devices[0] = replace(devices[0], enumeration='8')
                now[0] = 1
                manager.reconcile()
                self.assertIsNot(manager.members['default'], previous)
                self.assertTrue(all(worker.returncode is not None for worker in workers))
                self.assertEqual(Path(manager.members['default'].worker_options['state_path']), checkpoint)
                self.assertEqual(RuntimeState(checkpoint).load()['enumeration'], '7')
            finally:
                for member in [*manager.members.values(), *manager.cleanup_members.values()]:
                    manager.close_member(member)
    def test_same_usb_port_and_control_path_replacement_gets_its_own_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            now, devices = [0], [device(1), device(2)]
            base = Settings()
            private = replace(base, modem=replace(base.modem, serial='ue-2'))
            write_atomic(path, Configuration(base, {'private': private}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                discovery=lambda: devices, clock=lambda: now[0])
            try:
                manager.reconcile()
                original = devices[0]
                previous = manager.members['default']
                peer = manager.members['private'].children['modem'].process
                checkpoint = Path(previous.worker_options['state_path'])
                RuntimeState(checkpoint).save(dict(identity=original.identity, usb_path=usb_key(original),
                    enumeration='7', cid=4, handle=42, network={'interface': original.interface}))
                devices[0] = replace(original, serial='replacement-ue', product='Replacement modem')
                now[0] = 1
                manager.reconcile()
                replacement = manager.members['default']
                self.assertIsNot(replacement, previous)
                target = manager.ownership.checkpoint(devices[0])
                self.assertEqual(Path(replacement.worker_options['state_path']), target)
                self.assertNotEqual(target, checkpoint)
                self.assertEqual(RuntimeState(checkpoint).load()['handle'], 42)
                self.assertFalse(target.exists())
                self.assertEqual(manager.ownership_cleanup[0]['state'], 'WAIT_DEVICE')
                self.assertIs(manager.members['private'].children['modem'].process, peer)
                RuntimeState(target).save(dict(identity='replacement-ue', usb_path=usb_key(original),
                    enumeration='8', cid=5, handle=77, network={'interface': original.interface}))
                devices[0] = original
                now[0] = 2
                manager.reconcile()
                self.assertEqual(Path(manager.members['default'].worker_options['state_path']), checkpoint)
                self.assertEqual(RuntimeState(target).load()['handle'], 77)
                self.assertIs(manager.members['private'].children['modem'].process, peer)
            finally:
                for member in [*manager.members.values(), *manager.cleanup_members.values()]:
                    manager.close_member(member)

    def test_rebinding_an_auto_profile_materializes_the_displaced_ue_and_keeps_its_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            now, devices = [0], [device(1), device(2)]
            write_atomic(path, Configuration(Settings(), {}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                discovery=lambda: devices, clock=lambda: now[0])
            try:
                manager.reconcile()
                identifier = next(key for key in manager.members if key != 'default')
                original = manager.members[identifier]
                checkpoint = Path(original.worker_options['state_path'])
                RuntimeState(checkpoint).save(dict(identity='ue-2', usb_path=usb_key(device(2)),
                    enumeration='7', handle=42, cid=4, network={'interface': 'wwan2'}))
                peer = manager.members['default'].children['modem'].process
                configuration = Configuration.load(path)
                selected = configuration.profiles[identifier]
                write_atomic(path, configuration.update(identifier, replace(selected,
                    modem=replace(selected.modem, serial='ue-3', apn='private'))).render())
                devices.append(device(3))
                now[0] = 1
                manager.reconcile()
                self.assertEqual(len(manager.members), 3)
                self.assertEqual(manager.members[identifier].settings.modem.serial, 'ue-3')
                self.assertEqual(manager.members[identifier].settings.modem.apn, 'private')
                displaced_id = next(key for key, value in manager.members.items() if value.settings.modem.serial == 'ue-2')
                self.assertNotEqual(displaced_id, identifier)
                self.assertIn(displaced_id, Configuration.load(path).profiles)
                self.assertEqual(Path(manager.members[displaced_id].worker_options['state_path']), checkpoint)
                self.assertEqual(RuntimeState(checkpoint).load()['handle'], 42)
                self.assertNotEqual(Path(manager.members[identifier].worker_options['state_path']), checkpoint)
                self.assertIs(manager.members['default'].children['modem'].process, peer)
            finally:
                for member in [*manager.members.values(), *manager.cleanup_members.values()]:
                    manager.close_member(member)

    def test_reassigned_physical_modem_uses_the_same_journal_and_peer_process_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            now = [0]
            base = replace(Settings(), modem=replace(Settings().modem, serial='ue-1'))
            private = replace(base, modem=replace(base.modem, serial='ue-2'))
            configuration = Configuration(base, {'private': private})
            write_atomic(path, configuration.render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                discovery=lambda: [device(1), device(2)], clock=lambda: now[0])
            try:
                manager.reconcile()
                old = manager.members['default']
                peer = manager.members['private'].children['modem'].process
                checkpoint = Path(old.worker_options['state_path'])
                RuntimeState(checkpoint).save(dict(identity='ue-1', enumeration='7', handle=42, cid=4,
                    cleanup_pending=True, network={'interface': 'wwan1'}))
                # Move the same UE from default to a named profile, keeping UE2.
                disabled = replace(base, modem=replace(base.modem, serial='missing', mode='monitor'),
                                   telemetry=replace(base.telemetry, enabled=False))
                write_atomic(path, Configuration(disabled, {'private': private, 'renamed': base}).render())
                now[0] = 1
                manager.reconcile()
                self.assertNotIn('default', manager.members)
                self.assertEqual(Path(manager.members['renamed'].worker_options['state_path']), checkpoint)
                self.assertEqual(RuntimeState(checkpoint).load()['handle'], 42)
                self.assertIs(manager.members['private'].children['modem'].process, peer)
                self.assertFalse(manager.cleanup_members)
            finally:
                for member in [*manager.members.values(), *manager.cleanup_members.values()]:
                    manager.close_member(member)

    def test_orphaned_owned_session_gets_cleanup_only_actor_then_is_reaped_before_reassignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            now = [0]
            base = replace(Settings(), modem=replace(Settings().modem, serial='ue-1'))
            write_atomic(path, Configuration(base, {}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                discovery=lambda: [device(1), device(2)], clock=lambda: now[0])
            try:
                manager.reconcile()
                original = manager.members['default']
                checkpoint = Path(original.worker_options['state_path'])
                RuntimeState(checkpoint).save(dict(identity='ue-1', enumeration='7', handle=42, cid=4,
                    cleanup_pending=True, network={'interface': 'wwan1'}))
                # Explicitly bind the profile to UE2, leaving UE1 unassigned.
                changed = replace(base, modem=replace(base.modem, serial='ue-2'))
                write_atomic(path, Configuration(changed, {}).render())
                now[0] = 1
                manager.reconcile()
                replacement = manager.members['default']
                self.assertNotEqual(Path(replacement.worker_options['state_path']), checkpoint)
                cleanup = next(iter(manager.cleanup_members.values()))
                self.assertEqual(cleanup.settings.modem.mode, 'monitor')
                self.assertFalse(cleanup.settings.telemetry.enabled)
                self.assertEqual(Path(cleanup.worker_options['state_path']), checkpoint)
                self.assertIn('--recover-owned-connection', cleanup.children['modem'].process.command)
                self.assertIsNone(cleanup.children['telemetry'].process)
                write_atomic(path, Configuration(base, {}).render())
                now[0] = 2
                with patch.object(manager, 'close_member', wraps=manager.close_member) as close:
                    manager.reconcile()
                    self.assertTrue(any(call.args[0] is cleanup for call in close.call_args_list))
                self.assertFalse(cleanup.children['modem'].alive)
                self.assertFalse(manager.cleanup_members)
                self.assertEqual(Path(manager.members['default'].worker_options['state_path']), checkpoint)
            finally:
                for member in [*manager.members.values(), *manager.cleanup_members.values()]:
                    manager.close_member(member)
