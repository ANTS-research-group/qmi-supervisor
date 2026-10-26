"""Synthetic modem process for IPC and lifecycle acceptance; no GI or networking."""
import argparse
import errno
import os
from datetime import datetime, timezone
from pathlib import Path
import signal
import threading
import time

from modules.runtime.ipc import AcquisitionServer, Heartbeat, revision
from modules.runtime.settings import load_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--worker-id', required=True)
    parser.add_argument('--owner-lock-fd', type=int)
    parser.add_argument('--recover-owned-connection', action='store_true')
    parser.add_argument('--device')
    parser.add_argument('--log-level', default='INFO')
    for option in ('owner-lock-path', 'state-path', 'device-lock-dir', 'usb-path', 'peer-routes'):
        parser.add_argument('--'+option)
    args = parser.parse_args()
    if args.role != 'modem':
        if args.role == 'telemetry' and os.environ.get('QMI_TEST_STORAGE_MODE_FILE'):
            from modules.telemetry.collector import Telemetry
            original = Telemetry.persist
            mode_path = Path(os.environ['QMI_TEST_STORAGE_MODE_FILE'])
            def persist(collector, payload):
                mode = mode_path.read_text().strip()
                if mode == 'full':
                    raise OSError(errno.ENOSPC, 'Synthetic full storage')
                if mode == 'slow':
                    time.sleep(.35)
                if mode == 'blocked':
                    threading.Event().wait()
                return original(collector, payload)
            Telemetry.persist = persist
        if args.role == 'web' and os.environ.get('QMI_TEST_STATIC_ROOT'):
            from functools import partial
            from modules.web import server
            server.serve = partial(server.serve, static_root=Path(os.environ['QMI_TEST_STATIC_ROOT']))
        from modules.runtime.workers import main as worker
        return worker()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    settings = load_settings(args.config)
    heartbeat = Heartbeat(args.runtime/'modem.json', args.worker_id, settings)
    server = AcquisitionServer(args.runtime/'modem.sock', stop)
    pending = threading.Event()
    server.wakeup = pending.set
    device = dict(path='/dev/synthetic-qmi', interface='synthetic-wwan', usb_path='/sys/synthetic',
                  serial='synthetic-ue', product='Synthetic modem')
    if args.usb_path:
        device.update(path=settings.modem.device, usb_path=args.usb_path,
                      interface='synthetic-'+Path(settings.modem.device).name, serial=settings.modem.serial)
    generation = time.monotonic_ns() // 1000
    acquisitions, last_reload = 0, 0
    if args.recover_owned_connection and args.state_path:
        Path(args.state_path).unlink(missing_ok=True)
    try:
        while not stop.is_set():
            pending.clear()
            if time.monotonic()-last_reload > 0.5:
                settings = load_settings(args.config)
                heartbeat.settings = settings
                last_reload = time.monotonic()
            phase = 'ONLINE' if settings.modem.mode == 'connect' else 'MONITOR'
            item = server.take()
            if item:
                started = time.monotonic()
                acquisitions += 1
                item['response'] = dict(device=device, generation=generation,
                    static={'dms_value_imei': '000000000000001', 'dms_value_imsi': '999100000000001'},
                    dynamic={'nas_value_5g_signal_strength_rsrp': -85,
                             'nas_value_5g_signal_strength_extended': -11,
                             'nas_value_5g_signal_strength_snr': 21},
                    report={'requests': {}, 'observation_state': phase, 'synthetic_apn': settings.modem.apn},
                    window=dict(started=started, start_utc=datetime.now(timezone.utc).isoformat(),
                                ended=time.monotonic(), queue_wait_ms=(started-item['queued'])*1000))
                item['done'].set()
            heartbeat.beat(phase=phase, error=None,
                applied_revision=revision(settings, ('modem', 'network', 'recovery')),
                snapshot=dict(state=phase, device=device, network={}, device_generation=generation,
                              synthetic_acquisitions=acquisitions))
            pending.wait(0.1)
    finally:
        server.close()
        heartbeat.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
