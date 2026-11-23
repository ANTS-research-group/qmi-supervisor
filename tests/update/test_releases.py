"""Release parsing: installability reasons, version mapping, bounds and selection."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from modules.update import state
from modules.update.releases import (
    apply_signature_policy, is_newer, newest, parse_release, parse_releases,
)
from tests.update.fixtures import GITHUB_V120, asset_item, release_item

NOW = datetime(2026, 11, 21, 3, 7, 41, tzinfo=timezone.utc)


class ReleaseParsingTests(unittest.TestCase):
    def test_github_v120_is_listed_but_not_installable_without_checksums(self):
        release = parse_release(GITHUB_V120)
        self.assertFalse(release['installable'])
        self.assertEqual(release['reason'], 'no_checksums')
        self.assertEqual(release['version'], '1.2.0')
        self.assertEqual(release['deb']['version'], '1.2.0-1')
        self.assertEqual(release['deb']['sha256'],
                         '4c04b0bf88ea7c87ef80293ef7173f356250e475ff3b78b26c1bd1b4c9ba23c0')
        self.assertEqual(release['published_utc'], '2026-11-05T12:22:41+00:00')
        self.assertIsNone(release['sums'])
        self.assertIsNone(release['signature'])

    def test_signature_policy(self):
        self.assertTrue(parse_release(release_item('v9.9.0'))['installable'])
        unsigned = parse_release(release_item('v9.9.0', signature=False))
        self.assertEqual(unsigned['reason'], 'unsigned')
        self.assertTrue(parse_release(release_item('v9.9.0', signature=False),
                                      require_signature=False)['installable'])
        self.assertEqual(parse_release(release_item('v9.9.0', sums=False),
                                       require_signature=False)['reason'], 'no_checksums')

    def test_release_candidate_tag_and_asset_spellings(self):
        plain = parse_release(release_item('v1.3.0-rc1', deb='qmi-supervisor_1.3.0~rc1-1_all.deb'))
        self.assertEqual(plain['deb']['version'], '1.3.0~rc1-1')
        self.assertTrue(plain['prerelease'])
        self.assertTrue(plain['installable'])
        renamed = parse_release(release_item('v1.3.0-rc1', deb='qmi-supervisor_1.3.0.rc1-1_all.deb'))
        self.assertEqual(renamed['deb']['version'], '1.3.0~rc1-1')
        old = parse_release(release_item('v1.3.0-rc1', deb='qmi-supervisor_1.3.0-rc1-1_all.deb'))
        self.assertEqual(old['reason'], 'version_mismatch')

    def test_package_count_name_and_tag(self):
        two = release_item('v9.9.0', deb=['qmi-supervisor_9.9.0-1_all.deb', 'qmi-supervisor_9.9.0-2_all.deb'])
        self.assertEqual(parse_release(two)['reason'], 'several_packages')
        self.assertEqual(parse_release(release_item('v9.9.0', deb=False))['reason'], 'no_package')
        self.assertEqual(parse_release(release_item('v9.9.0', deb='qmi-supervisor_1.2.0_all.deb'))['reason'],
                         'bad_package_name')
        for tag in ('latest', 'nightly'):
            with self.subTest(tag=tag):
                self.assertEqual(parse_release(release_item(tag))['reason'], 'bad_tag')
        self.assertIsNone(parse_release(release_item('v9.9.0', draft=True)))
        arm = release_item('v9.9.0')
        arm['assets'].append(asset_item('qmi-supervisor_9.9.0-1_arm64.deb'))
        self.assertTrue(parse_release(arm)['installable'])

    def test_asset_readiness_url_and_size(self):
        self.assertEqual(parse_release(release_item('v9.9.0', state='open'))['reason'], 'asset_not_ready')
        self.assertEqual(parse_release(release_item('v9.9.0', size=0))['reason'], 'asset_not_ready')
        item = release_item('v9.9.0')
        for asset in item['assets']:
            asset['browser_download_url'] = 'http://example.com/x'
        self.assertEqual(parse_release(item)['reason'], 'bad_url')
        item = release_item('v9.9.0')
        item['html_url'] = 'javascript:alert(1)'
        self.assertIsNone(parse_release(item)['html_url'])
        self.assertEqual(parse_release(release_item('v9.9.0', size=40 * 1024 * 1024))['reason'],
                         'package_too_large')

    def test_notes_are_plain_text_and_bounded_to_8192_bytes(self):
        body = ('x' * 8180) + '\r\n\x07' + ('ñ' * 50)
        release = parse_release(release_item('v9.9.0', body=body))
        encoded = release['notes'].encode('utf-8')
        self.assertLessEqual(len(encoded), 8192)
        encoded.decode('utf-8')  # still valid UTF-8 after the cut
        self.assertTrue(release['notes_truncated'])
        self.assertNotIn('\r', release['notes'])
        self.assertNotIn('\x07', release['notes'])
        self.assertIn('\n', release['notes'])
        self.assertTrue(all(character in '\n\t' or ord(character) >= 32 for character in release['notes']))

    def test_parse_releases_limits_to_thirty_newest_first(self):
        items = [release_item(f'v1.{index}.0', published_at=f'2026-11-20T10:{index:02d}:00Z')
                 for index in range(35)]
        parsed = parse_releases(items)
        self.assertEqual(len(parsed), 30)
        stamps = [release['published_utc'] for release in parsed]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(parsed[0]['tag'], 'v1.34.0')

    def test_newest_selection_by_channel_and_version(self):
        stable = parse_release(release_item('v1.9.0', published_at='2026-11-01T00:00:00Z'))
        newer_prerelease = parse_release(release_item(
            'v1.10.0-rc1', deb='qmi-supervisor_1.10.0~rc1-1_all.deb', published_at='2026-11-10T00:00:00Z'))
        everything = [stable, newer_prerelease]
        self.assertEqual(newest(everything, 'stable', installable_only=True)['version'], '1.9.0-1')
        self.assertEqual(newest(everything, 'prerelease', installable_only=True)['version'], '1.10.0~rc1-1')
        high = parse_release(release_item('v1.10.0', published_at='2026-11-05T00:00:00Z'))
        low = parse_release(release_item('v1.9.0', published_at='2026-11-01T00:00:00Z'))
        self.assertEqual(newest([low, high], 'stable', installable_only=True)['version'], '1.10.0-1')
        self.assertFalse(is_newer('1.2.0-1', '1.2.0+1.g1bfa441-1'))
        self.assertTrue(is_newer('1.3.0-1', '1.2.0-1'))
        self.assertFalse(is_newer('1.3.0-1', 'unknown'))
        self.assertFalse(is_newer(None, '1.2.0-1'))

    def test_signature_policy_only_touches_the_unsigned_reason(self):
        unsigned = parse_release(release_item('v9.9.0', signature=False))
        relaxed = apply_signature_policy(unsigned, False)
        self.assertIsNone(relaxed['reason'])
        self.assertTrue(relaxed['installable'])
        self.assertEqual(apply_signature_policy(relaxed, True)['reason'], 'unsigned')
        no_sums = parse_release(release_item('v9.9.0', sums=False))
        self.assertEqual(apply_signature_policy(no_sums, False)['reason'], 'no_checksums')


class UntrustedTextTests(unittest.TestCase):
    def test_a_lone_surrogate_never_crashes_and_never_reaches_the_state(self):
        item = release_item('v9.9.0')
        item['body'] = 'notes \ud800 still here'
        item['name'] = 'name \ud800'
        release = parse_release(item)
        self.assertNotIn('\ud800', release['notes'])
        self.assertNotIn('\ud800', release['name'])
        release['notes'].encode('utf-8')
        document = state.new_state('1.2.0-1', NOW)
        document['releases'] = [release]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            state.write_state(path, document, now=NOW)
            self.assertIsNotNone(state.load_state(path))

    def test_an_asset_name_longer_than_the_contract_is_not_stored(self):
        release = parse_release(release_item(
            'v9.9.0', deb='qmi-supervisor_' + '9' * 300 + '-1_all.deb'))
        self.assertEqual(release['reason'], 'no_package')
        self.assertIsNone(release['deb'])

    def test_a_package_version_longer_than_the_contract_is_a_bad_package_name(self):
        release = parse_release(release_item(
            'v9.9.0', deb='qmi-supervisor_9.9.0-' + '1' * 200 + '_all.deb'))
        self.assertEqual(release['reason'], 'bad_package_name')
        self.assertIsNone(release['deb']['version'])


if __name__ == '__main__':
    unittest.main()
