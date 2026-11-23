"""The append-only update history: bounds, rotation, cleaning and enum checks."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from modules.update import history
from modules.update.state import UpdatePaths

MOMENT = '2026-11-21T03:16:40+00:00'


def record(**overrides):
    fields = {
        'id': '9b2c5d0e4f5a4c1b8a7e6d5c4b3a2910',
        'request_id': None,
        'started_at_utc': MOMENT,
        'at_utc': MOMENT,
        'duration_s': 398,
        'from': '1.3.0-1',
        'to': '1.4.0-1',
        'reason': 'manual',
        'result': 'success',
        'step_failed': None,
        'code': None,
        'detail': None,
        'rollback_available': True,
        'installed_after': '1.4.0-1',
        'config_restored': False,
        'signed_by': None,
    }
    fields.update(overrides)
    return history.make_record(**fields)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = UpdatePaths(state_dir=Path(temporary.name) / 'update')
        self.paths.state_dir.mkdir()

    def lines(self):
        return self.paths.history.read_text().splitlines()

    def test_append_is_one_line_0644_even_under_a_restrictive_umask_and_rotates(self):
        previous = os.umask(0o077)
        try:
            history.append(self.paths, record())
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.paths.history.stat().st_mode), 0o644)
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(json.loads(self.lines()[0])['schema_version'], 1)
        for _ in range(150):
            # Bypass make_record: the line itself must trigger the rotation check.
            line = dict(record())
            line['detail'] = None
            line['from'] = '1.3.0-1' + 'x' * 1500
            history.append(self.paths, line)
        self.assertTrue(self.paths.history_rotated.exists())
        self.assertFalse(self.paths.history_rotated.with_name('history.jsonl.2').exists())
        self.assertEqual({item.name for item in self.paths.state_dir.iterdir()},
                         {'history.jsonl', 'history.jsonl.1'})
        self.assertLessEqual(len(self.lines()[-1].encode()), history.LINE_MAX_BYTES)
        self.assertLessEqual(self.paths.history.stat().st_size, history.MAX_BYTES)

    def test_an_oversized_line_nulls_the_detail(self):
        oversized = dict(record(), detail='x' * 4000)
        history.append(self.paths, oversized)
        line = self.lines()[0]
        self.assertLessEqual(len(line.encode()), history.LINE_MAX_BYTES)
        self.assertIsNone(json.loads(line)['detail'])

    def test_read_returns_the_last_lines_oldest_first_across_the_rotation(self):
        self.paths.history_rotated.write_text(
            json.dumps(record(to='1.1.0-1', detail='old')) + '\n'
            + 'not json\n'
            + json.dumps(['a list is ignored']) + '\n')
        history.append(self.paths, record(to='1.2.0-1', detail='current'))
        values = history.read(self.paths, limit=10)
        self.assertEqual([value['to'] for value in values], ['1.1.0-1', '1.2.0-1'])
        self.assertEqual([value['detail'] for value in values], ['old', 'current'])
        self.assertEqual(len(history.read(self.paths, limit=1)), 1)
        self.assertEqual(history.read(self.paths, limit=1)[0]['to'], '1.2.0-1')


class CleanDetailTests(unittest.TestCase):
    def test_urls_long_numbers_secrets_and_whitespace(self):
        self.assertIsNone(history.clean_detail(None))
        self.assertEqual(history.clean_detail('a\n\t b'), 'a b')
        self.assertEqual(history.clean_detail('see https://example.invalid/x?y=1 now'),
                         'see <url> now')
        self.assertEqual(history.clean_detail('imei 356938035643809'), 'imei <number>')
        self.assertEqual(history.clean_detail('id 1234567890123'), 'id 1234567890123')
        self.assertEqual(history.clean_detail('pbkdf2_sha256$600000$abc$def'),
                         history.WITHHELD)
        for text in ('Password failed', 'the TOKEN expired', 'Authorization missing'):
            self.assertEqual(history.clean_detail(text), history.WITHHELD)

    def test_a_multibyte_detail_is_cut_on_a_character_boundary(self):
        value = history.clean_detail('é' * 200)
        self.assertEqual(value, 'é' * 128)
        self.assertLessEqual(len(value.encode('utf-8')), history.DETAIL_MAX_BYTES)


class MakeRecordTests(unittest.TestCase):
    def test_enums_are_checked_and_the_schema_version_is_added(self):
        value = record()
        self.assertEqual(value['schema_version'], 1)
        for field, bad in (('reason', 'sometimes'), ('result', 'maybe'),
                           ('step_failed', 'unknown_step'), ('code', 'unknown_code')):
            with self.assertRaises(ValueError, msg=field):
                record(**{field: bad})
        self.assertEqual(record(step_failed='health', code='health_failed')['code'],
                         'health_failed')

    def test_the_contract_enums_match_the_schema(self):
        schema = json.loads((Path(__file__).resolve().parents[2] / 'doc' / 'schemas'
                             / 'update-history.schema.json').read_text())
        self.assertEqual(set(schema['properties']['code']['enum']), set(history.CODES) | {None})
        self.assertEqual(set(schema['properties']['step_failed']['enum']),
                         set(history.STEPS) | {None})
        self.assertEqual(set(schema['properties']['reason']['enum']), set(history.REASONS))
        self.assertEqual(set(schema['properties']['result']['enum']), set(history.RESULTS))


if __name__ == '__main__':
    unittest.main()
