"""Per-group QMI query scheduling; deliberately independent of GI and hardware.

USB modems answer QMI requests one at a time (one interrupt-endpoint poll per
response), so acquisition time grows with the number of requests per sample.
Groups that change slowly can therefore be refreshed less often than radio
measurements. A group that is not queried in a sample remains null there.
"""

# Request keys reported by session.collect(), in the order they are issued.
GROUPS = {'serving': 'nas.get_serving_system', 'strength': 'nas.get_signal_strength',
          'signal': 'nas.get_signal_info', 'home': 'nas.get_home_network',
          'cell': 'nas.get_cell_location_info', 'bands': 'nas.get_rf_band_information',
          'txrx': 'nas.txrx', 'wds_stats': 'wds.statistics', 'wds_status': 'wds.status',
          'wds_rates': 'wds.rates'}
# Fixed profiles keep their historical request sets: every listed group, every sample.
PRESETS = {'fast': {'strength', 'signal', 'wds_stats', 'wds_status'},
           'standard': set(GROUPS) - {'home', 'txrx'},
           'full': set(GROUPS)}


def periods(telemetry):
    """Refresh period in seconds for each group; 0 disables a group.

    A period shorter than the sampling interval means every sample.
    """
    if telemetry.profile == 'custom':
        values = {group: getattr(telemetry, 'query_' + group) for group in GROUPS}
        return {group: max(value, telemetry.interval) if value > 0 else 0 for group, value in values.items()}
    return {group: telemetry.interval if group in PRESETS[telemetry.profile] else 0 for group in GROUPS}


class QueryPlan:
    def __init__(self):
        self.reset()

    def reset(self):
        self.next_due, self.last_observed, self.active = {}, {}, {}

    def due(self, telemetry, now):
        """Groups to request in the sample starting at monotonic time `now`."""
        selected = set()
        # Half an interval of tolerance absorbs scheduling jitter, so a period
        # behaves as the nearest whole number of sampling intervals.
        tolerance = telemetry.interval / 2
        slow = 0
        for group, period in periods(telemetry).items():
            if period <= 0:
                self.next_due.pop(group, None)
                self.active.pop(group, None)
                continue
            if self.active.get(group) != period:
                # Stagger slow groups so they do not all join the same sample.
                offset = 0
                if period > telemetry.interval:
                    offset = (slow * telemetry.interval) % period
                    slow += 1
                self.active[group] = period
                self.next_due[group] = now + offset
            elif period > telemetry.interval:
                slow += 1
            if self.next_due[group] <= now + tolerance:
                selected.add(group)
        return selected

    def completed(self, groups, records, now):
        """Advance groups whose request was actually sent; retry the others next sample."""
        for group in groups:
            record = records.get(GROUPS[group])
            if record is None or record.get('status') == 'not_requested':
                continue
            self.next_due[group] = now + self.active.get(group, 0)
            if record.get('status') == 'ok':
                self.last_observed[group] = record.get('received_at_utc')

    def report(self, queried):
        return {group: {'interval_s': period, 'queried': group in queried,
                        'last_observed_at_utc': self.last_observed.get(group)}
                for group, period in self.active.items()}

    def period(self, group):
        return self.active.get(group, 0)
