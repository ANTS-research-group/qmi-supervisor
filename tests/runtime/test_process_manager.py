from dataclasses import replace
import itertools
import fcntl
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from modules.deployment.provisioning import render_config, write_atomic
from modules.runtime.ipc import AcquisitionServer, request, revision
from modules.runtime.processes import ProcessManager
from modules.runtime.settings import Settings


class Process:
    sequence = 1000000

    def __init__(self, command, **kwargs):
        type(self).sequence += 1
        self.pid, self.command, self.returncode = self.sequence, command, None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class ProcessManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = self.root/'settings.ini'
        self.runtime = self.root/'run'
        self.runtime.mkdir()
        self.settings = Settings()
        self.now = 0
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            for child in manager.children.values():
                child.stop()
            if manager.publisher:
                manager.publisher.close()
        self.directory.cleanup()

    def apply(self, manager, connection, telemetry):
        settings = replace(self.settings, modem=replace(self.settings.modem, mode='connect' if connection else 'monitor'),
                           telemetry=replace(self.settings.telemetry, enabled=telemetry))
        self.config.write_text(render_config(settings))
        manager.reconcile()
        return settings

    def manager(self):
        self.config.write_text(render_config(self.settings))
        manager = ProcessManager(self.config, self.settings, threading.Event(), runtime=self.runtime,
                                 popen=Process, clock=lambda: self.now)
        self.managers.append(manager)
        return manager

    def test_all_objective_transitions_preserve_required_workers(self):
        combinations = list(itertools.product((False, True), repeat=2))
        for initial, target in itertools.product(combinations, repeat=2):
            with self.subTest(initial=initial, target=target):
                manager = self.manager()
                self.apply(manager, *initial)
                before = {name: child.process for name, child in manager.children.items()}
                self.apply(manager, *target)
                self.assertEqual(manager.children['modem'].alive, any(target))
                self.assertEqual(manager.children['telemetry'].alive, target[1])
                if any(initial) and any(target):
                    self.assertIs(manager.children['modem'].process, before['modem'])
                if initial[1] and target[1]:
                    self.assertIs(manager.children['telemetry'].process, before['telemetry'])

    def test_idle_configuration_keeps_only_cleanup_worker_until_owned_checkpoint_is_released(self):
        manager = self.manager()
        checkpoint = self.root/'qmi-supervisor-state.json'
        checkpoint.write_text('{"version":1,"identity":"synthetic"}')
        idle = replace(self.settings, modem=replace(self.settings.modem, mode='monitor'),
                       telemetry=replace(self.settings.telemetry, enabled=False))
        write_atomic(self.config, render_config(idle))
        manager.reconcile()
        self.assertTrue(manager.children['modem'].alive)
        self.assertFalse(manager.children['telemetry'].alive)
        self.assertIn('--recover-owned-connection', manager.children['modem'].process.command)
        feature = manager.feature('modem', False, 'disabled-revision')
        self.assertTrue(feature['cleanup_pending'])
        self.assertTrue(feature['pending'])
        checkpoint.unlink()
        manager.reconcile()
        self.assertFalse(manager.children['modem'].alive)
        feature = manager.feature('modem', False, 'disabled-revision')
        self.assertFalse(feature['pending'])
        self.assertFalse(feature['cleanup_pending'])

    def test_telemetry_configuration_does_not_replace_modem(self):
        manager = self.manager()
        self.apply(manager, True, True)
        modem, telemetry = manager.children['modem'].process, manager.children['telemetry'].process
        self.config.write_text(render_config(replace(self.settings,
            telemetry=replace(self.settings.telemetry, interval=2))))
        manager.reconcile()
        self.assertIs(manager.children['modem'].process, modem)
        self.assertIsNot(manager.children['telemetry'].process, telemetry)
        self.assertTrue(telemetry.terminated)

    def test_crashed_consumer_has_its_own_backoff(self):
        manager = self.manager()
        self.apply(manager, True, True)
        modem = manager.children['modem'].process
        manager.children['telemetry'].process.returncode = 1
        manager.reconcile()
        self.assertIsNone(manager.children['telemetry'].process)
        self.assertEqual(manager.children['telemetry'].next_start, 2)
        self.assertIs(manager.children['modem'].process, modem)
        manager.reconcile()
        self.assertIn('exited with status 1', manager.children['telemetry'].error)
        self.now = 2
        manager.reconcile()
        self.assertTrue(manager.children['telemetry'].alive)
        self.assertEqual(manager.children['telemetry'].restarts, 1)

    def test_freshness_requires_a_real_worker_heartbeat(self):
        manager = self.manager()
        self.apply(manager, True, False)
        component = manager.feature('modem', True, 'requested')
        self.assertFalse(component['fresh'])
        self.assertFalse(component['effective'])

    def test_status_reads_never_refresh_a_heartbeat_and_expired_worker_is_reaped(self):
        manager = self.manager()
        self.apply(manager, True, False)
        child = manager.children['modem']
        old = child.process
        self.now = 10
        (self.runtime/'modem.json').write_text(json.dumps(dict(
            worker_id=child.worker_id, heartbeat_monotonic=5, phase='ONLINE')))
        manager.reconcile()
        self.assertEqual(child.last_heartbeat, 5)
        self.now = 21
        manager.reconcile()
        self.assertTrue(old.terminated)
        self.assertIsNone(child.process)

    def test_a_changed_log_level_restarts_every_worker_with_the_new_flag(self):
        manager = self.manager()
        self.apply(manager, True, True)
        children = dict(manager.children)
        old = {role: child.process for role, child in children.items()}
        manager.log_level = 'DEBUG'
        manager.reconcile()
        restarted = 0
        for role, child in children.items():
            if old[role] is None:
                continue
            restarted += 1
            self.assertIsNot(child.process, old[role], role)
            self.assertTrue(old[role].terminated, role)
            self.assertEqual(child.process.command[child.process.command.index('--log-level') + 1], 'DEBUG', role)
        self.assertGreaterEqual(restarted, 2)

    def test_unchanged_actor_files_are_not_reparsed_and_cannot_renew_liveness(self):
        manager = self.manager()
        self.apply(manager, True, False)
        child = manager.children['modem']
        path = self.runtime/'modem.json'
        path.write_text(json.dumps(dict(worker_id=child.worker_id, heartbeat_monotonic=1, phase='ONLINE')))
        self.now = 3
        from modules.runtime.ipc import read_json
        with patch('modules.runtime.processes.read_json', wraps=read_json) as read:
            child.observe(self.runtime, clock=lambda: self.now)
            self.now = 5
            child.observe(self.runtime, clock=lambda: self.now)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(child.last_heartbeat, 1)
            write_atomic(path, json.dumps(dict(worker_id=child.worker_id, heartbeat_monotonic=4, phase='ONLINE')))
            child.observe(self.runtime, clock=lambda: self.now)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(child.last_heartbeat, 4)

    def test_bounded_operation_deadline_allows_a_known_inflight_operation(self):
        manager = self.manager()
        self.apply(manager, True, False)
        child = manager.children['modem']
        (self.runtime/'modem.json').write_text(json.dumps(dict(worker_id=child.worker_id,
            heartbeat_monotonic=1, operation_deadline=25, phase='CONFIGURE_IP')))
        self.now = 20
        manager.reconcile()
        self.assertTrue(child.alive)
        self.now = 26
        manager.reconcile()
        self.assertFalse(child.alive)

    def test_invalid_update_keeps_running_configuration(self):
        manager = self.manager()
        self.apply(manager, True, True)
        old = manager.children['modem'].process
        self.config.write_text('[unknown]\ninvalid=true\n')
        manager.reconcile()
        self.assertIs(manager.children['modem'].process, old)
        self.assertIn('Unknown configuration', manager.error)

    def test_component_revisions_do_not_depend_on_other_components(self):
        modified = replace(self.settings, web=replace(self.settings.web, port=9090))
        self.assertNotEqual(revision(self.settings), revision(modified))
        self.assertEqual(revision(self.settings, ('telemetry',)), revision(modified, ('telemetry',)))

    def test_legacy_owner_prevents_starting_any_competing_tree(self):
        manager = self.manager()
        with (self.root/'qmi-supervisor.lock').open('a') as legacy:
            fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(manager.run(), 1)
            self.assertTrue(all(child.process is None for child in manager.children.values()))


class AcquisitionIPCTests(unittest.TestCase):
    def test_request_is_executed_by_owner_not_socket_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            server = AcquisitionServer(Path(directory)/'modem.sock', stop)
            result = []
            caller = threading.Thread(target=lambda: result.append(request(server.path,
                dict(method='acquire', run_id='test', deadline=time.monotonic()+1), 2)))
            caller.start()
            item = None
            deadline = time.monotonic()+1
            while item is None and time.monotonic() < deadline:
                item = server.take()
                time.sleep(.005)
            self.assertIsNotNone(item)
            self.assertEqual(result, [])
            item['response'] = {'value': 42}
            item['done'].set()
            caller.join(2)
            self.assertEqual(result, [{'value': 42}])
            stop.set()
            server.close()

    def test_arbitrary_commands_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            server = AcquisitionServer(Path(directory)/'modem.sock', stop)
            with self.assertRaisesRegex(RuntimeError, 'Unsupported IPC'):
                request(server.path, {'method': 'execute', 'command': 'anything'}, 1)
            self.assertIsNone(server.take())
            stop.set()
            server.close()
