"""Exercise the real GLib actor loop with an injected, non-hardware adapter."""
from dataclasses import replace
from pathlib import Path
import json
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
from modules.runtime.ipc import AcquisitionUnavailable, read_json, request
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

    def test_failure_streak_survives_a_recreated_worker(self):
        from modules.runtime.diagnosis import annotate

        class FailingBackend(FakeBackend):
            def open(self, device):
                super().open(device)
                raise annotate(RuntimeError('ModemManager is active.'), 'host.modemmanager_active')

        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            runtime.mkdir()
            path = root/'config.ini'
            settings = Settings()
            write_atomic(path, render_config(settings))
            device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
            streak = runtime/'failure-streak.json'

            def worker(identifier, flag):
                outcome = []
                def execute():
                    try:
                        outcome.append(modem_worker(path, runtime, identifier, settings, flag,
                                                    discovery=lambda *unused: device))
                    except BaseException as error:
                        outcome.append(error)
                return threading.Thread(target=execute), outcome

            with patch('modules.device.backend.QmiBackend', FailingBackend), \
                    patch('modules.network.manager.Network', FakeNetwork), \
                    patch('modules.runtime.workers.subprocess.run',
                          return_value=SimpleNamespace(returncode=3)):
                stop = threading.Event()
                thread, outcome = worker('first', stop)
                with self.assertLogs('modules.runtime.supervisor', level='ERROR') as first:
                    thread.start()
                    try:
                        self.wait(lambda: bool((read_json(streak) or {}).get('tracker', {}).get('error_logged')))
                    finally:
                        stop.set()
                        thread.join(7)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcome, [0])
                self.assertTrue(any('Modem needs attention' in record.getMessage() for record in first.records))
                saved = read_json(streak)
                self.assertEqual((saved['identity'], saved['usb_path']),
                                 ('synthetic', str(Path('/sys/synthetic').resolve())))
                self.assertEqual(streak.stat().st_mode & 0o777, 0o600)
                self.assertFalse(streak.name.startswith('connection-state'))
                failing_since, attempts = saved['tracker']['failing_since_utc'], saved['tracker']['attempts']

                stop = threading.Event()
                thread, outcome = worker('second', stop)
                with patch('modules.runtime.diagnosis.STREAK_SAVE_SECONDS', 0), \
                        self.assertNoLogs('modules.runtime.supervisor', level='ERROR'):
                    thread.start()
                    try:
                        self.wait(lambda: (read_json(streak) or {}).get('tracker', {}).get('attempts', 0) > attempts)
                    finally:
                        stop.set()
                        thread.join(7)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcome, [0])
                saved = read_json(streak)
                self.assertEqual(saved['tracker']['failing_since_utc'], failing_since)
                self.assertGreater(saved['tracker']['attempts'], attempts)

                stop = threading.Event()
                thread, outcome = worker('health', stop)
                with patch('modules.device.backend.QmiBackend', FakeBackend), \
                        patch('modules.network.manager.Network', FakeNetwork), \
                        patch('modules.runtime.workers.subprocess.run',
                              return_value=SimpleNamespace(returncode=3)):
                    thread.start()
                    try:
                        self.wait(lambda: (read_json(runtime/'modem.json') or {}).get('phase') == 'ONLINE')
                        self.wait(lambda: not streak.exists())
                    finally:
                        stop.set()
                        thread.join(8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcome, [0])

    def test_failure_streak_from_another_modem_is_ignored(self):
        from modules.runtime.diagnosis import annotate
        from modules.runtime.state import RuntimeState

        class FailingBackend(FakeBackend):
            def open(self, device):
                super().open(device)
                raise annotate(RuntimeError('ModemManager is active.'), 'host.modemmanager_active')

        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            runtime.mkdir()
            path = root/'config.ini'
            settings = Settings()
            write_atomic(path, render_config(settings))
            device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
            streak = runtime/'failure-streak.json'
            RuntimeState(streak).save({'identity': 'another-modem', 'usb_path': str(Path('/sys/synthetic').resolve()),
                                       'tracker': {'code': 'host.modemmanager_active', 'step': 'OPEN',
                                                   'operation': 'open.session', 'count': 9, 'attempts': 9,
                                                   'since_utc': '2026-11-01T00:00:00+00:00',
                                                   'failing_since_utc': '2026-11-01T00:00:00+00:00',
                                                   'last_utc': '2026-11-01T00:10:00+00:00',
                                                   'error_logged': True, 'previous': None}})
            stop, outcome = threading.Event(), []
            def execute():
                try:
                    outcome.append(modem_worker(path, runtime, 'foreign', settings, stop,
                                                discovery=lambda *unused: device))
                except BaseException as error:
                    outcome.append(error)
            with patch('modules.device.backend.QmiBackend', FailingBackend), \
                    patch('modules.network.manager.Network', FakeNetwork), \
                    patch('modules.runtime.workers.subprocess.run',
                          return_value=SimpleNamespace(returncode=3)):
                thread = threading.Thread(target=execute)
                thread.start()
                try:
                    self.wait(lambda: (read_json(streak) or {}).get('identity') == 'synthetic')
                    saved = read_json(streak)
                    self.assertEqual(saved['tracker']['attempts'], 1)
                    self.assertNotEqual(saved['tracker']['failing_since_utc'], '2026-11-01T00:00:00+00:00')
                finally:
                    stop.set()
                    thread.join(7)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [0])

    def test_the_production_snapshot_publishes_a_counted_diagnosis(self):
        from modules.runtime.diagnosis import stage

        class CoreTimeout(RuntimeError):
            domain = 'qmi_core_error_quark'
            code = 2

        class FailingBackend(FakeBackend):
            def open(self, device):
                super().open(device)
                with stage('open.recover.saved_stop'):
                    raise CoreTimeout('Transaction timed out')

        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            runtime.mkdir()
            path = root/'config.ini'
            settings = Settings()
            write_atomic(path, render_config(settings))
            device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
            stop, outcome = threading.Event(), []
            def execute():
                try:
                    outcome.append(modem_worker(path, runtime, 'diagnosis', settings, stop,
                                                discovery=lambda *unused: device))
                except BaseException as error:
                    outcome.append(error)
            with patch('modules.runtime.diagnosis.PERSISTENT_SECONDS', 0), \
                    patch('modules.device.backend.QmiBackend', FailingBackend), \
                    patch('modules.network.manager.Network', FakeNetwork), \
                    patch('modules.runtime.workers.subprocess.run',
                          return_value=SimpleNamespace(returncode=3)):
                thread = threading.Thread(target=execute)
                thread.start()
                try:
                    self.wait(lambda: ((read_json(runtime/'modem.json') or {}).get(
                        'snapshot', {}).get('diagnosis') or {}).get('count', 0) >= 3, timeout=20)
                    published = read_json(runtime/'modem.json')['snapshot']['diagnosis']
                    self.assertEqual(published['code'], 'qmi.no_answer')
                    self.assertEqual(published['operation'], 'open.recover.saved_stop')
                    self.assertEqual(published['step'], 'OPEN')
                    self.assertEqual(published['severity'], 'error')
                    self.assertGreaterEqual(published['attempts'], 3)
                    self.assertLessEqual(len(json.dumps(published)), 4096)
                finally:
                    stop.set()
                    thread.join(10)
            self.assertFalse(thread.is_alive())
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

    def test_unreadable_modem_answers_two_acquisitions_with_a_code(self):
        from modules.runtime import diagnosis
        from modules.runtime.diagnosis import annotate

        class CoreTimeout(RuntimeError):
            domain = 'qmi_core_error_quark'
            code = 2

        class FailingBackend(FakeBackend):
            def open(self, device):
                super().open(device)
                # A recovery read, not the first boot-time allocation: this
                # failure must never be treated as a startup transient.
                with diagnosis.stage('open.recover.saved_stop'):
                    raise annotate(CoreTimeout('Transaction timed out'), 'qmi.no_answer')

        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            runtime.mkdir()
            path = root/'config.ini'
            settings = Settings()
            write_atomic(path, render_config(settings))
            device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
            stop, outcome = threading.Event(), []
            def execute():
                try:
                    outcome.append(modem_worker(path, runtime, 'unreadable', settings, stop,
                                                discovery=lambda *unused: device))
                except BaseException as error:
                    outcome.append(error)
            with patch('modules.device.backend.QmiBackend', FailingBackend), \
                    patch('modules.network.manager.Network', FakeNetwork), \
                    patch('modules.runtime.workers.subprocess.run',
                          return_value=SimpleNamespace(returncode=3)):
                actor = threading.Thread(target=execute)
                actor.start()
                try:
                    # The first failed open leaves an active, coded failure streak
                    # and no readable session; both queued items must be answered
                    # at once instead of sitting until their deadline.
                    self.wait(lambda: ((read_json(runtime/'failure-streak.json') or {}).get(
                        'tracker', {}).get('code')) == 'qmi.no_answer')
                    for attempt in ('first', 'second'):
                        started = time.monotonic()
                        with self.assertRaises(AcquisitionUnavailable) as raised:
                            request(runtime/'modem.sock', dict(method='acquire', run_id=attempt,
                                    deadline=time.monotonic()+.4), 1.5)
                        elapsed = time.monotonic()-started
                        error = raised.exception
                        self.assertEqual(error.code, 'modem_not_readable')
                        self.assertLess(elapsed, .5, 'a queued item must not wait for its deadline')
                        self.assertNotIn('queue is full', str(error))
                        self.assertIn(error.details['state'], {'BACKOFF', 'WAIT_DEVICE'})
                        self.assertEqual(error.details['failed_step'], 'OPEN')
                        self.assertEqual(error.details['device_identity'], 'synthetic')
                        self.assertEqual(set(error.details['diagnosis']),
                                         {'code', 'severity', 'attempts', 'failing_since_utc'})
                        self.assertEqual(error.details['diagnosis']['code'], 'qmi.no_answer')
                finally:
                    stop.set()
                    actor.join(8)
            self.assertFalse(actor.is_alive())
            self.assertEqual(outcome, [0])

    def test_an_acquisition_during_open_waits_and_a_quick_open_still_serves_it(self):
        def backend_class(delay):
            class SlowOpenBackend(FakeBackend):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.opening = threading.Event()

                def open(self, device):
                    self.opening.set()
                    # Pump the actor's context exactly as a QMI call does, so the
                    # 250 ms tick fires while the controller is in State.OPEN.
                    loop = GLib.MainLoop.new(self.session.context, False)
                    timer = GLib.timeout_source_new(max(1, int(delay*1000)))
                    timer.set_callback(lambda *args: (loop.quit(), False)[1])
                    timer.attach(self.session.context)
                    loop.run()
                    timer.destroy()
                    super().open(device)
            return SlowOpenBackend

        for name, delay, deadline, quick in (('deadline', .6, .35, False), ('quick-open', .25, .9, True)):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                FakeBackend.instances.clear()
                root = Path(directory)
                runtime = root/'run'
                runtime.mkdir()
                path = root/'config.ini'
                defaults = Settings()
                settings = replace(defaults, modem=replace(defaults.modem, mode='monitor'))
                write_atomic(path, render_config(settings))
                device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
                stop, outcome = threading.Event(), []
                def execute():
                    try:
                        outcome.append(modem_worker(path, runtime, 'open-wait', settings, stop,
                                                    discovery=lambda *unused: device))
                    except BaseException as error:
                        outcome.append(error)
                with patch('modules.device.backend.QmiBackend', backend_class(delay)), \
                        patch('modules.network.manager.Network', FakeNetwork), \
                        patch('modules.runtime.workers.subprocess.run',
                              return_value=SimpleNamespace(returncode=3)):
                    actor = threading.Thread(target=execute)
                    actor.start()
                    try:
                        self.wait(lambda: bool(FakeBackend.instances))
                        self.assertTrue(FakeBackend.instances[0].opening.wait(3))
                        self.wait(lambda: (read_json(runtime/'modem.json') or {}).get('phase') == 'OPEN')
                        started = time.monotonic()
                        if quick:
                            # The open finishes before this deadline: the queued item
                            # must still be served, not silently dropped.
                            response = request(runtime/'modem.sock', dict(method='acquire', run_id='quick',
                                               deadline=started+deadline), 3.0)
                            self.assertEqual(response['device']['serial'], 'synthetic')
                            self.assertEqual(response['dynamic']['nas_value_5g_signal_strength_rsrp'], -80)
                        else:
                            # While OPEN the item is left to its deadline; it is not
                            # answered early as an unreadable modem.
                            with self.assertRaises(AcquisitionUnavailable) as raised:
                                request(runtime/'modem.sock', dict(method='acquire', run_id='open',
                                        deadline=started+deadline), 2.0)
                            self.assertGreaterEqual(time.monotonic()-started, .3,
                                                    'OPEN must keep the item until its deadline')
                            self.assertEqual(raised.exception.code, 'acquisition_expired')
                            self.assertNotIn(raised.exception.code, {'modem_not_readable', 'modem_absent'})
                    finally:
                        stop.set()
                        actor.join(8)
                self.assertFalse(actor.is_alive())
                self.assertEqual(outcome, [0])

    def test_a_ready_backend_serves_a_sample_with_diagnosis_and_state(self):
        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            runtime.mkdir()
            path = root/'config.ini'
            defaults = Settings()
            settings = replace(defaults, modem=replace(defaults.modem, mode='monitor'))
            write_atomic(path, render_config(settings))
            device = Device('/dev/synthetic', 'synthetic-wwan', '/sys/synthetic', 'synthetic', 'Synthetic UE')
            stop, outcome = threading.Event(), []
            def execute():
                try:
                    outcome.append(modem_worker(path, runtime, 'ready-sample', settings, stop,
                                                discovery=lambda *unused: device))
                except BaseException as error:
                    outcome.append(error)
            with patch('modules.device.backend.QmiBackend', FakeBackend), \
                    patch('modules.network.manager.Network', FakeNetwork), \
                    patch('modules.runtime.workers.subprocess.run',
                          return_value=SimpleNamespace(returncode=3)):
                actor = threading.Thread(target=execute)
                actor.start()
                try:
                    self.wait(lambda: (read_json(runtime/'modem.json') or {}).get('phase') == 'MONITOR')
                    published = (read_json(runtime/'modem.json') or {}).get('snapshot') or {}
                    # The D3 label and the contract's network subset are real fields.
                    self.assertEqual((published.get('id'), published.get('log_label')),
                                     ('default', 'synthetic'))
                    self.assertEqual(published['device']['serial'], 'synthetic')
                    self.assertIn('gateway', published['network'])
                    self.assertIn('interface', published['network'])
                    backend = FakeBackend.instances[0]
                    self.assertTrue(backend.ready)
                    response = request(runtime/'modem.sock', dict(method='acquire', run_id='sample',
                                       deadline=time.monotonic()+.8), 1.5)
                    self.assertEqual(response['dynamic']['nas_value_5g_signal_strength_rsrp'], -80)
                    self.assertEqual(response['static'], {})
                    self.assertEqual(response['report']['observation_state'], 'MONITOR')
                    self.assertEqual(response['device']['serial'], 'synthetic')
                    self.assertIsInstance(response['generation'], int)
                    self.assertEqual(response['generation'], backend.generation)
                    self.assertEqual(response['state'], 'MONITOR')
                    # A healthy session publishes a compact (here null) diagnosis;
                    # the compact shape itself is pinned by the unreadable test.
                    self.assertIn('diagnosis', response)
                    self.assertIsNone(response['diagnosis'])
                finally:
                    stop.set()
                    actor.join(8)
            self.assertFalse(actor.is_alive())
            self.assertEqual(outcome, [0])

    def test_an_expired_acquisition_is_dropped_with_a_deadline_code(self):
        class GatedDiscovery:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def __call__(self, *unused):
                self.entered.set()
                self.release.wait(5)
                return None

        FakeBackend.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root/'run'
            runtime.mkdir()
            path = root/'config.ini'
            settings = Settings()
            write_atomic(path, render_config(settings))
            stop, outcome, gate = threading.Event(), [], GatedDiscovery()
            deadline, answered = [], []
            def execute():
                try:
                    outcome.append(modem_worker(path, runtime, 'expired', settings, stop, discovery=gate))
                except BaseException as error:
                    outcome.append(error)
            def acquire():
                value = dict(method='acquire', run_id='expired', deadline=time.monotonic()+.6)
                deadline.append(value['deadline'])
                try:
                    answered.append(request(runtime/'modem.sock', value, 3.0))
                except BaseException as error:
                    answered.append(error)
            with patch('modules.device.backend.QmiBackend', FakeBackend), \
                    patch('modules.network.manager.Network', FakeNetwork), \
                    patch('modules.runtime.workers.subprocess.run',
                          return_value=SimpleNamespace(returncode=3)):
                actor = threading.Thread(target=execute)
                actor.start()
                requester = None
                try:
                    self.assertTrue(gate.entered.wait(5))
                    requester = threading.Thread(target=acquire)
                    requester.start()
                    self.wait(lambda: bool(deadline))
                    # Let the queued item expire, then release the actor so its next
                    # tick takes it before the socket's own deadline response.
                    time.sleep(max(0, deadline[0]+.01-time.monotonic()))
                    gate.release.set()
                    requester.join(4)
                    self.assertFalse(requester.is_alive())
                    self.assertEqual(len(answered), 1)
                    error = answered[0]
                    self.assertIsInstance(error, AcquisitionUnavailable)
                    self.assertEqual(error.code, 'acquisition_expired')
                    self.assertNotIn('queue is full', str(error))
                    # The dropped item left the one-slot queue free for the next one.
                    with self.assertRaises(AcquisitionUnavailable) as followup:
                        request(runtime/'modem.sock', dict(method='acquire', run_id='followup',
                                deadline=time.monotonic()+.4), 1.5)
                    self.assertEqual(followup.exception.code, 'modem_absent')
                finally:
                    gate.release.set()
                    if requester is not None:
                        requester.join(4)
                    stop.set()
                    actor.join(8)
            self.assertFalse(actor.is_alive())
            self.assertEqual(outcome, [0])
