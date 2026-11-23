"""Debian version ordering, tag mapping and installed-version detection."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from modules.runtime import version


class ComparisonTests(unittest.TestCase):
    def assertOrder(self, first, operator, second):
        result = version.compare_versions(first, second)
        expected = {'<': -1, '=': 0, '>': 1}[operator]
        self.assertEqual(result, expected, f'{first} {operator} {second}')

    def test_the_packaging_vectors_from_the_plan(self):
        self.assertOrder('1.2.0+1.g1bfa441-1', '>', '1.2.0-1')
        self.assertOrder('1.3.0~rc1-1', '<', '1.3.0-1')
        self.assertOrder('1:0.1-1', '>', '9.9-1')
        self.assertOrder('1.2.0-2', '>', '1.2.0-1')
        self.assertOrder('1.10.0-1', '>', '1.9.0-1')

    def test_tilde_sorts_before_everything_and_letters_before_other_characters(self):
        self.assertOrder('1.0~rc1-1', '<', '1.0~rc2-1')
        self.assertOrder('1.0~rc2-1', '<', '1.0-1')
        self.assertOrder('1.0a-1', '<', '1.0+-1')
        self.assertOrder('1.0~-1', '<', '1.0-1')

    def test_a_missing_revision_compares_like_zero(self):
        self.assertOrder('1.2.0', '=', '1.2.0-0')
        self.assertOrder('1.2.0', '<', '1.2.0-1')

    def test_numbers_compare_as_numbers_without_leading_zeroes(self):
        self.assertOrder('1.02.0-1', '=', '1.2.0-1')
        self.assertOrder('1.2-1', '=', '1.2-1')

    def test_digits_compare_before_letters_and_punctuation_like_dpkg(self):
        # dpkg's order() weighs a digit as 0 when the other side is not a digit.
        self.assertOrder('1.0', '<', '1.a')
        self.assertOrder('1.0+g1', '>', '1.0+1')
        self.assertOrder('1.2.0+1.g1bfa441-1', '<', '1.2.0+1.gabc1234-1')

    @unittest.skipUnless(shutil.which('dpkg'), 'dpkg is required to cross-check ordering')
    def test_ordering_agrees_with_dpkg(self):
        vectors = [
            ('1.2.0+1.g1bfa441-1', '1.2.0-1'), ('1.3.0~rc1-1', '1.3.0-1'),
            ('1:0.1-1', '9.9-1'), ('1.2.0-2', '1.2.0-1'), ('1.10.0-1', '1.9.0-1'),
            ('1.0', '1.a'), ('1.0+g1', '1.0+1'), ('1.2.0+1.g1bfa441-1', '1.2.0+1.gabc1234-1'),
            ('1.0a-1', '1.0+-1'), ('1.0~rc1-1', '1.0-1'), ('1.02.0-1', '1.2.0-1'),
            ('2.0~~', '2.0~'), ('2.0~', '2.0'), ('1.0-0', '1.0'),
        ]
        relations = {-1: 'lt', 0: 'eq', 1: 'gt'}
        for first, second in vectors:
            for relation in ('lt', 'eq', 'gt'):
                expected = next(value for value, name in relations.items() if name == relation)
                agrees = subprocess.run(
                    ['dpkg', '--compare-versions', first, relation, second],
                    check=False, capture_output=True).returncode == 0
                with self.subTest(first=first, relation=relation, second=second):
                    self.assertEqual(agrees, version.compare_versions(first, second) == expected,
                                     f'{first} {relation} {second}')

    def test_malformed_versions_are_rejected(self):
        for value in ('', ' 1.2.0-1', 'x:1.0-1', '-1', '1.0-'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    version.compare_versions(value, '1.0-1')
        with self.assertRaises(TypeError):
            version.compare_versions(None, '1.0-1')


class TagTests(unittest.TestCase):
    def test_release_candidates_map_to_tildes_and_releases_stay_plain(self):
        self.assertEqual(version.tag_to_upstream('v1.3.0-rc1'), '1.3.0~rc1')
        self.assertEqual(version.tag_to_upstream('v2.0.0-rc12'), '2.0.0~rc12')
        self.assertEqual(version.tag_to_upstream('v1.2.0'), '1.2.0')
        self.assertEqual(version.tag_to_upstream('1.2.0'), '1.2.0')

    def test_a_tag_that_is_not_a_version_is_rejected(self):
        for tag in ('', 'v', 'release', 'v 1.0', 'v1.0:2'):
            with self.subTest(tag=tag):
                with self.assertRaises(ValueError):
                    version.tag_to_upstream(tag)
        with self.assertRaises(TypeError):
            version.tag_to_upstream(None)

    def test_git_describe_output_maps_exactly_like_build_deb_sh(self):
        self.assertEqual(version.describe_to_version('v1.2.0-0-g1bfa441'), '1.2.0-1')
        self.assertEqual(version.describe_to_version('v1.2.0-1-g1bfa441'), '1.2.0+1.g1bfa441-1')
        self.assertEqual(version.describe_to_version('v1.3.0-rc1-0-gabc1234'), '1.3.0~rc1-1')
        self.assertEqual(version.describe_to_version('v1.3.0-rc1-2-gabc1234'),
                         '1.3.0~rc1+2.gabc1234-1')
        self.assertEqual(version.describe_to_version('v1.2.0-0-g1bfa441-dirty'), '1.2.0.dirty-1')
        self.assertEqual(version.describe_to_version('v1.2.0-1-g1bfa441-dirty', revision='2'),
                         '1.2.0+1.g1bfa441.dirty-2')


class InstalledVersionTests(unittest.TestCase):
    def setUp(self):
        version.reset_cache()
        self.addCleanup(version.reset_cache)
        self.addCleanup(setattr, version, 'APPLICATION_ROOT', version.APPLICATION_ROOT)

    def test_the_VERSION_file_beside_the_entry_point_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'VERSION').write_text('1.2.0+10.g1182bbc-1\n')
            version.APPLICATION_ROOT = root
            with (patch.object(version, '_dpkg_query', side_effect=AssertionError('dpkg consulted')),
                  patch.object(version, '_git_describe', side_effect=AssertionError('git consulted'))):
                self.assertEqual(version.installed_version(), '1.2.0+10.g1182bbc-1')

    def test_the_dpkg_fallback_answers_for_an_installed_tree_without_a_VERSION_file(self):
        version.APPLICATION_ROOT = Path('/opt/qmi-supervisor')
        with (patch.object(version, '_read_version_file', return_value=None),
              patch.object(version, '_dpkg_query', return_value='1.2.0-2') as query,
              patch.object(version, '_git_describe', side_effect=AssertionError('git consulted'))):
            self.assertEqual(version.installed_version(), '1.2.0-2')
        query.assert_called_once_with()

    @unittest.skipUnless(shutil.which('git'), 'git is required for the real describe test')
    def test_git_describe_reads_a_real_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(*arguments):
                return subprocess.run(
                    ['git', '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                     *arguments], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

            run('init', '-q')
            (root / 'file').write_text('x')
            run('add', 'file')
            run('commit', '-qm', 'one')
            run('tag', 'v1.2.0')
            self.assertRegex(version._git_describe(root), r'^v1\.2\.0-0-g[0-9a-f]+$')
            (root / 'file').write_text('y')
            run('commit', '-qam', 'two')
            self.assertRegex(version._git_describe(root), r'^v1\.2\.0-1-g[0-9a-f]+$')

    def test_the_git_fallback_describes_a_development_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            version.APPLICATION_ROOT = Path(directory)
            with patch.object(version, '_git_describe',
                              return_value='v1.3.0-rc1-2-gabc1234-dirty') as describe:
                self.assertEqual(version.installed_version(), '1.3.0~rc1+2.gabc1234.dirty-1')
            describe.assert_called_once_with(Path(directory))

    def test_unknown_when_no_source_can_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            version.APPLICATION_ROOT = Path(directory)
            with patch.object(version, '_git_describe', return_value=None):
                self.assertEqual(version.installed_version(), 'unknown')

    def test_the_result_is_cached_for_the_process_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'VERSION').write_text('1.0.0-1\n')
            version.APPLICATION_ROOT = root
            with patch.object(version, '_detect', wraps=version._detect) as detect:
                self.assertEqual(version.installed_version(), '1.0.0-1')
                self.assertEqual(version.installed_version(), '1.0.0-1')
            self.assertEqual(detect.call_count, 1)
            version.reset_cache()
            with patch.object(version, '_detect', wraps=version._detect) as detect:
                self.assertEqual(version.installed_version(), '1.0.0-1')
            self.assertEqual(detect.call_count, 1)


if __name__ == '__main__':
    unittest.main()
