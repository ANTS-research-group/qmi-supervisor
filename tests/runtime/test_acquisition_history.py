"""The public history must contain actual acquisitions, not heartbeat samples."""
from dataclasses import replace
from contextlib import closing
from pathlib import Path
import os
import subprocess
import tempfile
import threading
import time
import unittest

from modules.runtime.platform import StatusPublisher
from modules.runtime.ipc import read_json
from modules.runtime.processes import ProcessManager
from modules.runtime.settings import Settings
from modules.deployment.provisioning import render_config, write_atomic
from modules.telemetry.history import AcquisitionHistory
from tests.web.static_fixture import make_export


def payload(identifier, *, generation=7, run='capture', missing=False):
    return dict(log_id=identifier, device='/dev/synthetic', device_identity='synthetic',
                device_generation=generation, end_datetime='sample-time',
                dynamic={'nas_value_5g_signal_strength_rsrp': None if missing else -85,
                         'nas_value_5g_signal_strength_extended': -11,
                         'nas_value_5g_signal_strength_snr': 0},
                timing=dict(run_id=run, acquired_start_monotonic=100+identifier,
                            effective_interval_ms=1000, skipped_intervals=0))


class AcquisitionHistoryTests(unittest.TestCase):
    def test_executable_actor_preserves_every_point_between_heartbeats_and_web_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, config = root/'run', root/'settings.ini'
            defaults = Settings()
            settings = replace(defaults,
                telemetry=replace(defaults.telemetry, interval=.1, collection_budget=.08,
                                  directory=str(root/'captures')),
                web=replace(defaults.web, enabled=True))
            # Use a private listen port and export without relying on Next.
            import socket
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                settings = replace(settings, web=replace(settings.web, port=probe.getsockname()[1]))
            environment = dict(os.environ, QMI_TEST_STATIC_ROOT=str(make_export(root/'static')))
            write_atomic(config, render_config(settings))
            def launch(command, **kwargs):
                command[2] = 'tests.runtime.actor_fixture'
                return subprocess.Popen(command, env=environment, **kwargs)
            stop = threading.Event()
            manager = ProcessManager(config, settings, stop, runtime=runtime, popen=launch)
            result = []
            platform = threading.Thread(target=lambda: result.append(manager.run()))
            platform.start()
            def wait(condition, timeout=7):
                deadline = time.monotonic()+timeout
                while time.monotonic() < deadline:
                    if condition():
                        return
                    time.sleep(.03)
                self.fail('Executable history did not reach the expected state')
            try:
                wait(lambda: len((read_json(runtime/'status.json') or {}).get('history', [])) >= 25)
                history = read_json(runtime/'status.json')['history']
                identifiers = [point['sample_id'] for point in history]
                self.assertEqual(identifiers, list(range(identifiers[0], identifiers[-1]+1)))
                modem, telemetry = manager.children['modem'].process, manager.children['telemetry'].process
                resized = replace(settings, web=replace(settings.web, history_size=5))
                write_atomic(config, render_config(resized))
                wait(lambda: len((read_json(runtime/'telemetry.json') or {}).get('acquisition_history', [])) == 5)
                self.assertIs(manager.children['modem'].process, modem)
                self.assertIs(manager.children['telemetry'].process, telemetry)
                disabled = replace(resized, web=replace(resized.web, enabled=False))
                write_atomic(config, render_config(disabled))
                wait(lambda: (read_json(runtime/'telemetry.json') or {}).get('acquisition_history') == [])
                self.assertIs(manager.children['telemetry'].process, telemetry)
            finally:
                stop.set()
                platform.join(20)
            self.assertFalse(platform.is_alive())
            self.assertEqual(result, [0])

    def test_actor_window_is_bounded_resizable_and_optional_without_fabricated_values(self):
        web = replace(Settings().web, enabled=True)
        history = AcquisitionHistory(web)
        for identifier in range(1, 151):
            history.append(payload(identifier, missing=identifier == 149))
        self.assertEqual([p['sample_id'] for p in history.snapshot()], list(range(31, 151)))
        self.assertIsNone(history.snapshot()[-2]['rsrp'])
        self.assertEqual(history.snapshot()[-1]['snr'], 0)
        history.configure(replace(web, history_size=3))
        self.assertEqual([p['sample_id'] for p in history.snapshot()], [148, 149, 150])
        history.configure(replace(web, enabled=False))
        history.append(payload(151))
        self.assertEqual(history.snapshot(), [])
        history.configure(web)
        history.append(payload(152))
        self.assertEqual([p['sample_id'] for p in history.snapshot()], [152])
        history.append(payload(153, generation=8))
        self.assertEqual([p['sample_id'] for p in history.snapshot()], [153])

    def test_heartbeat_and_parent_delays_do_not_drop_acquired_points_or_duplicate_them(self):
        history = AcquisitionHistory(replace(Settings().web, enabled=True))
        with tempfile.TemporaryDirectory() as directory, \
                closing(StatusPublisher(Path(directory)/'status.json', history_size=120)) as publisher:
            snapshot = dict(state='ONLINE', run_id='capture', device_generation=7,
                            modem_readable=True, heartbeat_monotonic=100,
                            processes={'modem': {'alive': True}})
            for identifier in range(1, 191):
                sample = payload(identifier, missing=identifier == 188)
                history.append(sample)
                # Several valid samples are acquired between observations.
                if identifier in {1, 3, 10, 15, 47, 190}:
                    actor = dict(sample=sample, run_id='capture',
                                 acquisition_history=history.snapshot(),
                                 heartbeat_monotonic=100+identifier,
                                 stats={'last_write_sample_id': identifier-1})
                    publisher.update_platform(snapshot, actor)
                    publisher.update_platform(snapshot, actor)
            self.assertEqual([p['sample_id'] for p in publisher.history], list(range(71, 191)))
            self.assertIsNone(publisher.history[-3]['rsrp'])
            self.assertEqual(publisher.history[-1]['snr'], 0)
            self.assertEqual(publisher.pipeline_history[-1]['last_write_sample_id'], 189)
            self.assertEqual(publisher.heartbeat_monotonic, 100)

    def test_replacement_rejects_old_history_and_run_restart_does_not_merge_sample_ids(self):
        with tempfile.TemporaryDirectory() as directory, \
                closing(StatusPublisher(Path(directory)/'status.json')) as publisher:
            old = payload(10)
            snapshot = dict(run_id='capture', device_generation=8, modem_readable=True)
            publisher.update_platform(snapshot, dict(sample=old, acquisition_history=[{'sample_id': 10}]))
            self.assertIsNone(publisher.sample)
            self.assertEqual(list(publisher.history), [])
            current = payload(11, generation=8)
            stale_point = dict(sample_id=10, run_id='capture', device_generation=7)
            publisher.update_platform(snapshot, dict(sample=current, acquisition_history=[stale_point]))
            self.assertEqual([p['sample_id'] for p in publisher.history], [11])
            restarted = payload(1, generation=8, run='new-capture')
            publisher.update_platform(dict(snapshot, run_id='new-capture'), dict(sample=restarted,
                acquisition_history=[dict(sample_id=999, run_id='capture', device_generation=8)]))
            self.assertEqual([p['sample_id'] for p in publisher.history], [1])
