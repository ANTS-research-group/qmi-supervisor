"""Real `gpgv` against the committed signature fixtures (skipped when gpgv is missing)."""
from pathlib import Path
import shutil
import tempfile
import unittest

from modules.update import commands, verify
from modules.update.errors import UpdateError

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'update'


@unittest.skipUnless(FIXTURES.is_dir() and shutil.which('gpgv'), 'gpgv and its fixtures are required')
class SignatureFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workdir = Path(temporary.name)
        self.keyring = FIXTURES / 'test-keyring.gpg'

    def verify(self, signature, sums):
        return verify.verify_signature(sums, signature, self.keyring,
                                       runner=commands.run_command, workdir=str(self.workdir))

    def test_the_committed_checksums_are_signed_by_the_committed_fingerprint(self):
        fingerprint = (FIXTURES / 'fingerprint.txt').read_text().strip()
        self.assertEqual(self.verify(FIXTURES / 'SHA256SUMS.asc', FIXTURES / 'SHA256SUMS'),
                         fingerprint)
        self.assertEqual(verify.parse_sums((FIXTURES / 'SHA256SUMS').read_bytes()),
                         {'qmi-supervisor_9.9.0-1_all.deb':
                          '40a55da8eac78702c9df6a7bb45bf0f826ff5c18ee0520ec64cb1c771b651796'})

    def test_altered_data_another_key_and_a_garbage_signature_are_bad_signatures(self):
        for signature, sums in (
                (FIXTURES / 'SHA256SUMS.asc', FIXTURES / 'SHA256SUMS.tampered'),
                (FIXTURES / 'SHA256SUMS.other.asc', FIXTURES / 'SHA256SUMS'),
        ):
            with self.assertRaises(UpdateError) as caught:
                self.verify(signature, sums)
            self.assertEqual((caught.exception.step, caught.exception.code),
                             ('verify', 'bad_signature'))
        garbage = self.workdir / 'garbage.asc'
        garbage.write_text('this is not a signature\n')
        with self.assertRaises(UpdateError) as caught:
            self.verify(garbage, FIXTURES / 'SHA256SUMS')
        self.assertEqual(caught.exception.code, 'bad_signature')

    def test_the_keyring_must_be_binary_and_the_workdir_is_left_clean(self):
        armored = self.workdir / 'armored.gpg'
        armored.write_text('-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nAAAA\n'
                           '-----END PGP PUBLIC KEY BLOCK-----\n')
        with self.assertRaises(UpdateError) as caught:
            verify.verify_signature(FIXTURES / 'SHA256SUMS', FIXTURES / 'SHA256SUMS.asc',
                                    armored, runner=commands.run_command, workdir=str(self.workdir))
        self.assertEqual(caught.exception.code, 'bad_signature')
        self.assertEqual(list(self.workdir.iterdir()), [armored])


if __name__ == '__main__':
    unittest.main()
