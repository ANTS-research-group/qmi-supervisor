"""Compact acquisition history, independent of heartbeat/publication cadence."""
from collections import deque
import math


def sample_point(payload):
    values = payload['dynamic']
    timing = payload.get('timing', {})
    point = dict(sample_id=payload['log_id'],
                 time=timing.get('acquired_start_utc', payload.get('end_datetime')),
                 monotonic=timing.get('acquired_start_monotonic'),
                 lag_ms=timing.get('schedule_lag_ms'),
                 device_generation=payload.get('device_generation'))
    for field in ('acquisition_duration_ms', 'schedule_lag_ms', 'effective_interval_ms',
                  'skipped_intervals', 'run_id'):
        point[field] = timing.get(field)
    for key, nr, lte in (
        ('rsrp', 'nas_value_5g_signal_strength_rsrp', 'nas_value_lte_rsrp'),
        ('rsrq', 'nas_value_5g_signal_strength_extended', 'nas_value_lte_rsrq'),
        ('snr', 'nas_value_5g_signal_strength_snr', 'nas_value_lte_snr'),
    ):
        value, rat = values.get(nr), 'NR'
        if value is None:
            value, rat = values.get(lte), 'LTE'
        point[key], point[key+'_rat'] = value, rat if value is not None else None
    point['rat'] = point['rsrp_rat']
    traffic = payload.get('collection', {}).get('interface_traffic') or {}
    for field in ('throughput_download_speed_kbps', 'throughput_upload_speed_kbps'):
        value = values.get(field)
        available = not traffic or (traffic.get('enabled') and traffic.get('status') == 'ok')
        valid = type(value) in {int, float} and value >= 0
        if valid:
            try:
                valid = math.isfinite(value)
            except OverflowError:
                valid = False
        point[field] = value if available and valid else None
    point.update(traffic_rate_unit='Kibit/s', traffic_status=traffic.get('status'),
                 traffic_enabled=traffic.get('enabled'), traffic_interface=traffic.get('interface'),
                 traffic_observed_at_utc=traffic.get('observed_at_utc'),
                 traffic_observed_monotonic=traffic.get('observed_monotonic'),
                 traffic_interval_seconds=traffic.get('interval_seconds'))
    return point


class AcquisitionHistory:
    """Retain each actual sample; a latest-only IPC file can then recover gaps."""
    def __init__(self, web):
        self.points = deque(maxlen=web.history_size)
        self.enabled = web.enabled
        self.epoch = None

    def configure(self, web):
        if self.points.maxlen != web.history_size:
            self.points = deque(self.points, maxlen=web.history_size)
        self.enabled = web.enabled
        if not self.enabled:
            self.points.clear()

    def append(self, payload):
        if not self.enabled:
            return
        epoch = (payload.get('device_identity'), payload.get('device_generation'),
                 payload.get('timing', {}).get('run_id'))
        if self.epoch != epoch:
            self.points.clear()
            self.epoch = epoch
        self.points.append(sample_point(payload))

    def snapshot(self):
        return list(self.points)
