"""Retire host resources after a uniquely identified UE changes USB topology."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from modules.device.device import scan_devices
from modules.runtime.ipc import Heartbeat
from modules.runtime.state import RuntimeState


def relocated_device(owner, devices):
    """A unique serial can prove relocation; a missing/duplicate serial cannot."""
    candidates = [device for device in devices if device.serial and device.serial == owner.get('identity')]
    physical = {str(Path(device.usb_path).resolve()): device for device in candidates}
    if len(physical) != 1:
        return None
    topology, device = next(iter(physical.items()))
    return device if topology != owner.get('usb_path') else None


def retire_host_resources(state_path, settings, stop, *, expected_usb, discovery=scan_devices, network_factory=None):
    """Never reuse a WDS CID across USB transports or perform a QMI operation."""
    from modules.network.manager import Network
    state = RuntimeState(state_path)
    saved = state.load()
    if saved is None:
        return
    owner = json.loads(state.path.with_name('owner.json').read_text())
    device = relocated_device(owner, discovery())
    if (device is None or str(Path(device.usb_path).resolve()) != str(Path(expected_usb).resolve()) or
            saved.get('identity') != owner.get('identity') or saved.get('usb_path') != owner.get('usb_path')):
        raise RuntimeError('USB relocation could not be confirmed; ownership was retained')
    if stop.is_set():
        raise RuntimeError('USB relocation cleanup was cancelled; ownership was retained')
    network = (network_factory or Network)(settings.network, settings.recovery.operation_timeout, stop)
    network.restore(saved['network'])
    network.close()
    # A transport change invalidates the old CID context. The new actor will
    # negotiate/verify its APN; this cleanup never stops a bearer on the new UE.
    state.clear()


def relocation_worker(runtime, worker_id, settings, stop, *, owner_lock_fd, owner_lock_path,
                      state_path, device_lock_dir, usb_path):
    lock_path = Path(owner_lock_path)
    if owner_lock_fd is None:
        raise RuntimeError('USB relocation requires an inherited platform ownership lock')
    descriptor, expected = os.fstat(owner_lock_fd), lock_path.stat()
    if (descriptor.st_dev, descriptor.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError('Inherited modem lock does not match the instance lock')
    saved = RuntimeState(state_path).load()
    if saved is None:
        return 0
    directory = Path(device_lock_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A replacement UE may already occupy the old USB port. Retirement never
    # touches QMI, so serialize the journal operation instead of taking that
    # peer's physical lease. The parent reaps the old owner before starting us.
    key = 'retirement-'+hashlib.sha256(str(Path(state_path).resolve()).encode()).hexdigest()
    with os.fdopen(os.dup(owner_lock_fd), 'a') as platform_lock, (directory/(key+'.lock')).open('a') as physical_lock:
        fcntl.flock(platform_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(physical_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        heartbeat = Heartbeat(Path(runtime)/'modem.json', worker_id, settings)
        try:
            heartbeat.beat(force=True, phase='CLEANUP',
                operation_deadline=time.monotonic()+settings.recovery.operation_timeout*6+5)
            retire_host_resources(state_path, settings, stop, expected_usb=usb_path)
        finally:
            heartbeat.close()
    return 0
