"""Exercise the real GLib actor loop with an injected, non-hardware adapter."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import os
import fcntl
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.deployment.provisioning import render_config, write_atomic
from modules.device.device import Device
from modules.runtime.ipc import read_json, request
from modules.runtime.settings import Settings
from modules.runtime.processes import ProcessManager

try:
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import GLib
    from modules.runtime.workers import modem_worker
    GI_AVAILABLE = True
except (ImportError, ValueError):
    GI_AVAILABLE = False


class FakeNetwork:
    def __init__(self, *unused):
        self.address = self.route = self.route_metric = None
        self.connectivity_status = {}


class FakeBackend:
    instances = []

    def __init__(self, settings, stop, state_path):
        self.settings, self.stop = settings, stop
        self.session = SimpleNamespace(context=GLib.MainContext.new(), client=lambda *args: None, timeout=20)
        self.network = FakeNetwork()
        self.device = None
        self.static, self.collection_report = {}, {}
        self.gnss = None
        self.state = SimpleNamespace(load=lambda: None)
        self.generation = 0
        self.owned = False
        self.foreign = True
        self.open_count = self.close_count = self.disconnect_count = self.prepared = 0
        self.preparing = threading.Event()
        self.prepare_finished = threading.Event()
        self.acquired_during_prepare = False
        type(self).instances.append(self)

    def open(self, device):
        self.open_count += 1
        self.device = device
        self.generation += 1

    def prepare(self):
        self.prepared += 1
        self.preparing.set()
        loop = GLib.MainLoop.new(self.session.context, False)
        timer = GLib.timeout_source_new(1200)
        timer.set_callback(lambda *args: (loop.quit(), False)[1])
        timer.attach(self.session.context)
        loop.run()
        timer.destroy()
        self.prepare_finished.set()

    def connect(self):
        self.owned = True

    def configure_ip(self):
        self.network.address = '198.18.0.2/30'

    def registered(self):
        return True

    def healthy(self):
        return self.owned

    def present(self):
        return True

    def maintain_network(self):
        pass

    def checkpoint(self):
        pass

    def disconnect(self):
        self.disconnect_count += 1
        self.owned = False
        self.network.address = None

    def metrics(self):
        if self.preparing.is_set() and not self.prepare_finished.is_set():
            self.acquired_during_prepare = True
        self.collection_report = {'requests': {}, 'observation_state': self.observation_state}
        return {}, {'nas_value_5g_signal_strength_rsrp': -80}

    def close(self):
        self.close_count += 1
        self.disconnect()
        self.device = None


@unittest.skipUnless(GI_AVAILABLE, 'System libqmi GI bindings are required')
class ModemActorTests(unittest.TestCase):
    def wait(self, condition, timeout=5):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.01)
        self.fail('Actor did not reach the expected condition')

    def test_explicit_owned_cleanup_in_monitor_mode_never_prepares_apn_or_registration(self):
        from modules.runtime.state import RuntimeState
        for recover in (False, True):
            with self.subTest(recover_owned=recover), tempfile.TemporaryDirectory() as directory:
                FakeBackend.instances.clear()
                root = Path(directory)
                runtime = root/'run'
                runtime.mkdir()
                path, checkpoint = root/'settings.ini', root/'owned-state.json'
                settings = replace(Settings(), modem=replace(Settings().modem, mode='monitor'),
                                   telemetry=replace(Settings().telemetry, enabled=False))
                write_atomic(path, render_config(settings))
                RuntimeState(checkpoint).save(dict(identity='synthetic', enumeration='1', network={}))
                class RecoveringBackend(FakeBackend):
                    def __init__(self, candidate, stop, state_path):
                        super().__init__(candidate, stop, state_path)
                        self.state = RuntimeState(state_path)
                        self.recovered = 0
                    def recover(self):
                        self.recovered += 1
                        self.state.clear()
                stop, outcome = threading.Event(), []
                device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
                def execute():
                    try:
                        outcome.append(modem_worker(path, runtime, 'cleanup-test', settings, stop,
                            discovery=lambda *unused: device, state_path=checkpoint,
                            recover_owned_connection=recover))
                    except BaseException as error:
                        outcome.append(error)
                with patch('modules.device.backend.QmiBackend', RecoveringBackend), \
                        patch('modules.network.manager.Network', FakeNetwork):
                    actor = threading.Thread(target=execute)
                    actor.start()
                    try:
                        self.wait(lambda: (read_json(runtime/'modem.json') or {}).get('phase') == 'MONITOR')
                        backend = FakeBackend.instances[0]
                        self.assertEqual(backend.recovered, int(recover))
                        self.assertEqual(backend.prepared, 0)
                        self.assertEqual(checkpoint.exists(), not recover)
                    finally:
                        stop.set()
                        actor.join(7)
                    self.assertFalse(actor.is_alive())
                    self.assertEqual(outcome, [0])

    def test_physical_usb_lock_blocks_duplicate_workers_with_the_shared_platform_lock(self):
        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root/'qmi-supervisor.lock'
            settings = replace(Settings(), modem=replace(Settings().modem, mode='monitor'))
            device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
            stop, outcome, threads = threading.Event(), [], []
            with lock_path.open('a') as owner, patch('modules.device.backend.QmiBackend', FakeBackend), \
                    patch('modules.network.manager.Network', FakeNetwork):
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def launch(identifier):
                    runtime = root/identifier
                    runtime.mkdir()
                    path = runtime/'settings.ini'
                    write_atomic(path, render_config(settings))
                    def execute():
                        try:
                            outcome.append(modem_worker(path, runtime, identifier, settings, stop,
                                discovery=lambda *unused: device, owner_lock_fd=owner.fileno(),
                                owner_lock_path=lock_path, state_path=runtime/'connection-state.json',
                                device_lock_dir=root/'device-locks'))
                        except BaseException as error:
                            outcome.append(error)
                    thread = threading.Thread(target=execute)
                    threads.append(thread)
                    thread.start()
                    return runtime
                try:
                    first = launch('first')
                    self.wait(lambda: (read_json(first/'modem.json') or {}).get('phase') == 'MONITOR')
                    second = launch('duplicate')
                    self.wait(lambda: (read_json(second/'modem.json') or {}).get('phase') == 'BACKOFF')
                    self.assertEqual(FakeBackend.instances[0].open_count, 1)
                    self.assertEqual(FakeBackend.instances[1].open_count, 0)
                    self.assertEqual((read_json(first/'modem.json') or {})['phase'], 'MONITOR')
                finally:
                    stop.set()
                    for thread in threads:
                        thread.join(7)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(outcome, [0, 0])

    def test_telemetry_remains_readable_during_control_and_disconnect_retains_modem(self):
        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            runtime.mkdir()
            path = root/'config.ini'
            settings = Settings()
            write_atomic(path, render_config(settings))
            stop, outcome = threading.Event(), []
            device = Device('/dev/synthetic-only', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
            def execute():
                try:
                    outcome.append(modem_worker(path, runtime, 'actor-test', settings, stop,
                                                discovery=lambda *args: device))
                except BaseException as error:
                    outcome.append(error)
            with patch('modules.device.backend.QmiBackend', FakeBackend), \
                    patch('modules.network.manager.Network', FakeNetwork), \
                    patch('modules.runtime.workers.subprocess.run', return_value=SimpleNamespace(returncode=3)):
                actor = threading.Thread(target=execute)
                actor.start()
                try:
                    self.wait(lambda: bool(FakeBackend.instances))
                    backend = FakeBackend.instances[0]
                    self.assertTrue(backend.preparing.wait(3))
                    response = request(runtime/'modem.sock', dict(method='acquire', run_id='capture',
                                       deadline=time.monotonic()+.8), 1.5)
                    self.assertEqual(response['dynamic']['nas_value_5g_signal_strength_rsrp'], -80)
                    self.assertTrue(backend.acquired_during_prepare, 'The control wait must still dispatch bounded reads')
                    self.wait(lambda: backend.owned)
                    original_open = backend.open_count
                    disabled = replace(settings, modem=replace(settings.modem, mode='monitor'))
                    write_atomic(path, render_config(disabled))
                    self.wait(lambda: not backend.owned)
                    self.assertTrue(backend.foreign)
                    self.assertEqual(backend.close_count, 0, 'Connection-off keeps the QMI session available')
                    self.assertEqual(backend.open_count, original_open)
                    self.wait(lambda: (read_json(runtime/'modem.json') or {}).get('phase') == 'MONITOR')
                    response = request(runtime/'modem.sock', dict(method='acquire', run_id='capture',
                                       deadline=time.monotonic()+.8), 1.5)
                    self.assertEqual(response['device']['serial'], 'synthetic')
                    self.assertFalse(backend.owned)
                    # Recreate a broken private transport without reopening QMI.
                    (runtime/'modem.sock').unlink()
                    self.wait(lambda: (runtime/'modem.sock').is_socket())
                    response = request(runtime/'modem.sock', dict(method='acquire', run_id='capture',
                                       deadline=time.monotonic()+.8), 1.5)
                    self.assertEqual(response['device']['serial'], 'synthetic')
                    self.assertEqual(backend.open_count, original_open)
                finally:
                    stop.set()
                    actor.join(8)
                self.assertFalse(actor.is_alive())
                self.assertEqual(outcome, [0])

    def test_executable_workers_inherit_ownership_and_recover_without_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, path = root/'run', root/'config.ini'
            defaults = Settings()
            settings = replace(defaults,
                modem=replace(defaults.modem, mode='monitor', device='/dev/qmi-supervisor-test-absent'),
                telemetry=replace(defaults.telemetry, directory=str(root/'captures')))
            write_atomic(path, render_config(settings))
            stop = threading.Event()
            manager = ProcessManager(path, settings, stop, runtime=runtime)
            outcome = []
            actor = threading.Thread(target=lambda: outcome.append(manager.run()))
            actor.start()
            try:
                self.wait(lambda: manager.children['modem'].status.get('phase') == 'WAIT_DEVICE')
                self.wait(lambda: manager.children['telemetry'].status.get('phase') == 'WAIT_DEVICE')
                self.assertGreaterEqual(manager.children['telemetry'].status.get('skipped_intervals', 0), 1)
                modem = manager.children['modem'].process
                self.assertIsNotNone(modem)
                self.assertFalse((read_json(runtime/'modem.json') or {}).get('snapshot', {}).get('modem_readable'))
                manager.children['telemetry'].process.kill()
                self.wait(lambda: manager.children['telemetry'].restarts == 1)
                self.wait(lambda: manager.children['telemetry'].alive)
                self.assertIs(manager.children['modem'].process, modem)
                self.assertIsNone(modem.poll())
                idle = replace(settings, telemetry=replace(settings.telemetry, enabled=False))
                write_atomic(path, render_config(idle))
                self.wait(lambda: all(child.process is None for child in manager.children.values()))
                with (root/'qmi-supervisor.lock').open('a') as external:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(external, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse((runtime/'modem.sock').exists())
            finally:
                stop.set()
                actor.join(15)
            self.assertFalse(actor.is_alive())
            self.assertEqual(outcome, [0])
            self.assertEqual(path.read_text(), render_config(idle))
