"""One platform owner supervising an independent worker pair per physical UE."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import fcntl
import json
import logging
from logging import handlers
import os
from pathlib import Path
import re
import subprocess
import time

from modules.device.device import scan_devices
from modules.runtime.fleet_config import Configuration
from modules.runtime.fleet_status import modem_snapshot, signal_summary
from modules.runtime.configuration import configuration_lock
from modules.runtime.ipc import LatestFile, read_json, revision
from modules.runtime.processes import Child, ProcessManager, notify_systemd
from modules.runtime.ownership import OwnershipStore
from modules.runtime.relocation import relocated_device

LOG = logging.getLogger(__name__)


def device_key(device):
    """Use USB topology as ownership identity even when serials are duplicated."""
    return str(Path(device.usb_path).resolve())


def profile_slug(value):
    """USB text as an identifier fragment: 'Quectel' -> 'quectel'."""
    return re.sub(r'[^a-z0-9]+', '-', str(value or '').lower()).strip('-')[:24]


def automatic_profile_id(vendor, source, occupied=()):
    """A readable, stable id: auto-<manufacturer>-<serial or USB topology>."""
    base = ('auto-' + (vendor or 'modem'))[:24]
    room = max(1, 48 - len(base) - 1)
    base = (base + '-' + (source or 'modem')[:room]).rstrip('-')
    candidate, counter = base, 2
    while candidate in occupied:
        suffix = '-' + str(counter)
        candidate = base[:48 - len(suffix)].rstrip('-') + suffix
        counter += 1
    return candidate


def assignments(configuration, devices, previous=None):
    """Reserve explicit selectors first; never give a USB modem to two profiles."""
    previous = previous or {}
    # Several QMI functions on one physical UE must not become competing owners.
    physical = {}
    for device in sorted(devices, key=lambda value: value.path):
        physical.setdefault(device_key(device), device)
    devices = list(physical.values())
    remaining = set(physical)
    settings = {'default': configuration.base, **configuration.profiles}
    result = {}
    order = sorted(settings, key=lambda key: (
        settings[key].modem.device == 'auto', not bool(settings[key].modem.serial), key != 'default', key))
    def automatic_id(device, occupied=()):
        serial_unique = device.serial and sum(candidate.serial == device.serial for candidate in devices) == 1
        vendor = profile_slug(device.manufacturer) or profile_slug(device.product)
        if serial_unique:
            source = profile_slug(device.serial)
        else:
            # Duplicated or absent serials fall back to the USB topology.
            source = '-'.join(profile_slug(part) for part in Path(device.usb_path).parts[-2:])
        return automatic_profile_id(vendor, source, occupied)

    for identifier in order:
        value = settings[identifier]
        explicit = value.modem.device != 'auto' or bool(value.modem.serial)
        if not explicit:
            continue
        matches = [device for device in devices
                   if (value.modem.device == 'auto' or Path(value.modem.device).resolve() == Path(device.path).resolve())
                   and (not value.modem.serial or device.serial == value.modem.serial)]
        error = None
        selected = None
        if explicit and len(matches) > 1:
            error = 'Multiple physical modems match this selector; use a unique serial or device path'
        else:
            available = [device for device in matches if device_key(device) in remaining]
            available.sort(key=lambda device: (device_key(device) != previous.get(identifier), device.path))
            if available:
                selected = available[0]
                remaining.remove(device_key(selected))
            elif matches:
                error = 'The selected modem is already reserved by another profile'
        result[identifier] = (value, selected, error)
    automatic = configuration.base.modem.device == 'auto' and not configuration.base.modem.serial
    for device in devices if automatic else []:
        identifier = next((key for key, topology in previous.items() if key.startswith('auto-') and
                           key not in settings and key not in result and topology == device_key(device)), None)
        if identifier is not None and device_key(device) in remaining:
            result[identifier] = (configuration.base, device, None)
            remaining.remove(device_key(device))
    for identifier in order:
        if identifier in result:
            continue
        available = [physical[key] for key in remaining]
        available.sort(key=lambda device: (device_key(device) != previous.get(identifier), device.path))
        selected = available[0] if available else None
        if selected:
            remaining.remove(device_key(selected))
        result[identifier] = (settings[identifier], selected, None)
    if automatic:
        for key in sorted(remaining):
            device = physical[key]
            # An editable auto-profile may have been rebound to another UE.
            # Keep its saved intent and allocate a new stable ID for this UE.
            identifier = automatic_id(device, occupied=set(result) | set(settings))
            result[identifier] = (configuration.base, device, None)
        for identifier in previous:
            if identifier.startswith('auto-') and identifier not in result and identifier not in settings:
                result[identifier] = (configuration.base, None, None)
    return result


class FleetManager:
    """The parent imports neither GI nor modem backends; workers own resources."""
    def __init__(self, config, stop, *, runtime='/run/qmi-supervisor', popen=subprocess.Popen,
                 discovery=scan_devices, clock=time.monotonic, device_override=None, log_level='INFO',
                 log_level_override=False):
        self.config, self.stop, self.runtime = Path(config).resolve(), stop, Path(runtime)
        self.popen, self.discovery, self.clock = popen, discovery, clock
        self.device_override, self.log_level = device_override, log_level
        self.log_level_override = log_level_override
        self.log_file = ''
        self.configuration = Configuration.load(self.config)
        self.members, self.bindings, self.inventory = {}, read_json(self.runtime/'bindings.json') or {}, []
        self.ownership = OwnershipStore(self.runtime)
        self.cleanup_members, self.ownership_cleanup = {}, []
        self.ownership_error = None
        self.web = Child('web')
        self.publisher = None
        self.error = None
        self.discovery_error = self.materialization_error = None
        self.runtime_errors = {}
        self.owner_lock_fd = None
        self.next_scan = 0
        self._fingerprint = None
        self._status_cache = {}
        self._devices = []

    def reload(self):
        try:
            stat = self.config.stat()
            fingerprint = stat.st_mtime_ns, stat.st_size, stat.st_ino
            if fingerprint != self._fingerprint:
                self._fingerprint = fingerprint
                candidate = Configuration.load(self.config)
                if self.device_override and candidate.profiles:
                    raise ValueError('--device cannot override a configuration containing named modem profiles')
                self.configuration = candidate
                self.error = None
        except (OSError, ValueError) as error:
            self.error = str(error)

    @staticmethod
    def close_member(manager):
        for role in ('telemetry', 'modem', 'web'):
            if not manager.children[role].stop(timeout=6):
                return False
        if manager.publisher:
            manager.publisher.close()
            manager.publisher = None
        return True

    def cached_status(self, identifier):
        path = self.runtime/'modems'/identifier/'status.json'
        try:
            stat = path.stat()
            fingerprint = stat.st_mtime_ns, stat.st_size, stat.st_ino
            previous = self._status_cache.get(identifier)
            if previous is None or previous[0] != fingerprint:
                value = read_json(path)
                if value is not None:
                    self._status_cache[identifier] = (fingerprint, value)
            return self._status_cache.get(identifier, (None, {}))[1]
        except OSError:
            return self._status_cache.get(identifier, (None, {}))[1]

    @staticmethod
    def effective_settings(identifier, settings, device):
        pinned = replace(settings, modem=replace(settings.modem, device=device.path, serial=device.serial))
        if identifier != 'default':
            pinned = replace(pinned, telemetry=replace(pinned.telemetry,
                directory=str(Path(pinned.telemetry.directory)/'profiles'/identifier)))
        return pinned

    def reconcile_member(self, identifier, pinned, device):
        from modules.deployment.provisioning import render_config, write_atomic
        runtime = self.runtime/'modems'/identifier
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = runtime/'settings.ini'
        content = render_config(pinned)
        if not path.exists() or path.read_text() != content:
            write_atomic(path, content)
        manager = self.members.get(identifier)
        if manager is None:
            state_path = self.ownership.prepare(identifier, device, pinned, self._devices,
                                                self._ownership_bindings)
            options = dict(owner_lock_path=self.runtime.parent/'qmi-supervisor.lock',
                state_path=state_path,
                device_lock_dir=self.runtime/'device-locks', usb_path=device.usb_path,
                peer_routes=self.runtime/'route-peers.json')
            manager = ProcessManager(path, pinned, self.stop, runtime=runtime, popen=self.popen,
                clock=self.clock, log_level=self.log_level, manage_web=False, worker_options=options)
            manager.assignment = device.path, device_key(device), device.identity, device.enumeration
            manager.owner_lock_fd = self.owner_lock_fd
            self.members[identifier] = manager
        manager.reconcile()
        return manager

    def checkpoint_path(self, identifier):
        return (self.runtime.parent/'qmi-supervisor-state.json' if identifier == 'default'
                else self.runtime/'modems'/identifier/'connection-state.json')

    def cleanup_pending(self, identifier, settings):
        if settings.modem.mode == 'connect':
            return False
        try:
            return self.checkpoint_path(identifier).exists() or self.ownership.pending_for(identifier)
        except (OSError, ValueError):
            return True

    def reconcile_cleanup(self, planned):
        """Release abandoned owned bearers without applying network objectives."""
        reserved = {device_key(device) for _, device, _ in planned.values() if device}
        reserved.update(member.assignment[1] for member in self.members.values())
        self.ownership.migrate_unassigned(self._devices, reserved, self._ownership_bindings, self.configuration)
        by_usb = {device_key(device): device for device in self._devices}
        self.ownership_cleanup = []
        for owner, checkpoint in self.ownership.records():
            if not checkpoint.exists():
                if self.ownership.attach_path(checkpoint).exists() and owner['usb_path'] not in reserved:
                    self.ownership_cleanup.append(dict(profile=owner['profile'], state='WAIT_REGISTRATION',
                        error='Pending attach profile recovery requires connection management on the original UE'))
                continue
            relocated = relocated_device(owner, self._devices)
            device = relocated or by_usb.get(owner['usb_path'])
            if (relocated is None and device is not None and device.identity == owner['identity']
                    and owner['usb_path'] in reserved):
                continue
            if relocated is not None and any(member.assignment[1] == owner['usb_path'] and
                    member.assignment[2] == owner['identity']
                    for member in self.members.values()):
                self.ownership_cleanup.append(dict(profile=owner['profile'], state='STOPPING',
                    error='Previous USB transport owner has not been reaped'))
                continue
            if device is None or device.identity != owner['identity']:
                self.ownership_cleanup.append(dict(profile=owner['profile'], state='WAIT_DEVICE',
                    error='Owned connection cleanup is pending; reconnect the original physical modem'))
                continue
            try:
                member = self.reconcile_cleanup_member(checkpoint, device, retire_relocated=relocated is not None)
                state, error = 'CLEANUP', member.error
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as failure:
                state, error = 'BACKOFF', str(failure)
            self.ownership_cleanup.append(dict(profile=owner['profile'], state=state, error=error))

    def reconcile_cleanup_member(self, checkpoint, device, *, retire_relocated=False):
        from modules.deployment.provisioning import render_config, write_atomic
        from modules.runtime.settings import load_settings
        key = checkpoint.parent.name
        member = self.cleanup_members.get(key)
        if member is not None and (member.assignment != (device.path, device_key(device), device.identity, device.enumeration) or
                bool(member.worker_options.get('retire_relocated')) != retire_relocated):
            if self.clock() < getattr(member, '_cleanup_retry', 0) and any(
                    child.alive for child in member.children.values()):
                raise RuntimeError('Previous cleanup owner could not be reaped; replacement is blocked')
            if not self.close_member(member):
                failures = getattr(member, '_cleanup_failures', 0)
                member._cleanup_retry = self.clock()+min(2*2**min(failures, 5), 60)
                member._cleanup_failures = failures+1
                raise RuntimeError('Previous cleanup owner could not be reaped; replacement is blocked')
            del self.cleanup_members[key]
            member = None
        if member is None:
            settings = load_settings(checkpoint.with_name('settings.ini'))
            settings = replace(settings, modem=replace(settings.modem, device=device.path,
                serial=device.serial, mode='monitor'), telemetry=replace(settings.telemetry, enabled=False))
            path = checkpoint.with_name('cleanup.ini')
            write_atomic(path, render_config(settings))
            member = ProcessManager(path, settings, self.stop, runtime=checkpoint.parent/'cleanup',
                popen=self.popen, clock=self.clock, log_level=self.log_level, manage_web=False,
                worker_options=dict(owner_lock_path=self.runtime.parent/'qmi-supervisor.lock',
                    state_path=checkpoint, device_lock_dir=self.runtime/'device-locks',
                    usb_path=device.usb_path, peer_routes=self.runtime/'route-peers.json',
                    retire_relocated=retire_relocated))
            member.assignment = device.path, device_key(device), device.identity, device.enumeration
            member.owner_lock_fd = self.owner_lock_fd
            self.cleanup_members[key] = member
        member.reconcile()
        return member

    def stop_cleanup(self, planned):
        """Reap cleanup actors before the device can return to a normal profile."""
        reserved = {device_key(device) for _, device, _ in planned.values() if device}
        blocked = set()
        for key, member in list(self.cleanup_members.items()):
            checkpoint = Path(member.worker_options['state_path'])
            if checkpoint.exists() and (member.worker_options.get('retire_relocated') or
                                       member.assignment[1] not in reserved):
                continue
            if self.clock() < getattr(member, '_cleanup_retry', 0) and any(
                    child.alive for child in member.children.values()):
                blocked.add(member.assignment[1])
                continue
            if self.close_member(member):
                del self.cleanup_members[key]
            else:
                failures = getattr(member, '_cleanup_failures', 0)
                member._cleanup_retry = self.clock()+min(2*2**min(failures, 5), 60)
                member._cleanup_failures = failures+1
                blocked.add(member.assignment[1])
        return blocked

    def save_runtime(self, name, content):
        from modules.deployment.provisioning import write_atomic
        path = self.runtime/name
        try:
            if not path.exists() or path.read_text() != content:
                write_atomic(path, content)
            self.runtime_errors.pop(name, None)
            return True
        except OSError as error:
            self.runtime_errors[name] = f'Cannot publish {name}: {error}'
            return False

    def platform_error(self):
        return '; '.join(value for value in (self.error, self.discovery_error, self.materialization_error,
                                              self.ownership_error,
                                              *self.runtime_errors.values()) if value) or None

    def _apply_log_file(self, path):
        """Optional rotating file sink next to the journal; empty keeps journald only."""
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, 'qmi_log_file', False):
                root.removeHandler(handler)
                handler.close()
        if not path:
            return
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            handler = handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3)
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
            handler.qmi_log_file = True
            root.addHandler(handler)
            LOG.info('Logging to %s', path)
        except OSError as error:
            LOG.warning('Could not open %s: %s', path, error)

    def reconcile(self):
        from modules.deployment.provisioning import write_atomic
        self.reload()
        configuration = self.configuration
        if self.device_override:
            if configuration.profiles:
                self.error = '--device cannot override a configuration containing named modem profiles'
                return
            configuration = configuration.update('default', replace(configuration.base,
                modem=replace(configuration.base.modem, device=self.device_override)))
        level = configuration.base.system.log_level
        if not self.log_level_override and level != self.log_level:
            self.log_level = level
            logging.getLogger().setLevel(level)
            for manager in self.members.values():
                manager.log_level = level
            LOG.info('Log level changed to %s', level)
        want_file = configuration.base.system.log_file
        if want_file != self.log_file:
            self.log_file = want_file
            self._apply_log_file(want_file)
        now = self.clock()
        if now >= self.next_scan:
            self.next_scan = now+1
            try:
                self._devices = list(self.discovery())
                self.discovery_error = None
            except OSError as error:
                self.discovery_error = f'Modem discovery failed: {error}'
                # Keep existing ownership on a transient discovery error.
        planned = assignments(configuration, self._devices, self.bindings)
        # Migration must use the old bindings, before a profile is rebound.
        self._ownership_bindings = dict(self.bindings)
        automatic = {identifier: (settings, device) for identifier, (settings, device, _) in planned.items()
                     if identifier.startswith('auto-') and identifier not in configuration.profiles and device}
        self.materialization_error = None
        unmaterialized = set()
        if automatic:
            try:
                # Materialise new devices once. Editing the default modem later
                # must not silently change another UE's APN or objectives.
                with configuration_lock(self.config):
                    stored = Configuration.load(self.config)
                    refreshed = assignments(stored, self._devices, self.bindings)
                    for identifier, (_, device, _) in refreshed.items():
                        if not identifier.startswith('auto-') or identifier in stored.profiles or device is None:
                            continue
                        unique = device.serial and sum(value.serial == device.serial for value in self._devices) == 1
                        pinned = replace(stored.base, modem=replace(stored.base.modem,
                            device='auto' if unique else device.path, serial=device.serial))
                        stored = stored.update(identifier, pinned)
                    write_atomic(self.config, stored.render())
                self.configuration = configuration = stored
                planned = assignments(configuration, self._devices, self.bindings)
            except (OSError, ValueError) as error:
                self.materialization_error = f'Cannot persist automatic modem profiles: {error}'
                for identifier in automatic:
                    settings, device, _ = planned[identifier]
                    planned[identifier] = (settings, device, self.materialization_error)
                    unmaterialized.add(identifier)
        blocked = set()
        for identifier, manager in list(self.members.items()):
            target = planned.get(identifier)
            device = target[1] if target else None
            selector = (device.path, device_key(device), device.identity, device.enumeration) if device else None
            if selector != getattr(manager, 'assignment', None):
                waiting = now < getattr(manager, '_cleanup_retry', 0) and any(
                    child.alive for child in manager.children.values())
                if not waiting and self.close_member(manager):
                    del self.members[identifier]
                    self._status_cache.pop(identifier, None)
                else:
                    # Never replace an owner that has not been reaped.
                    if not waiting:
                        failures = getattr(manager, '_cleanup_failures', 0)
                        manager._cleanup_retry = self.clock()+min(2*2**min(failures, 5), 60)
                        manager._cleanup_failures = failures+1
                    blocked.add(identifier)
                    desired = target[0] if target else replace(manager.settings,
                        modem=replace(manager.settings.modem, mode='monitor'),
                        telemetry=replace(manager.settings.telemetry, enabled=False))
                    planned[identifier] = (desired, device, 'Previous modem owner could not be reaped; replacement is blocked')
        cleanup_blocked = self.stop_cleanup(planned)
        cleanup_blocked.update(self.members[identifier].assignment[1] for identifier in blocked)
        inventory = []
        for identifier, (settings, device, error) in planned.items():
            manager = self.members.get(identifier)
            if device:
                self.bindings[identifier] = device_key(device)
                pinned = self.effective_settings(identifier, settings, device)
                if device_key(device) in cleanup_blocked:
                    blocked.add(identifier)
                    error = 'Previous cleanup owner could not be reaped; replacement is blocked'
                if identifier not in blocked and identifier not in unmaterialized:
                    try:
                        manager = self.reconcile_member(identifier, pinned, device)
                        if manager.error:
                            error = f'Profile application failed: {manager.error}'
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as failure:
                        manager = self.members.get(identifier)
                        error = f'Profile application failed: {failure}'
                        if manager is not None:
                            # Supervise the last valid runtime configuration even
                            # while a new configuration cannot be written.
                            try:
                                manager.reconcile()
                            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as recovery:
                                error += f'; last valid profile recovery failed: {recovery}'
                elif identifier in unmaterialized and manager is not None:
                    try:
                        manager.reconcile()
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as recovery:
                        error += f'; last valid profile recovery failed: {recovery}'
            record = self.cached_status(identifier) if device else {}
            child = manager.children['modem'] if manager else None
            worker_id = child.worker_id if child and child.process is not None else None
            if (manager is None or record.get('profile_instance_id') != manager.instance_id or
                    ((record.get('processes') or {}).get('modem') or {}).get('worker_id') != worker_id):
                record = {}
            snapshot = manager.children['modem'].status.get('snapshot', {}) if manager else {}
            serial_unique = device and device.serial and sum(d.serial == device.serial for d in self._devices) == 1
            selector = dict(device='auto' if serial_unique else device.path if device else settings.modem.device,
                            serial=device.serial if device else settings.modem.serial)
            cleanup_pending = self.cleanup_pending(identifier, settings)
            if cleanup_pending and device is None:
                error = error or 'Owned connection cleanup is pending; reconnect the assigned modem'
            active = settings.modem.mode == 'connect' or settings.telemetry.enabled or cleanup_pending
            features = record.get('features') or {key: dict(enabled=enabled, effective=False, pending=enabled,
                state='WAIT_DEVICE' if enabled else 'DISABLED', fresh=not enabled, error=error,
                desired_revision=revision(settings, sections), applied_revision=None)
                for key, enabled, sections in (
                    ('connection', settings.modem.mode == 'connect', ('modem', 'network', 'recovery')),
                    ('telemetry', settings.telemetry.enabled, ('telemetry',)))}
            if manager is not None:
                actual = manager.settings
                features = {
                    'connection': manager.feature('modem', actual.modem.mode == 'connect',
                        revision(actual, ('modem', 'network', 'recovery'))),
                    'telemetry': manager.feature('telemetry', actual.telemetry.enabled,
                        revision(actual, ('telemetry',)))}
            if error:
                desired = pinned if device else settings
                pending_features = {}
                for key, enabled, sections, role in (
                        ('connection', settings.modem.mode == 'connect', ('modem', 'network', 'recovery'), 'modem'),
                        ('telemetry', settings.telemetry.enabled, ('telemetry',), 'telemetry')):
                    value = features.get(key, {})
                    requested = revision(desired, sections)
                    disabled = not enabled and (manager is None or manager.children[role].process is None)
                    pending_features[key] = dict(value, enabled=enabled, error=error,
                        applied_revision=requested if disabled else value.get('applied_revision'),
                        desired_revision=requested,
                        pending=not disabled and value.get('applied_revision') != requested)
                features = pending_features
            if cleanup_pending:
                value = features['connection']
                features['connection'] = dict(value, pending=True, cleanup_pending=True,
                    applied_revision=None if manager is None else value.get('applied_revision'),
                    error=error or value.get('error'))
            inventory.append(dict(id=identifier, configured=identifier == 'default' or identifier in configuration.profiles,
                detected=device is not None, device=asdict(device) if device else None, selector=selector,
                assignment_pending=identifier in blocked,
                cleanup_pending=cleanup_pending,
                profile_instance_id=manager.instance_id if manager else None,
                modem_worker_id=worker_id, modem_alive=bool(child and child.alive),
                state=('STOPPING' if identifier in blocked else 'IDLE' if not active else
                       'WAIT_DEVICE' if device is None else 'STARTING' if manager is None else
                       'BACKOFF' if not child.alive else snapshot.get('state', record.get('state', 'STARTING'))),
                error=error or record.get('error'), apn=settings.modem.apn,
                connection_enabled=settings.modem.mode == 'connect', monitoring_enabled=settings.telemetry.enabled,
                features=features, interface=device.interface if device else None,
                network=record.get('network', snapshot.get('network', {})),
                signal=signal_summary(record, now=self.clock(), interval=settings.telemetry.interval,
                                      alive=bool(child and child.alive))))
        self.inventory = inventory
        try:
            self.reconcile_cleanup(planned)
            self.ownership_error = None
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            self.ownership_error = f'Owned connection recovery failed: {error}'
        if self.bindings != getattr(self, '_saved_bindings', None):
            if self.save_runtime('bindings.json', json.dumps(self.bindings, sort_keys=True)):
                self._saved_bindings = dict(self.bindings)
        peers = []
        for entry in inventory:
            if not entry['detected']:
                continue
            settings = planned[entry['id']][0]
            connectivity = entry['network'].get('connectivity') or {}
            peers.append(dict(id=entry['id'], interface=entry['interface'],
                metric=settings.network.route_metric, policy=settings.network.route_policy,
                enabled=settings.modem.mode == 'connect' and settings.network.default_route and not entry['assignment_pending'],
                checkpoint=str(self.ownership.checkpoint(planned[entry['id']][1])),
                identity=entry['device']['serial'] or planned[entry['id']][1].identity,
                usb_path=device_key(planned[entry['id']][1]),
                enumeration=entry['device']['enumeration'],
                route=entry['network'].get('route'),
                fallback=settings.network.connectivity_check and connectivity.get('state') != 'up'))
        content = json.dumps({'peers': peers}, sort_keys=True)
        self.save_runtime('route-peers.json', content)
        self.reconcile_web()
        if configuration.base.web.enabled:
            self.publish()
        elif self.publisher:
            self.publisher.close()
            self.publisher = None

    def reconcile_web(self):
        settings, now = self.configuration.base, self.clock()
        child = self.web
        child.observe(self.runtime, clock=self.clock)
        signature = revision(settings, ('web',))
        if not settings.web.enabled or (child.process is not None and
                                        (signature != child.signature or child.log_level != self.log_level)):
            child.stop()
            child.next_start = 0
        elif child.process is not None and (not child.alive or now-child.last_heartbeat > 15):
            child.error = 'Web worker exited or stopped updating its heartbeat'
            child.stop(timeout=3, failed=True, clock=self.clock)
        if settings.web.enabled and child.process is None and now >= child.next_start:
            try:
                child.start(self.config, self.runtime, signature, popen=self.popen, clock=self.clock,
                            log_level=self.log_level)
            except OSError as error:
                child.error = str(error)
                child.next_start = now+min(2*2**min(child.failures, 5), 60)
                child.failures += 1

    def publish(self):
        if self.publisher is None:
            from modules.telemetry.resources import ProcessSampler
            sampler = ProcessSampler(clock=self.clock)
            observed, next_resources = None, 0
            def resources(value):
                nonlocal observed, next_resources
                now = self.clock()
                if observed is None or now >= next_resources:
                    observed = sampler.sample({'manager': os.getpid(), 'web': self.web.process})
                    next_resources = now+5
                merged = dict(value.get('resources') or {})
                for key in ('manager', 'web'):
                    merged[key] = observed[key]
                merged.update({key: observed[key] for key in ('sampled_at_utc', 'sampled_monotonic', 'interval_seconds')})
                return dict(value, resources=merged, boot_id=sampler.boot_id,
                            published_monotonic=now, published_at_utc=datetime.now(timezone.utc).isoformat())
            self.publisher = LatestFile(self.runtime/'status.json', transform=resources)
        selected = next((entry['id'] for entry in self.inventory if entry['id'] == 'default'),
                        next((entry['id'] for entry in self.inventory), 'default'))
        entry = next((value for value in self.inventory if value['id'] == selected), {})
        snapshot = modem_snapshot(self.cached_status(selected), entry, manager_heartbeat=self.clock())
        web = self.web
        processes = dict(snapshot.get('processes') or {})
        processes['web'] = dict(alive=web.alive, pid=web.process.pid if web.process else None,
            state=web.status.get('phase') if web.alive else 'STOPPED', error=web.error or web.status.get('error'),
            restarts=web.restarts, heartbeat_monotonic=web.last_heartbeat if web.alive else None,
            heartbeat_age_ms=max(0, self.clock()-web.last_heartbeat)*1000 if web.alive else None,
            fresh=web.alive and bool(web.status) and self.clock()-web.last_heartbeat < 15)
        snapshot.update(fleet=True, selected_modem=selected, modems=self.inventory,
                        ownership_cleanup=self.ownership_cleanup,
                        config_revision=self.configuration.revision, configuration_error=self.platform_error(),
                        processes=processes, manager_heartbeat_monotonic=self.clock())
        self.publisher.offer(snapshot)

    def run(self):
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime.chmod(0o700)
        with (self.runtime/'manager.lock').open('a') as lock, \
                (self.runtime.parent/'qmi-supervisor.lock').open('a') as modem_lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(modem_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.error('Another QMI Supervisor instance or legacy modem owner is running')
                return 1
            self.owner_lock_fd = modem_lock.fileno()
            notify_systemd('READY=1')
            try:
                while not self.stop.is_set():
                    self.reconcile()
                    notify_systemd('WATCHDOG=1')
                    self.stop.wait(.25)
            finally:
                notify_systemd('STOPPING=1')
                for manager in self.members.values():
                    self.close_member(manager)
                for manager in self.cleanup_members.values():
                    self.close_member(manager)
                self.web.stop(timeout=8)
                if self.publisher:
                    self.publisher.close()
        return 0
