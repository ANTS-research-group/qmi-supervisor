import json
import unittest

from tools.telemetry.query_timing_report import summarize


def row(acquisition, *responses):
    timing = {key: {'sent_at_utc': '2026-10-23T10:00:00+00:00', 'status': 'ok', 'duration_ms': offset,
                    'received_at_utc': f'2026-10-23T10:00:00.{offset:03d}+00:00'} for key, offset in responses}
    timing['schedule'] = {'signal': {'interval_s': 1.0, 'queried': True, 'last_observed_at_utc': None}}
    return {'acquisition_duration_ms': str(acquisition), 'query_timing_json': json.dumps(timing)}


class QueryTimingReportTests(unittest.TestCase):
    def test_serialized_responses_report_gap_order_and_request_counts(self):
        report = summarize([row(90, ('nas.a', 24), ('nas.b', 56), ('wds.c', 88)), row(30, ('nas.a', 24))])
        self.assertEqual(report['samples'], 2)
        self.assertEqual(report['response_gap_p50_ms'], 32)
        self.assertEqual(list(report['queries']), ['nas.a', 'nas.b', 'wds.c'])
        self.assertEqual(report['queries']['nas.a']['samples'], 2)
        self.assertEqual(report['queries']['wds.c']['mean_arrival'], 3)


if __name__ == '__main__':
    unittest.main()
