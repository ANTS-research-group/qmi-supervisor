"""Multi-UE configuration, assignment and independent executable acquisitions."""
from dataclasses import replace
from pathlib import Path
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modules.device.device import Device
from modules.runtime.fleet import FleetManager, assignments, automatic_profile_id, device_key
from modules.runtime.fleet_config import Configuration
from modules.runtime.ipc import read_json
from modules.runtime.settings import Settings, load_settings
from modules.deployment.provisioning import render_config, write_atomic
from modules.web.auth import hash_password
from tests.web.static_fixture import make_export
from tests.runtime.test_process_manager import Process


def device(number, serial=None):
    return Device(f'/dev/cdc-wdm{number}', f'wwan{number}', f'/sys/usb/{number}',
                  serial if serial is not None else f'ue-{number}', f'Synthetic UE {number}')


class ProfileTests(unittest.TestCase):
    def test_profiles_share_only_web_and_survive_legacy_and_scoped_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            path.write_text('[modem]\napn=internet\n[modem:private]\nserial=ue-2\napn=tenten\n'
                            '[telemetry:private]\ninterval=2\n[web]\nhistory_size=90\n')
            configuration = Configuration.load(path)
            self.assertEqual(load_settings(path).modem.apn, 'internet')
            self.assertEqual(load_settings(path, profile='private').modem.apn, 'tenten')
            self.assertEqual(configuration.profiles['private'].telemetry.interval, 2)
            encoded = hash_password('replacement password')
            changed = replace(configuration.base, modem=replace(configuration.base.modem, apn='other'),
                              web=replace(configuration.base.web, history_size=60, password_hash=encoded))
            updated = configuration.update('default', changed)
            write_atomic(path, updated.render())
            reloaded = Configuration.load(path)
            self.assertEqual(reloaded.profiles['private'].modem.apn, 'tenten')
            self.assertEqual(reloaded.profiles['private'].web.history_size, 60)
            self.assertEqual(reloaded.profiles['private'].web.password_hash, encoded)
            self.assertEqual(reloaded.revision, updated.revision)
            self.assertNotEqual(reloaded.revision, configuration.revision)
            self.assertNotIn(encoded, updated.render(redact=True))
            self.assertIn('<redacted>', updated.render(redact=True))

    def test_every_profile_is_validated_even_when_loading_only_legacy_defaults(self):
        invalid = ['[web:other]\nport=8081', '[network:other]\nmethod=qmi',
                   '[modem:../escape]\napn=internet', '[modem:default]\napn=internet',
                   '[modem:other]\nmode=invalid', '[modem:other]\napnn=internet',
                   '[modem:other]\napn=internet\n[telemetry:other]\ninterval=.2']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            for contents in invalid:
                path.write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    load_settings(path)


class AssignmentTests(unittest.TestCase):
    def test_rebinding_an_auto_profile_keeps_every_detected_modem_and_its_new_identifier_stable(self):
        one, two, three = device(1), device(2), device(3)
        base = Settings()
        initial = assignments(Configuration(base, {}), [one, two])
        identifier = next(key for key in initial if key != 'default')
        pinned = replace(base, modem=replace(base.modem, serial=three.serial, apn='private'))
        configuration = Configuration(base, {identifier: pinned})
        result = assignments(configuration, [one, two, three],
            {key: device_key(value[1]) for key, value in initial.items()})
        assigned = [value[1] for value in result.values() if value[1]]
        self.assertEqual(set(assigned), {one, two, three})
        self.assertEqual(result[identifier][1], three)
        new_identifier = next(key for key, value in result.items() if value[1] == two)
        self.assertNotEqual(identifier, new_identifier)
        previous = {key: device_key(value[1]) for key, value in result.items()}
        # Before persistence, losing default's device must not steal the peer.
        removed = assignments(configuration, [two, three], previous)
        self.assertIsNone(removed['default'][1])
        self.assertEqual(removed[new_identifier][1], two)
        saved = configuration.update(new_identifier,
            replace(base, modem=replace(base.modem, serial=two.serial)))
        returned = assignments(saved, [three, two, one], previous)
        self.assertEqual(returned[new_identifier][1], two)
        self.assertEqual(returned[identifier][1], three)

    def test_automatic_modems_remain_distinct_and_unplug_does_not_steal_a_peer(self):
        one, two = device(1), device(2)
        configuration = Configuration(Settings(), {})
        initial = assignments(configuration, [one, two])
        self.assertEqual(initial['default'][1], one)
        identifier = next(key for key in initial if key != 'default')
        self.assertEqual(initial[identifier][1], two)
        previous = {key: device_key(value[1]) for key, value in initial.items()}
        removed = assignments(configuration, [two], previous)
        self.assertIsNone(removed['default'][1])
        self.assertEqual(removed[identifier][1], two)
        replacement = replace(two, path='/dev/cdc-wdm8', interface='wwan8')
        returned = assignments(configuration, [replacement, one], previous)
        self.assertEqual(returned['default'][1], one)
        self.assertEqual(returned[identifier][1], replacement)

    def test_automatic_profiles_take_the_manufacturer_and_unique_serial(self):
        quectel = Device('/dev/cdc-wdm2', 'wwan1', '/sys/devices/pci0000:00/0000:00:08.1/usb4/4-1',
                         '18d94aeb', 'RM520N-GL', '3', 'Quectel')
        fibocom = Device('/dev/cdc-wdm1', 'wwan0', '/sys/devices/pci0000:00/0000:00:08.1/usb5/5-1',
                         '857df105', 'Fibocom Modem_SN:857DF105', '4', 'Fibocom')
        result = assignments(Configuration(Settings(), {}), [fibocom, quectel])
        self.assertEqual(sorted(result), ['auto-quectel-18d94aeb', 'default'])
        self.assertEqual(result['default'][1], fibocom)
        self.assertEqual(result['auto-quectel-18d94aeb'][1], quectel)

    def test_duplicate_serials_name_automatic_profiles_by_usb_topology(self):
        one = Device('/dev/cdc-wdm1', 'wwan0', '/sys/usb/1-1', 'same', 'UE', '2', 'Quectel')
        two = Device('/dev/cdc-wdm2', 'wwan1', '/sys/usb/1-2', 'same', 'UE', '3', 'Quectel')
        result = assignments(Configuration(Settings(), {}), [one, two])
        self.assertEqual(sorted(result), ['auto-quectel-usb-1-2', 'default'])
        self.assertEqual(result['default'][1], one)
        self.assertEqual(result['auto-quectel-usb-1-2'][1], two)

    def test_automatic_profile_names_stay_unique_and_within_the_identifier_limit(self):
        self.assertEqual(automatic_profile_id('quectel', '18d94aeb', {'auto-quectel-18d94aeb'}),
                         'auto-quectel-18d94aeb-2')
        longest = automatic_profile_id('manufacturer-with-a-long-name', 's' * 60)
        self.assertLessEqual(len(longest), 48)
        self.assertTrue(longest.startswith('auto-manufacturer-'))
        self.assertTrue(automatic_profile_id('', '').startswith('auto-modem-'))

    def test_explicit_profiles_reserve_before_automatic_discovery_and_report_conflicts(self):
        base = Settings()
        private = replace(base, modem=replace(base.modem, serial='ue-2', apn='tenten'))
        configuration = Configuration(base, {'private': private, 'duplicate': private})
        result = assignments(configuration, [device(1), device(2)])
        assigned = [value[1] for value in result.values() if value[1]]
        self.assertEqual(len(assigned), len({value.usb_path for value in assigned}))
        self.assertEqual(len(assigned), 2)
        self.assertTrue(any(value[2] for value in result.values()))

    def test_duplicate_serials_require_disambiguation_and_usb_functions_share_ownership(self):
        one, two = device(1, 'same'), device(2, 'same')
        base = Settings()
        pinned = replace(base, modem=replace(base.modem, serial='same'))
        result = assignments(Configuration(pinned, {}), [one, two])
        self.assertIsNone(result['default'][1])
        self.assertIn('Multiple physical modems', result['default'][2])
        result = assignments(Configuration(base, {}), [one, two, replace(one, path='/dev/cdc-wdm3')])
        self.assertEqual(sum(value[1] is not None for value in result.values()), 2)


class FleetProcessTests(unittest.TestCase):
    def test_absent_disabled_modem_with_owned_checkpoint_reports_pending_cleanup_only_for_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            base = replace(Settings(), modem=replace(Settings().modem, serial='ue-1'))
            private = replace(base, modem=replace(base.modem, serial='ue-2', mode='monitor'),
                              telemetry=replace(base.telemetry, enabled=False))
            write_atomic(path, Configuration(base, {'private': private}).render())
            checkpoint = runtime/'modems/private/connection-state.json'
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text('{"version":1,"identity":"ue-2"}')
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                                   discovery=lambda: [device(1)])
            try:
                manager.reconcile()
                entry = next(value for value in manager.inventory if value['id'] == 'private')
                self.assertFalse(entry['detected'])
                self.assertTrue(entry['cleanup_pending'])
                self.assertTrue(entry['features']['connection']['pending'])
                self.assertFalse(entry['features']['connection']['enabled'])
                self.assertEqual(entry['state'], 'WAIT_DEVICE')
                self.assertIn('reconnect', entry['features']['connection']['error'])
                self.assertNotIn('private', manager.members)
                self.assertTrue(manager.members['default'].children['modem'].alive)
                self.assertFalse(next(value for value in manager.inventory if value['id'] == 'default')['cleanup_pending'])
                self.assertIsNone(entry['signal'], 'an absent modem has no current reading')
                self.assertIn('signal', next(value for value in manager.inventory if value['id'] == 'default'))
            finally:
                for member in manager.members.values():
                    manager.close_member(member)
    def test_failed_pending_configuration_still_supervises_and_restarts_last_valid_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            now = [0]
            base = Settings()
            private = replace(base, modem=replace(base.modem, serial='ue-2', apn='tenten'))
            configuration = Configuration(base, {'private': private})
            write_atomic(path, configuration.render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                                   discovery=lambda: [device(1), device(2)], clock=lambda: now[0])
            try:
                manager.reconcile()
                peer = manager.members['default'].children['modem'].process
                old_capture = manager.members['private'].children['telemetry'].process
                old_capture.kill()
                changed = configuration.update('private', replace(private,
                    modem=replace(private.modem, apn='pending-apn')))
                write_atomic(path, changed.render())
                def fail_runtime(target, content, *args, **kwargs):
                    if Path(target) == runtime/'modems/private/settings.ini':
                        raise OSError('Synthetic private settings write failure')
                    return write_atomic(target, content, *args, **kwargs)
                with patch('modules.deployment.provisioning.write_atomic', side_effect=fail_runtime):
                    now[0] = 1
                    manager.reconcile()
                    self.assertEqual(manager.members['private'].children['telemetry'].restarts, 1)
                    now[0] = 3
                    manager.reconcile()
                current = manager.members['private'].children['telemetry']
                self.assertTrue(current.alive)
                self.assertIsNot(current.process, old_capture)
                self.assertEqual(manager.members['private'].settings.modem.apn, 'tenten')
                self.assertIs(manager.members['default'].children['modem'].process, peer)
            finally:
                for member in manager.members.values():
                    manager.close_member(member)

    def test_failed_profile_materialization_reports_detected_hardware_without_starting_shared_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            now = [0]
            devices = [device(1)]
            write_atomic(path, Configuration(Settings(), {}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                                   discovery=lambda: devices, clock=lambda: now[0])
            try:
                manager.reconcile()
                first = manager.members['default'].children['modem'].process
                devices.append(device(2))
                def fail_persistence(target, content, *args, **kwargs):
                    if Path(target) == path:
                        raise OSError('Synthetic main INI write failure')
                    return write_atomic(target, content, *args, **kwargs)
                with patch('modules.deployment.provisioning.write_atomic', side_effect=fail_persistence):
                    now[0] = 1
                    manager.reconcile()
                entry = next(value for value in manager.inventory if value['id'] != 'default')
                self.assertTrue(entry['detected'])
                self.assertEqual(entry['device']['path'], '/dev/cdc-wdm2')
                self.assertIn('main INI write failure', entry['error'])
                self.assertTrue(entry['features']['connection']['pending'])
                self.assertNotIn(entry['id'], manager.members)
                self.assertIs(manager.members['default'].children['modem'].process, first)
                now[0] = 4
                manager.reconcile()
                self.assertIn(entry['id'], manager.members)
                self.assertIn(entry['id'], Configuration.load(path).profiles)
                self.assertIsNone(manager.platform_error())
            finally:
                for member in manager.members.values():
                    manager.close_member(member)

    def test_invalid_configuration_is_not_reparsed_each_tick_and_keeps_the_last_valid_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            write_atomic(path, Configuration(Settings(), {}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                                   discovery=lambda: [device(1)])
            try:
                manager.reconcile()
                previous = manager.members['default'].children['modem'].process
                write_atomic(path, '[invalid]\nkey=value\n')
                with patch.object(Configuration, 'load', wraps=Configuration.load) as load:
                    manager.reconcile()
                    manager.reconcile()
                    self.assertEqual(load.call_count, 1)
                self.assertIs(manager.members['default'].children['modem'].process, previous)
                self.assertIn('Unknown configuration section', manager.platform_error())
            finally:
                for member in manager.members.values():
                    manager.close_member(member)

    def test_unreaped_owner_remains_visible_and_is_not_replaced_or_polled_in_a_tight_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            now = [0]
            devices = [device(1), device(2)]
            base = Settings()
            configuration = Configuration(base, {'private': replace(base,
                modem=replace(base.modem, serial='ue-2'))})
            write_atomic(path, configuration.render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                discovery=lambda: devices, clock=lambda: now[0])
            try:
                manager.reconcile()
                previous = manager.members['default']
                peer = manager.members['private'].children['modem'].process
                devices[0] = device(9)
                now[0] = 1
                with patch.object(manager, 'close_member', return_value=False) as cleanup:
                    manager.reconcile()
                    self.assertIs(manager.members['default'], previous)
                    entry = next(value for value in manager.inventory if value['id'] == 'default')
                    self.assertTrue(entry['assignment_pending'])
                    self.assertEqual(entry['state'], 'STOPPING')
                    self.assertTrue(entry['features']['connection']['pending'])
                    manager.reconcile()
                    cleanup.assert_called_once()
                now[0] = 4
                manager.reconcile()
                self.assertIsNot(manager.members['default'], previous)
                self.assertEqual(manager.members['default'].settings.modem.device, '/dev/cdc-wdm9')
                self.assertIs(manager.members['private'].children['modem'].process, peer)
            finally:
                for member in manager.members.values():
                    manager.close_member(member)

    def test_shared_runtime_publication_error_keeps_children_and_web_available_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            settings = replace(Settings(), web=replace(Settings().web, enabled=True))
            write_atomic(path, Configuration(settings, {}).render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                                   discovery=lambda: [device(1)])
            def fail_manifest(target, content, *args, **kwargs):
                if Path(target).name == 'route-peers.json':
                    raise OSError('Synthetic shared runtime failure')
                return write_atomic(target, content, *args, **kwargs)
            try:
                with patch('modules.deployment.provisioning.write_atomic', side_effect=fail_manifest):
                    manager.reconcile()
                self.assertTrue(manager.members['default'].children['modem'].alive)
                self.assertTrue(manager.web.alive)
                self.assertIn('shared runtime failure', manager.platform_error())
                owner = manager.members['default'].children['modem'].process
                manager.reconcile()
                self.assertIsNone(manager.platform_error())
                self.assertTrue((runtime/'route-peers.json').is_file())
                self.assertIs(manager.members['default'].children['modem'].process, owner)
            finally:
                for member in manager.members.values():
                    manager.close_member(member)
                manager.web.stop()
                if manager.publisher:
                    manager.publisher.close()

    def test_a_profile_runtime_write_failure_does_not_stop_its_peer_or_shared_web(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            base = Settings()
            base = replace(base, web=replace(base.web, enabled=True))
            private = replace(base, modem=replace(base.modem, serial='ue-2', apn='tenten'))
            configuration = Configuration(base, {'private': private})
            write_atomic(path, configuration.render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                                   discovery=lambda: [device(1), device(2)])
            try:
                manager.reconcile()
                peer = manager.members['default'].children['modem'].process
                web = manager.web.process
                old_runtime = (runtime/'modems/private/settings.ini').read_bytes()
                changed = configuration.update('private', replace(private,
                    modem=replace(private.modem, apn='new-apn')))
                write_atomic(path, changed.render())
                def fail_one_profile(target, content, *args, **kwargs):
                    if Path(target) == runtime/'modems/private/settings.ini':
                        raise OSError('Synthetic profile runtime is not writable')
                    return write_atomic(target, content, *args, **kwargs)
                with patch('modules.deployment.provisioning.write_atomic', side_effect=fail_one_profile):
                    manager.reconcile()
                self.assertIs(manager.members['default'].children['modem'].process, peer)
                self.assertIs(manager.web.process, web)
                self.assertEqual((runtime/'modems/private/settings.ini').read_bytes(), old_runtime)
                entry = next(value for value in manager.inventory if value['id'] == 'private')
                self.assertIn('not writable', entry['error'])
                self.assertTrue(entry['features']['connection']['pending'])
                self.assertEqual(manager.members['private'].settings.modem.apn, 'tenten')
                manager.reconcile()
                self.assertEqual(manager.members['private'].settings.modem.apn, 'new-apn')
                self.assertIsNone(next(value for value in manager.inventory if value['id'] == 'private')['error'])
            finally:
                for member in manager.members.values():
                    manager.close_member(member)
                manager.web.stop()
                if manager.publisher:
                    manager.publisher.close()

    def test_hotplug_uses_fresh_defaults_after_a_concurrent_configuration_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            configuration = Configuration(Settings(), {})
            write_atomic(path, configuration.render())
            updated = configuration.update('default', replace(configuration.base,
                modem=replace(configuration.base.modem, apn='concurrent-apn')))
            def discovery():
                write_atomic(path, updated.render())
                return [device(1), device(2)]
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process, discovery=discovery)
            try:
                manager.reconcile()
                stored = Configuration.load(path)
                self.assertEqual(stored.base.modem.apn, 'concurrent-apn')
                self.assertEqual(next(iter(stored.profiles.values())).modem.apn, 'concurrent-apn')
            finally:
                for member in manager.members.values():
                    manager.close_member(member)

    def test_hotplug_materialises_independent_settings_and_keeps_them_after_base_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, runtime = root/'settings.ini', root/'run'
            runtime.mkdir()
            configuration = Configuration(Settings(), {})
            write_atomic(path, configuration.render())
            manager = FleetManager(path, threading.Event(), runtime=runtime, popen=Process,
                                   discovery=lambda: [device(1), device(2)])
            try:
                manager.reconcile()
                stored = Configuration.load(path)
                self.assertEqual(len(stored.profiles), 1)
                identifier = next(iter(stored.profiles))
                self.assertEqual(stored.profiles[identifier].modem.serial, 'ue-2')
                self.assertEqual(stored.profiles[identifier].modem.device, 'auto')
                other_owner = manager.members[identifier].children['modem'].process
                changed = stored.update('default', replace(stored.base,
                    modem=replace(stored.base.modem, apn='changed-default'),
                    telemetry=replace(stored.base.telemetry, enabled=False)))
                write_atomic(path, changed.render())
                manager.reconcile()
                self.assertEqual(manager.members['default'].settings.modem.apn, 'changed-default')
                self.assertEqual(manager.members[identifier].settings.modem.apn, 'internet')
                self.assertTrue(manager.members[identifier].children['telemetry'].alive)
                self.assertFalse(manager.members['default'].children['telemetry'].alive)
                self.assertIs(manager.members[identifier].children['modem'].process, other_owner)
                self.assertFalse(manager.web.alive)
            finally:
                for member in manager.members.values():
                    manager.close_member(member)

    def test_two_real_worker_pairs_keep_apn_capture_and_recovery_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = Settings()
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            settings = replace(defaults, telemetry=replace(defaults.telemetry, directory=str(root/'captures')),
                               web=replace(defaults.web, enabled=True, port=port))
            second = replace(settings, modem=replace(settings.modem, serial='ue-2', apn='tenten'))
            configuration = Configuration(settings, {'private': second})
            path, runtime = root/'settings.ini', root/'run'
            write_atomic(path, configuration.render())
            environment = dict(os.environ, QMI_TEST_STATIC_ROOT=str(make_export(root/'static')))
            def launch(command, **kwargs):
                command[2] = 'tests.runtime.actor_fixture'
                return subprocess.Popen(command, env=environment, **kwargs)
            devices = [device(1), device(2)]
            stop = threading.Event()
            manager = FleetManager(path, stop, runtime=runtime, popen=launch, discovery=lambda: list(devices))
            result = []
            thread = threading.Thread(target=lambda: result.append(manager.run()))
            thread.start()
            def wait(condition, timeout=12):
                deadline = time.monotonic()+timeout
                while time.monotonic() < deadline:
                    if condition():
                        return
                    time.sleep(.03)
                self.fail('Independent modem workers did not reach the expected state')
            def status(identifier):
                return read_json(runtime/'modems'/identifier/'status.json') or {}
            try:
                wait(lambda: all(len(status(key).get('history', [])) >= 4 for key in ('default', 'private')))
                first = manager.members['default'].children['modem'].process
                second_owner = manager.members['private'].children['modem'].process
                web = manager.web.process
                for identifier, apn in [('default', 'internet'), ('private', 'tenten')]:
                    snapshot = status(identifier)
                    self.assertEqual(snapshot['sample']['collection']['synthetic_apn'], apn)
                    ids = [point['sample_id'] for point in snapshot['history']]
                    self.assertEqual(ids, list(range(ids[0], ids[-1]+1)))
                manager.members['private'].children['telemetry'].process.kill()
                wait(lambda: manager.members['private'].children['telemetry'].restarts > 0)
                self.assertIs(manager.members['default'].children['modem'].process, first)
                self.assertIs(manager.members['private'].children['modem'].process, second_owner)
                self.assertIs(manager.web.process, web)
                # Stop only one objective; the other UE continues acquiring.
                updated = configuration.update('private', replace(second,
                    telemetry=replace(second.telemetry, enabled=False)))
                write_atomic(path, updated.render())
                wait(lambda: not manager.members['private'].children['telemetry'].alive)
                self.assertTrue(manager.members['default'].children['telemetry'].alive)
                self.assertIs(manager.members['private'].children['modem'].process, second_owner)
                devices.remove(device(2))
                wait(lambda: 'private' not in manager.members)
                self.assertIs(manager.members['default'].children['modem'].process, first)
                self.assertIs(manager.web.process, web)
                self.assertTrue(list((root/'captures'/'profiles'/'private').rglob('*.csv')))
            finally:
                stop.set()
                thread.join(20)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
