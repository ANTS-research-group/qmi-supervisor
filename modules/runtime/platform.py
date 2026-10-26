"""Optional observability: a bounded latest snapshot and a separate HTTP process."""
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import subprocess
import sys
import threading
import time

from modules.device.radio import radio_summary
from modules.telemetry.resources import ProcessSampler
from modules.telemetry.history import sample_point

STATUS_PATH = Path('/run/qmi-supervisor/status.json')


def _history_error(error, limit=256):
    """Bound JSON-escaped bytes too: one emoji expands to twelve ASCII bytes."""
    if not isinstance(error, str):
        return error, False
    prefix, size = [], 0
    for character in error:
        cost = len(json.dumps(character, ensure_ascii=True))-2
        if size+cost > limit:
            return ''.join(prefix), True
        prefix.append(character)
        size += cost
    return error, False


class StatusPublisher:
    def __init__(self, path=STATUS_PATH, history_size=120, *, pipeline_history_size=None,
                 resource_targets=None, resource_interval=5, process_sampler=None,
                 clock=time.monotonic, utcnow=None):
        if history_size < 1 or (pipeline_history_size is not None and pipeline_history_size < 1):
            raise ValueError('History sizes must be positive')
        if not math.isfinite(resource_interval) or resource_interval <= 0:
            raise ValueError('Resource interval must be positive')
        self.path = Path(path)
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._condition = threading.Condition()
        self._closing = False
        self._dirty = True
        self.sample = None
        self.state = {'state': 'STARTING', 'device': None}
        self.history = deque(maxlen=history_size)
        self.pipeline_history = deque(maxlen=pipeline_history_size or history_size)
        self._last_pipeline = None
        self._published_sample = None
        self._active_device_key = None
        self._sample_run = None
        self.heartbeat_monotonic = self.heartbeat_at_utc = None
        self._sampler = process_sampler or ProcessSampler(clock=clock, utcnow=self.utcnow)
        self.boot_id = self._sampler.boot_id
        self.resources = None
        self._resource_targets = resource_targets
        self._resource_interval = resource_interval
        self._next_resources = self.clock()
        self._worker = threading.Thread(target=self._run, daemon=True, name='web-status-snapshot')
        self._worker.start()

    def set_resource_targets(self, provider):
        with self._condition:
            if self._closing:
                return
            self._resource_targets = provider
            self._next_resources = self.clock()
            self._condition.notify()

    def resize_history(self, size):
        with self._condition:
            self.history = deque(self.history, maxlen=size)
            self.pipeline_history = deque(self.pipeline_history, maxlen=size)

    def update_platform(self, snapshot, telemetry=None):
        """Merge actors without turning file reads into new actor heartbeats."""
        with self._condition:
            self.state = dict(snapshot)
            self.heartbeat_monotonic = snapshot.get('heartbeat_monotonic')
            self.heartbeat_at_utc = snapshot.get('heartbeat_at_utc')
            if telemetry:
                payload = telemetry.get('sample')
                if payload and (snapshot.get('modem_readable') is False
                        or payload.get('device_generation') != snapshot.get('device_generation')
                        or snapshot.get('processes', {}).get('modem', {}).get('alive') is False):
                    payload = None
                if payload is None:
                    self.sample = None
                key = ((payload.get('timing', {}).get('run_id'), payload.get('log_id'))
                       if payload else None)
                if key is not None and key != getattr(self, '_last_platform_sample', None):
                    self.offer(payload)
                    retained = telemetry.get('acquisition_history')
                    if self.sample is payload and isinstance(retained, list):
                        # The actor may acquire several samples between file
                        # publications. Recover actual points, never interpolate.
                        run = payload.get('timing', {}).get('run_id')
                        generation = payload.get('device_generation')
                        points = {}
                        for point in list(self.history) + retained[-self.history.maxlen:]:
                            if (not isinstance(point, dict) or point.get('run_id') != run
                                    or point.get('device_generation') != generation):
                                continue
                            identifier = point.get('sample_id')
                            if type(identifier) is int and 0 <= identifier <= payload['log_id']:
                                points[identifier] = point
                        self.history.clear()
                        self.history.extend(points[identifier] for identifier in sorted(points)[-self.history.maxlen:])
                    self._last_platform_sample = key
                observed = telemetry.get('heartbeat_monotonic')
                if observed is not None and (self._last_pipeline is None or observed > self._last_pipeline):
                    point = dict(telemetry.get('stats', {}), observed_monotonic=observed,
                                 time=telemetry.get('heartbeat_at_utc'), run_id=telemetry.get('run_id'))
                    error, truncated = _history_error(point.get('last_persistence_error'))
                    if truncated:
                        point.update(last_persistence_error=error, last_persistence_error_truncated=True)
                    self.pipeline_history.append(point)
                    self._last_pipeline = observed
            else:
                self.sample = None
            self._dirty = True
            self._condition.notify()

    def offer(self, payload):
        with self._condition:
            if self._closing:
                return
            key = payload.get('device'), payload.get('device_identity'), payload.get('device_generation')
            run = payload.get('timing', {}).get('run_id')
            active_generation = self._active_device_key[2] if self._active_device_key else None
            if (isinstance(key[2], int) and isinstance(active_generation, int)
                    and key[2] < active_generation):
                return
            if run is not None and self.state.get('run_id') not in {None, run}:
                return
            if ((any(value is not None for value in key) and self._active_device_key is not None
                 and key != self._active_device_key) or
                    (self._sample_run is not None and run != self._sample_run)):
                self.history.clear()
            if any(value is not None for value in key):
                self._active_device_key = key
            self._sample_run = run
            self.sample = payload
            point = sample_point(payload)
            self.history.append(point)
            self._dirty = True
            self._condition.notify()

    def heartbeat(self, supervisor):
        observed, observed_utc = self.clock(), self.utcnow().isoformat()
        device = supervisor.device
        network = getattr(supervisor.backend, 'network', None)
        generation = getattr(supervisor.backend, 'generation', None)
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = None
        telemetry = supervisor.telemetry.stats()
        run_id = getattr(supervisor.telemetry, 'run_id', None)
        if not isinstance(run_id, str):
            run_id = None
        values = {'state': supervisor.state.name, 'device': asdict(device) if device else None,
                  'error': supervisor.last_error, 'failed_step': supervisor.failed_state,
                  'retry_in_seconds': max(0, supervisor.deadline-supervisor.clock())
                                      if supervisor.state.name == 'BACKOFF' else None,
                  'network': {'address': getattr(network, 'address', None),
                              'route': getattr(network, 'route', None),
                              'route_metric': getattr(network, 'route_metric', None),
                              'requested_route_metric': supervisor.settings.network.route_metric,
                              'route_policy': supervisor.settings.network.route_policy,
                              'connectivity': getattr(network, 'connectivity_status', None)},
                  'connection': {'phase': supervisor.state.name,
                                 'mode': supervisor.settings.modem.mode,
                                 'requested_apn': supervisor.settings.modem.apn,
                                 'requested_plmn': supervisor.settings.modem.plmn or None},
                  'telemetry': telemetry, 'skipped_intervals': supervisor.skipped_intervals,
                  'device_generation': generation, 'run_id': run_id}
        with self._condition:
            if self._closing:
                return
            key = (device.path, device.identity, generation) if device else None
            if key is not None and self._active_device_key is not None and key != self._active_device_key:
                self.sample = None
                self.history.clear()
            if key is not None:
                self._active_device_key = key
            # Registration observations are current radio/SIM data, not an ONLINE bearer.
            # An earlier ONLINE observation must not survive a reopen or a failed session.
            phase = self.sample.get('collection', {}).get('observation_state') if self.sample else None
            if (supervisor.state.name not in {'ONLINE', 'MONITOR', 'WAIT_REGISTRATION'}
                    or (supervisor.state.name == 'WAIT_REGISTRATION' and phase != 'WAIT_REGISTRATION')
                    or (phase is not None and phase != supervisor.state.name)):
                self.sample = None
            self.state = values
            self.heartbeat_monotonic, self.heartbeat_at_utc = observed, observed_utc
            if self._last_pipeline is None or math.floor(observed) > math.floor(self._last_pipeline):
                point = dict(telemetry, time=observed_utc, observed_monotonic=observed, run_id=run_id)
                # Detailed errors remain in the latest state. Repeating an
                # unbounded error in 1,000 history points would bloat every GET.
                error = point.get('last_persistence_error')
                error, truncated = _history_error(error)
                if truncated:
                    point['last_persistence_error'] = error
                    point['last_persistence_error_truncated'] = True
                self.pipeline_history.append(point)
                self._last_pipeline = observed
            self._dirty = True
            self._condition.notify()

    def _sample_key(self):
        sample = self.sample or {}
        return (sample.get('timing') or {}).get('run_id'), sample.get('log_id')

    def _run(self):
        last = None
        last_error = None
        while True:
            with self._condition:
                if self._closing and not self._dirty:
                    return
                now = self.clock()
                resource_due = (not self._closing and self._resource_targets is not None
                                and now >= self._next_resources)
                remaining = max(0, 1-(now-last)) if last is not None else 0
                # A new observation is written at once; the one-second grid only
                # paces liveness and resource updates, and must not skip a sample.
                if self._sample_key() != self._published_sample:
                    remaining = 0
                if not resource_due and not (self._dirty and (not remaining or self._closing)):
                    waits = []
                    if self._resource_targets is not None:
                        waits.append(max(0, self._next_resources-now))
                    if self._dirty:
                        waits.append(remaining)
                    self._condition.wait(min(waits) if waits else None)
                    continue
                provider = self._resource_targets if resource_due else None
                if resource_due:
                    self._next_resources = now + self._resource_interval
            # /proc reads and process.poll() never run on the acquisition thread
            # or under its publisher lock. No additional resource thread exists.
            if provider is not None:
                try:
                    resources = self._sampler.sample(provider())
                except Exception as error:
                    resources = {'sampled_at_utc': self.utcnow().isoformat(),
                                 'sampled_monotonic': self.clock(), 'error': str(error)}
                with self._condition:
                    self.resources = resources
                    self._dirty = True
            with self._condition:
                if not self._dirty:
                    continue
                sample_key = self._sample_key()
                if (last is not None and self.clock()-last < 1 and not self._closing
                        and sample_key == self._published_sample):
                    continue
                snapshot = dict(self.state, sample=self.sample,
                                timing=self.sample.get('timing') if self.sample else None,
                                history=list(self.history), pipeline_history=list(self.pipeline_history),
                                resources=self.resources, boot_id=self.boot_id,
                                heartbeat_monotonic=self.heartbeat_monotonic,
                                heartbeat_at_utc=self.heartbeat_at_utc)
                self._dirty = False
            try:
                snapshot['radio'] = radio_summary(snapshot['sample']) if snapshot['sample'] else None
                snapshot['published_monotonic'] = self.clock()
                snapshot['updated_at'] = self.utcnow().isoformat()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(snapshot, allow_nan=False, separators=(',', ':')))
                temporary.replace(self.path)
            except (OSError, ValueError, TypeError) as error:
                if last_error is None or self.clock() - last_error > 60:
                    logging.getLogger(__name__).warning('Web snapshot unavailable: %s', error)
                    last_error = self.clock()
            else:
                self._published_sample = sample_key
            last = self.clock()

    def close(self, timeout=2):
        with self._condition:
            self._closing = True
            self._condition.notify()
        self._worker.join(timeout)
        return not self._worker.is_alive()


class OptionalWeb:
    def __init__(self, settings, config_path, status_path=STATUS_PATH):
        self.process = None
        if settings.web.enabled:
            root = Path(__file__).resolve().parents[2]
            self.process = subprocess.Popen([
                sys.executable, '-m', 'modules.web',
                '--config', str(Path(config_path).resolve()),
                '--status', str(Path(status_path).resolve()),
            ], cwd=root)

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
