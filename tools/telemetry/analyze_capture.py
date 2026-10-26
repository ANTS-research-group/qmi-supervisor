#!/usr/bin/env python3
"""Stream CSV v2 captures and compare early/late timing without loading the dataset."""
import argparse
from collections import deque
import csv
import json
import math
from pathlib import Path


class Window:
    def __init__(self, size):
        self.first, self.last = [], deque(maxlen=size)
        self.count = 0
        self.size = size

    def add(self, value):
        if not math.isfinite(value):
            return False
        if len(self.first) < self.size:
            self.first.append(value)
        self.last.append(value)
        self.count += 1
        return True

    def report(self):
        def quantiles(values):
            ordered = sorted(values)
            return {name: ordered[max(0, math.ceil(len(ordered)*fraction)-1)] if ordered else None
                    for name, fraction in [('p50_ms', .5), ('p95_ms', .95), ('p99_ms', .99)]}
        return {'count': self.count, 'window_samples': self.size,
                'first': quantiles(self.first), 'last': quantiles(self.last)}


def analyze(directory, window=1024):
    metrics = {key: Window(window) for key in ('acquisition_duration_ms', 'schedule_lag_ms', 'effective_interval_ms')}
    rows = invalid = omitted = dropped = files = 0
    for path in sorted(Path(directory).rglob('dynamic_stats_v2_*.csv')):
        files += 1
        with path.open(newline='') as stream:
            reader = csv.DictReader(stream)
            for row in reader:
                rows += 1
                for key, metric in metrics.items():
                    if row.get(key):
                        try:
                            if not metric.add(float(row[key])):
                                invalid += 1
                        except ValueError:
                            invalid += 1
                try:
                    omitted += int(row.get('skipped_intervals') or 0)
                    dropped = max(dropped, int(row.get('persistence_dropped_total') or 0))
                except ValueError:
                    invalid += 1
    return {'files': files, 'rows': rows, 'invalid_timing_fields': invalid,
            'recorded_skipped_intervals': omitted, 'max_reported_persistence_drops': dropped,
            'timing': {key: metric.report() for key, metric in metrics.items()},
            'note': 'Only recorded rows are observable. Missing files or a fully blocked disk cannot be reconstructed.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--window', type=int, default=1024)
    args = parser.parse_args()
    if not 1 <= args.window <= 100000:
        parser.error('window must be between 1 and 100000')
    print(json.dumps(analyze(args.directory, args.window), indent=2))


if __name__ == '__main__':
    main()
