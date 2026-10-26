#!/usr/bin/env python3
"""Summarise per-request QMI timing from dynamic CSV captures.

Reads query_timing_json from the last N samples of each CSV and reports how
long acquisition takes, how requests are serialised by the modem (gap between
consecutive responses) and how often each query group was requested. Read-only.
"""
import argparse
from collections import deque
import csv
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys

csv.field_size_limit(sys.maxsize)


def quantile(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, int(fraction * len(values)))] if values else None


def milliseconds(stamp):
    return datetime.fromisoformat(stamp).timestamp() * 1000


def summarize(rows):
    acquisition, windows, gaps, requested = [], [], [], {}
    durations, order = {}, {}
    for row in rows:
        acquisition.append(float(row['acquisition_duration_ms']))
        timing = json.loads(row['query_timing_json'])
        requests = {key: value for key, value in timing.items() if isinstance(value, dict)
                    and value.get('sent_at_utc') and value.get('received_at_utc')}
        for key, value in requests.items():
            durations.setdefault(key, []).append(value['duration_ms'])
            requested[key] = requested.get(key, 0) + 1
        if not requests:
            continue
        sent = min(milliseconds(value['sent_at_utc']) for value in requests.values())
        received = sorted((milliseconds(value['received_at_utc']), key) for key, value in requests.items())
        windows.append(received[-1][0] - sent)
        gaps.extend(later[0] - earlier[0] for earlier, later in zip(received, received[1:]))
        for position, (_, key) in enumerate(received, 1):
            order.setdefault(key, []).append(position)
    queries = {key: dict(samples=requested[key], p50_ms=quantile(values, .5), p95_ms=quantile(values, .95),
                         max_ms=max(values), mean_arrival=statistics.mean(order.get(key, [0])))
               for key, values in durations.items()}
    return dict(samples=len(rows), acquisition_p50_ms=quantile(acquisition, .5),
                acquisition_p95_ms=quantile(acquisition, .95),
                acquisition_max_ms=max(acquisition) if acquisition else None,
                query_window_p50_ms=quantile(windows, .5), response_gap_p50_ms=quantile(gaps, .5),
                queries=dict(sorted(queries.items(), key=lambda item: item[1]['mean_arrival'])))


def read_tail(path, count):
    with open(path, newline='', encoding='utf-8') as stream:
        rows = deque((row for row in csv.DictReader(stream) if row.get('query_timing_json')), maxlen=count)
    return list(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', nargs='+', type=Path, help='dynamic_stats_v2_*.csv files')
    parser.add_argument('--samples', type=int, default=600, help='Most recent samples per file (default: 600)')
    parser.add_argument('--json', action='store_true', help='Print machine-readable JSON')
    args = parser.parse_args(argv)
    reports = {str(path): summarize(read_tail(path, args.samples)) for path in args.csv}
    if args.json:
        print(json.dumps(reports, indent=2))
        return 0
    for path, report in reports.items():
        print(f"== {path}: {report['samples']} samples")
        if not report['samples']:
            continue
        print(f"acquisition p50/p95/max: {report['acquisition_p50_ms']:.1f} / {report['acquisition_p95_ms']:.1f} / "
              f"{report['acquisition_max_ms']:.1f} ms; response gap p50: {report['response_gap_p50_ms'] or 0:.1f} ms")
        print(f"{'request':32} {'samples':>8} {'p50':>7} {'p95':>7} {'max':>7} {'arrival':>8}")
        for key, query in report['queries'].items():
            print(f"{key:32} {query['samples']:8} {query['p50_ms']:7.1f} {query['p95_ms']:7.1f} "
                  f"{query['max_ms']:7.1f} {query['mean_arrival']:8.2f}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
