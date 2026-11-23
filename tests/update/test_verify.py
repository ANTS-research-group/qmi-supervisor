"""Checksum, download, signature and package verification."""
from datetime import datetime, timezone
import hashlib
import http.client
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error

from modules.update import commands, files, verify
from modules.update.errors import UpdateError
from modules.update.state import UpdatePaths
from modules.update.transport import TEST_SOURCE_ENV
from tests.update.fakes import FakeClock, FakeOpener, FakeUpdateHost, TEST_FINGERPRINT

NOW = datetime(2026, 11, 21, 3, 7, 41, tzinfo=timezone.utc)
DIGEST = hashlib.sha256(b'package bytes').hexdigest()


def sums_text(*lines):
    return ''.join(line + '\n' for line in lines)


class ParseSumsTests(unittest.TestCase):
    def test_two_space_and_star_lines_are_accepted(self):
        entries = verify.parse_sums(sums_text(f'{DIGEST}  a.deb', f'{DIGEST.upper()} *b.deb', ''))
        self.assertEqual(entries, {'a.deb': DIGEST, 'b.deb': DIGEST})

    def test_a_malformed_line_is_refused(self):
        for text in ('not a checksum line\n', f'{DIGEST} a.deb\nnonsense\n',
                     f'{DIGEST}  a.deb extra\n'):
            with self.assertRaises(UpdateError) as caught:
                verify.parse_sums(text)
            self.assertEqual((caught.exception.step, caught.exception.code),
                             ('verify', 'sums_invalid'))

    def test_a_duplicate_name_is_refused(self):
        with self.assertRaises(UpdateError) as caught:
            verify.parse_sums(sums_text(f'{DIGEST}  a.deb', f'{DIGEST} *a.deb'))
        self.assertEqual(caught.exception.code, 'sums_invalid')

    def test_an_oversized_or_binary_file_is_refused(self):
        for value in (b'x' * (verify.SUMS_MAX_BYTES + 1), b'\xff\xfe' * 10):
            with self.assertRaises(UpdateError) as caught:
                verify.parse_sums(value)
            self.assertEqual(caught.exception.code, 'sums_invalid')


class Sha256Tests(unittest.TestCase):
    def test_hash_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'package.deb'
            path.write_bytes(b'package bytes')
            self.assertEqual(verify.sha256_file(path), DIGEST)
            link = root / 'link.deb'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                verify.sha256_file(link)


class SignatureFingerprintTests(unittest.TestCase):
    def status(self, keyword, *rest):
        return '[GNUPG:] ' + ' '.join((keyword,) + rest) + '\n'

    def valid(self):
        return self.status('VALIDSIG', TEST_FINGERPRINT, '2026-11-20', '1234', '0', '4', '0', '1',
                           '10', '00', TEST_FINGERPRINT)

    def test_every_gpgv_status_mode(self):
        good = self.status('GOODSIG', 'ABCD', 'key') + self.valid()
        self.assertEqual(verify.signature_fingerprint(0, good), TEST_FINGERPRINT)
        self.assertIsNone(verify.signature_fingerprint(1, self.status('BADSIG', 'ABCD', 'key')))
        self.assertIsNone(verify.signature_fingerprint(
            2, self.status('ERRSIG', 'ABCD', '1', '', '0', '1', '') + self.status('NO_PUBKEY', 'ABCD')))
        self.assertIsNone(verify.signature_fingerprint(
            0, self.status('GOODSIG', 'ABCD', 'key') + self.status('EXPKEYSIG', 'ABCD', 'key')
            + self.valid()))
        self.assertIsNone(verify.signature_fingerprint(2, self.status('NODATA', '1')))

    def test_exit_zero_alone_is_not_enough_and_the_fingerprint_is_checked(self):
        self.assertIsNone(verify.signature_fingerprint(0, ''))
        no_goods = self.valid()
        self.assertIsNone(verify.signature_fingerprint(0, no_goods))
        short = self.status('GOODSIG', 'ABCD', 'key') + self.status(
            'VALIDSIG', 'ABCD', '2026-11-20', '1', '0', '4', '0', '1', '10', '00', 'ABCD')
        self.assertIsNone(verify.signature_fingerprint(0, short))
        lowered = (self.status('GOODSIG', 'abcd', 'key') + self.status(
            'VALIDSIG', TEST_FINGERPRINT.lower(), '2026-11-20', '1', '0', '4', '0', '1',
            '10', '00', TEST_FINGERPRINT.lower()))
        self.assertEqual(verify.signature_fingerprint(0, lowered), TEST_FINGERPRINT)

    def test_the_fake_host_modes_reach_verify_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = FakeClock(NOW)
            host = FakeUpdateHost(root / 'run', clock)
            keyring = root / 'keyring.gpg'
            keyring.write_bytes(b'binary keyring')
            sums, signature = root / 'SHA256SUMS', root / 'SHA256SUMS.asc'
            sums.write_text('data')
            signature.write_text('signature')
            self.assertEqual(verify.verify_signature(sums, signature, keyring,
                                                     runner=host, workdir=str(root)),
                             TEST_FINGERPRINT)
            for mode in ('bad', 'nokey', 'expired'):
                host.gpgv = mode
                with self.assertRaises(UpdateError) as caught:
                    verify.verify_signature(sums, signature, keyring, runner=host, workdir=str(root))
                self.assertEqual(caught.exception.code, 'bad_signature', mode)

    def test_the_gpgv_argv_is_exact_and_the_empty_home_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = FakeClock(NOW)
            host = FakeUpdateHost(root / 'run', clock)
            keyring = root / 'release.gpg'
            keyring.write_bytes(b'binary keyring')
            sums, signature = root / 'SHA256SUMS', root / 'SHA256SUMS.asc'
            sums.write_text('data')
            signature.write_text('signature')
            verify.verify_signature(sums, signature, keyring, runner=host, workdir=str(root))
            argv = next(call for call in host.calls if call[0] == 'gpgv')
            self.assertEqual(argv[1], '--homedir')
            home = Path(argv[2])
            self.assertFalse(home.exists(), 'the empty gpg home must be removed')
            self.assertEqual(argv[3:5], ['--status-fd', '1'])
            self.assertEqual(argv[5], '--keyring')
            self.assertEqual(Path(argv[6]), keyring.resolve())
            self.assertEqual(Path(argv[7]), signature)
            self.assertEqual(Path(argv[8]), sums)

    def test_a_missing_keyring_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(UpdateError) as caught:
                verify.verify_signature(root / 's', root / 'sig', root / 'missing.gpg',
                                        runner=lambda *a, **k: None, workdir=str(root))
            self.assertEqual((caught.exception.step, caught.exception.code),
                             ('verify', 'keyring_missing'))


class KeyringPathTests(unittest.TestCase):
    def setUp(self):
        verify._warned_test_keyring = False
        self.paths = UpdatePaths(keyring=Path('/opt/qmi-supervisor/keys/release-signing.gpg'))

    def test_the_shipped_keyring_is_the_default(self):
        with mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop(verify.TEST_KEYRING_ENV, None)
            os.environ.pop(TEST_SOURCE_ENV, None)
            self.assertEqual(verify.keyring_path(self.paths), self.paths.keyring)

    def test_the_test_keyring_is_honoured_only_in_test_mode_and_only_absolute(self):
        value = '/tmp/local-test-keyring.gpg'
        with mock.patch.dict('os.environ', {verify.TEST_KEYRING_ENV: value}, clear=False):
            import os
            os.environ[verify.TEST_KEYRING_ENV] = value
            os.environ.pop(TEST_SOURCE_ENV, None)
            self.assertEqual(verify.keyring_path(self.paths), self.paths.keyring)
            os.environ[TEST_SOURCE_ENV] = '1'
            with self.assertLogs('modules.update.verify', level='WARNING') as logs:
                self.assertEqual(verify.keyring_path(self.paths), Path(value))
            self.assertIn('TEST signing keyring', logs.output[0])
            os.environ[verify.TEST_KEYRING_ENV] = 'relative.gpg'
            self.assertEqual(verify.keyring_path(self.paths), self.paths.keyring)


class DownloadTests(unittest.TestCase):
    URL = 'https://example.invalid/package.deb'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = FakeClock(NOW)

    def run_download(self, opener, *, max_bytes=verify.DOWNLOAD_MAX_BYTES, expected_size=None,
                     name='package.deb'):
        return verify.download(opener, self.URL, self.root / name, step='download', name=name,
                               max_bytes=max_bytes, expected_size=expected_size, clock=self.clock.monotonic)

    def test_success_writes_atomically_and_hashes_while_copying(self):
        data = b'package bytes'
        opener = FakeOpener({self.URL: data})
        (self.root / '.package.deb.part').write_bytes(b'leftover')
        digest, size = self.run_download(opener, expected_size=len(data))
        self.assertEqual((digest, size), (DIGEST, len(data)))
        self.assertEqual((self.root / 'package.deb').read_bytes(), data)
        self.assertFalse((self.root / '.package.deb.part').exists())
        self.assertEqual(opener.urls, [self.URL])
        self.assertEqual(opener.calls[0][1], 'application/octet-stream')

    def test_an_http_error_maps_to_download_failed_without_the_url(self):
        error = urllib.error.HTTPError(self.URL, 404, 'missing', {}, None)
        opener = FakeOpener({self.URL: error})
        with self.assertRaises(UpdateError) as caught:
            self.run_download(opener)
        self.assertEqual((caught.exception.step, caught.exception.code),
                         ('download', 'download_failed'))
        self.assertIn('HTTP 404', caught.exception.detail)
        self.assertNotIn('https://', caught.exception.detail)
        self.assertNotIn(self.URL, caught.exception.detail)
        self.assertFalse((self.root / '.package.deb.part').exists())

    def test_the_size_limits_stop_the_read_early(self):
        opener = FakeOpener({self.URL: b'x' * (1024 * 1024)})
        with self.assertRaises(UpdateError) as caught:
            self.run_download(opener, max_bytes=1024)
        self.assertEqual(caught.exception.code, 'download_too_large')
        self.assertLess(opener.bytes_read, 1024 * 1024)
        opener = FakeOpener({self.URL: b'x' * 100})
        with self.assertRaises(UpdateError) as caught:
            self.run_download(opener, expected_size=99)
        self.assertEqual(caught.exception.code, 'download_too_large')

    def test_a_published_size_mismatch_is_refused(self):
        opener = FakeOpener({self.URL: b'x' * 100})
        with self.assertRaises(UpdateError) as caught:
            self.run_download(opener, expected_size=120)
        self.assertEqual(caught.exception.code, 'download_size_mismatch')
        self.assertFalse((self.root / 'package.deb').exists())

    def test_network_errors_map_to_download_failed(self):
        for error in (urllib.error.URLError('offline'), TimeoutError('slow'),
                      http.client.HTTPException('bad response'), ValueError('bad url')):
            with self.assertRaises(UpdateError) as caught:
                self.run_download(FakeOpener({self.URL: error}))
            self.assertEqual(caught.exception.code, 'download_failed')
            self.assertEqual(caught.exception.detail,
                             f'package.deb: {type(error).__name__}')

    def test_a_symlinked_cache_directory_is_refused(self):
        real = self.root / 'real'
        real.mkdir()
        link = self.root / 'cache'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(UpdateError) as caught:
            files.ensure_private_dir(link, code='cache_unsafe')
        self.assertEqual((caught.exception.step, caught.exception.code),
                         ('download', 'cache_unsafe'))
        self.assertIn('not a private directory', caught.exception.detail)


class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_directories_and_files_get_private_modes(self):
        directory = files.ensure_private_dir(self.root / 'a' / 'cache', code='cache_unsafe')
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        target = directory / 'file'
        files.write_private(target, b'secret')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(files.read_nofollow(target, 100), b'secret')
        self.assertEqual([item.name for item in directory.iterdir()], ['file'])

    def test_a_public_directory_and_an_oversized_or_symlinked_file_are_refused(self):
        public = self.root / 'public'
        public.mkdir(mode=0o755)
        with self.assertRaises(UpdateError):
            files.ensure_private_dir(public, code='backup_failed')
        target = self.root / 'data'
        target.write_bytes(b'x' * 11)
        with self.assertRaises(ValueError):
            files.read_nofollow(target, 10)
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(OSError):
            files.read_nofollow(link, 100)


@unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb is required')
class RealPackageTests(unittest.TestCase):
    """Build tiny packages with the real dpkg-deb and run the real preflight."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workdir = self.root / 'work'
        self.workdir.mkdir()
        self.config = self.root / 'settings.ini'
        self.python = Path('/usr/bin/python3')

    def fake_script(self):
        return (
            'import sys\n'
            'path = sys.argv[sys.argv.index("--config") + 1]\n'
            'with open(path, encoding="utf-8") as stream:\n'
            '    text = stream.read()\n'
            'if "[update]" in text:\n'
            '    print("qmi_supervisor.py: error: Unknown configuration section [update]",\n'
            '          file=sys.stderr)\n'
            '    sys.exit(2)\n'
        )

    def build(self, version, *, include_updater=True):
        package = self.root / f'tree-{version}'
        control = package / 'DEBIAN'
        control.mkdir(parents=True)
        (control / 'control').write_text(
            f'Package: qmi-supervisor\nVersion: {version}\nArchitecture: all\n'
            'Maintainer: tests <tests@invalid>\nDescription: synthetic\n')
        (package / 'opt/qmi-supervisor').mkdir(parents=True)
        (package / 'opt/qmi-supervisor/qmi_supervisor.py').write_text(self.fake_script())
        (package / 'usr/local/sbin').mkdir(parents=True)
        if include_updater:
            (package / 'usr/local/sbin/qmi-supervisor-update').write_text('#!/bin/sh\n')
        (package / 'etc/systemd/system').mkdir(parents=True)
        (package / 'etc/systemd/system/qmi-supervisor-update-install@.service').write_text('[Service]\n')
        (package / 'etc/systemd/system/qmi-supervisor-update-rollback.service').write_text('[Service]\n')
        output = self.root / f'qmi-supervisor_{version}_all.deb'
        subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(package), str(output)],
                       check=True, capture_output=True)
        return output

    def check(self, deb, version, *, preflight=True):
        return verify.check_package(deb, version, runner=commands.run_command,
                                    workdir=self.workdir, python=self.python,
                                    config=self.config, preflight=preflight)

    def test_a_plain_configuration_passes_and_the_preflight_argv_has_no_dash_i(self):
        deb = self.build('1.9.0-1')
        self.config.write_text('[web]\nport = 8080\n')
        calls = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return commands.run_command(args, **kwargs)
        verify.check_package(deb, '1.9.0-1', runner=runner, workdir=self.workdir,
                             python=self.python, config=self.config)
        preflight = [call for call in calls if '--check-config' in call][0]
        self.assertEqual(preflight[:4], [str(self.python), '-B', '-E', '-s'])
        self.assertTrue(preflight[4].endswith('opt/qmi-supervisor/qmi_supervisor.py'))
        self.assertNotIn('-I', preflight)
        self.assertEqual(preflight[-2:], ['--config', str(self.config)])
        self.assertEqual(list(self.workdir.iterdir()), [], 'the extracted tree is removed')

    def test_an_ini_the_target_rejects_is_config_incompatible(self):
        deb = self.build('1.9.0-1')
        self.config.write_text('[update]\nmode = notify\n')
        with self.assertRaises(UpdateError) as caught:
            self.check(deb, '1.9.0-1')
        self.assertEqual(caught.exception.code, 'config_incompatible')
        self.assertIn('Unknown configuration section [update]', caught.exception.detail)
        self.assertNotIn('qmi_supervisor.py: ', caught.exception.detail)

    def test_a_missing_updater_is_package_incomplete(self):
        deb = self.build('1.9.0-1', include_updater=False)
        self.config.write_text('[web]\n')
        with self.assertRaises(UpdateError) as caught:
            self.check(deb, '1.9.0-1')
        self.assertEqual(caught.exception.code, 'package_incomplete')

    def test_another_version_or_package_name_is_package_mismatch(self):
        deb = self.build('1.9.0-1')
        self.config.write_text('[web]\n')
        with self.assertRaises(UpdateError) as caught:
            self.check(deb, '1.9.0-2')
        self.assertEqual(caught.exception.code, 'package_mismatch')
        result = type('R', (), {'returncode': 0, 'stdout': 'Package: other\n'
                                'Version: 1.9.0-1\nArchitecture: all\n', 'stderr': ''})()
        with self.assertRaises(UpdateError) as caught:
            verify.check_package(deb, '1.9.0-1', runner=lambda args, **kwargs: result,
                                 workdir=self.workdir, python=self.python, config=self.config)
        self.assertEqual(caught.exception.code, 'package_mismatch')

    def test_the_preflight_can_be_disabled_for_a_repair(self):
        deb = self.build('1.9.0-1')
        self.config.write_text('[update]\nmode = notify\n')
        self.check(deb, '1.9.0-1', preflight=False)


if __name__ == '__main__':
    unittest.main()
