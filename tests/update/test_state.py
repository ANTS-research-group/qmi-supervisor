"""State file permissions, validation, lock and derived fields."""
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest

from modules.update.releases import parse_release
from modules.update.state import (
    MAX_STATE_BYTES, ensure_state_dir, load_state, new_state, refresh_derived, state_lock, write_state,
)
from tests.update.fixtures import release_item

NOW = datetime(2026, 11, 21, 3, 7, 41, tzinfo=timezone.utc)


class StateFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_write_state_permissions_round_trip_and_no_temporary_left(self):
        directory = self.root / 'update'
        path = directory / 'state.json'
        document = new_state('1.2.0-1', NOW)
        document['releases'] = [{'notes': 'acentuación'}]
        write_state(path, document, now=NOW)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual([item.name for item in directory.iterdir() if item.name.startswith('.state.json.')], [])
        loaded = load_state(path)
        self.assertEqual(loaded['releases'], [{'notes': 'acentuación'}])
        self.assertEqual(loaded['updated_utc'], '2026-11-21T03:07:41+00:00')

    def test_load_state_rejects_missing_invalid_unknown_and_oversized(self):
        self.assertIsNone(load_state(self.root / 'missing.json'))
        invalid = self.root / 'invalid.json'
        invalid.write_text('not json')
        self.assertIsNone(load_state(invalid))
        unknown = self.root / 'unknown.json'
        unknown.write_text(json.dumps({'schema_version': 2, 'releases': []}))
        self.assertIsNone(load_state(unknown))
        big = self.root / 'big.json'
        big.write_bytes(b'x' * (MAX_STATE_BYTES + 1))
        self.assertIsNone(load_state(big))

    def test_symlinked_state_directory_is_refused(self):
        real = self.root / 'real'
        real.mkdir()
        link = self.root / 'link'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            ensure_state_dir(link)

    def test_state_lock_is_exclusive(self):
        lock = self.root / 'update' / 'update.lock'
        with state_lock(lock) as first:
            self.assertTrue(first)
            with state_lock(lock) as second:
                self.assertFalse(second)
        with state_lock(lock) as again:
            self.assertTrue(again)

    def test_refresh_derived_follows_channel_and_signature_policy(self):
        stable = parse_release(release_item('v1.9.0', published_at='2026-11-01T00:00:00Z'))
        prerelease = parse_release(release_item(
            'v1.10.0-rc1', deb='qmi-supervisor_1.10.0~rc1-1_all.deb', published_at='2026-11-10T00:00:00Z'))
        document = new_state('1.2.0-1', NOW)
        document['releases'] = [stable, prerelease]
        refresh_derived(document, current_version='1.2.0-1', channel='stable', require_signature=True)
        self.assertEqual(document['newest_available'], {'tag': 'v1.9.0', 'version': '1.9.0-1'})
        self.assertTrue(document['update_available'])
        refresh_derived(document, current_version='1.2.0-1', channel='prerelease', require_signature=True)
        self.assertEqual(document['newest_available']['version'], '1.10.0~rc1-1')

        unsigned = parse_release(release_item('v2.0.0', signature=False, published_at='2026-11-15T00:00:00Z'))
        document['releases'] = [unsigned]
        refresh_derived(document, current_version='1.2.0-1', channel='stable', require_signature=True)
        self.assertIsNone(document['newest_installable'])
        self.assertEqual(document['newest_available']['version'], '2.0.0-1')
        self.assertFalse(document['update_available'])
        refresh_derived(document, current_version='1.2.0-1', channel='stable', require_signature=False)
        self.assertEqual(document['newest_installable']['version'], '2.0.0-1')
        self.assertTrue(document['update_available'])


if __name__ == '__main__':
    unittest.main()
