import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.web.auth import (AuthError, AuthManager, hash_password, verify_password,
                              is_password_hash, PBKDF2_ITERATIONS, MAX_SESSIONS, MAX_CLIENTS)


PASSWORD = 'synthetic-password-only'


class PasswordHashTests(unittest.TestCase):
    def test_hash_format_salt_and_password_verification(self):
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)
        algorithm, iterations, salt, digest = first.split('$')
        self.assertEqual((algorithm, iterations), ('pbkdf2_sha256', '600000'))
        self.assertEqual((len(bytes.fromhex(salt)), len(bytes.fromhex(digest))), (16, 32))
        self.assertNotEqual(first, second)
        self.assertTrue(is_password_hash(first))
        self.assertTrue(verify_password(PASSWORD, first))
        self.assertFalse(verify_password(PASSWORD+'wrong', first))

    def test_unicode_and_space_are_exact_without_trimming_or_normalization(self):
        password = ' pässwörd-日本語 '
        encoded = hash_password(password)
        self.assertTrue(verify_password(password, encoded))
        self.assertFalse(verify_password(password.strip(), encoded))
        self.assertFalse(verify_password(password.replace('ä', 'a\u0308'), encoded))

    def test_password_character_and_utf8_byte_limits(self):
        with patch('modules.web.auth.hashlib.pbkdf2_hmac', return_value=b'0'*32):
            self.assertTrue(is_password_hash(hash_password('é'*128)))
            self.assertTrue(is_password_hash(hash_password('a'*256)))
        for password in (None, 123, '', 'short', 'é'*129, 'a'*257, 'abcdefghij\n',
                         'abcdefghij\r', 'abcdefghij\0', 'abcdefghij\t', 'abcdefghij\x7f', '\ud800'*10):
            with self.subTest(type=type(password).__name__):
                with self.assertRaises(ValueError):
                    hash_password(password)
                self.assertFalse(verify_password(password, 'invalid'))

    def test_hash_validation_rejects_unsupported_weak_or_excessive_work_factors(self):
        valid = 'pbkdf2_sha256$600000$'+'00'*16+'$'+'11'*32
        self.assertTrue(is_password_hash(valid))
        for value in (None, b'bad', '', valid+'$', valid.replace('600000', '1'),
                      valid.replace('600000', '599999'), valid.replace('600000', '2000001'),
                      valid.replace('600000', '999999999999999999999999'),
                      valid.replace('pbkdf2_sha256', 'sha256'), valid.replace('00'*16, 'aa'),
                      valid.replace('11'*32, 'not-hex'), valid.upper()):
            self.assertFalse(is_password_hash(value))
            self.assertFalse(verify_password(PASSWORD, value))
        for factor in (1, 599999, 2000001, True):
            with self.assertRaises(ValueError):
                hash_password(PASSWORD, iterations=factor)


class AuthManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password(PASSWORD)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qmi-auth-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'state/sessions.json'
        self.now = 100.0
        self.settings = SimpleNamespace(username='admin', password_hash=self.password_hash,
                                        session_timeout=28800)
        self.manager = self.new_manager()

    def new_manager(self, *, settings=None, boot='test-boot'):
        return AuthManager(settings or self.settings, self.path, clock=lambda: self.now, boot_id=boot)

    def login(self, *, manager=None, client='192.0.2.1'):
        return (manager or self.manager).login('admin', PASSWORD, client)

    def test_successful_session_is_opaque_and_only_digest_is_stored(self):
        session = self.login()
        self.assertEqual(set(session), {'session_id', 'csrf_token', 'username', 'expires_in'})
        self.assertEqual(len(session['session_id']), 43)
        self.assertEqual(len(session['csrf_token']), 43)
        self.assertNotEqual(session['session_id'], session['csrf_token'])
        self.assertEqual(session['expires_in'], 28800)
        info = self.manager.authenticate(session['session_id'])
        self.assertEqual(info, {key: value for key, value in session.items() if key != 'session_id'})
        content = self.path.read_text()
        self.assertNotIn(session['session_id'], content)
        self.assertNotIn(PASSWORD, content)
        self.assertNotIn(self.password_hash, content)
        digest = hashlib.sha256(session['session_id'].encode()).hexdigest()
        self.assertIn(digest, json.loads(content)['sessions'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.path.parent.glob('.sessions.json.*')))

    def test_polling_does_not_write_or_extend_expiration(self):
        session = self.login()
        with patch.object(self.manager, '_save', side_effect=AssertionError('Authentication must not write')):
            self.now += 10
            self.assertEqual(self.manager.authenticate(session['session_id'])['expires_in'], 28790)
            for _ in range(50):
                self.assertFalse(self.manager.reconfigure(self.settings))
                self.assertIsNotNone(self.manager.authenticate(session['session_id']))
            self.now = 100+28800
            self.assertIsNone(self.manager.authenticate(session['session_id']))

    def test_invalid_cookie_and_clock_reset_do_not_authenticate(self):
        for cookie in (None, '', True, 'a'*42, 'a'*44, '../file', 'session='+'a'*43, '\n'+'a'*43):
            self.assertIsNone(self.manager.authenticate(cookie))
            self.assertFalse(self.manager.logout(cookie))
        session = self.login()
        self.now = 99
        self.assertIsNone(self.manager.authenticate(session['session_id']))

    def test_same_boot_restart_keeps_session_and_new_boot_rejects_it(self):
        session = self.login()
        self.now += 1
        self.assertIsNotNone(self.new_manager().authenticate(session['session_id']))
        self.assertIsNone(self.new_manager(boot='new-boot').authenticate(session['session_id']))

    def test_rotated_username_or_password_hash_invalidates_persisted_sessions(self):
        session = self.login()
        for change in ({'username': 'other'}, {'password_hash': hash_password('another-password')}):
            settings = SimpleNamespace(**(vars(self.settings) | change))
            self.assertIsNone(self.new_manager(settings=settings).authenticate(session['session_id']))

    def test_reconfigure_revokes_immediately_and_persists_only_on_change(self):
        session = self.login()
        updated = SimpleNamespace(**(vars(self.settings) | {'username': 'other'}))
        with patch.object(self.manager, '_save', wraps=self.manager._save) as save:
            self.assertTrue(self.manager.reconfigure(updated))
            self.assertIsNone(self.manager.authenticate(session['session_id']))
            self.assertFalse(self.manager.reconfigure(updated))
            save.assert_called_once()
        self.assertIsNone(self.new_manager(settings=updated).authenticate(session['session_id']))

    def test_timeout_reduction_shortens_sessions_and_increase_does_not_extend_them(self):
        session = self.login()
        shorter = SimpleNamespace(**(vars(self.settings) | {'session_timeout': 60}))
        self.assertTrue(self.manager.reconfigure(shorter))
        self.now += 20
        self.assertEqual(self.manager.authenticate(session['session_id'])['expires_in'], 40)
        self.assertEqual(self.new_manager(settings=shorter).authenticate(session['session_id'])['expires_in'], 40)
        self.manager.reconfigure(self.settings)
        self.assertEqual(self.manager.authenticate(session['session_id'])['expires_in'], 40)
        self.now += 40
        self.assertIsNone(self.manager.authenticate(session['session_id']))

    def test_restart_applies_reduced_timeout_even_before_previous_process_reconfigured(self):
        session = self.login()
        shorter = SimpleNamespace(**(vars(self.settings) | {'session_timeout': 60}))
        self.now += 20
        self.assertEqual(self.new_manager(settings=shorter).authenticate(session['session_id'])['expires_in'], 40)

    def test_disk_failure_does_not_issue_a_session(self):
        with patch.object(self.manager, '_save', side_effect=OSError('disk unavailable')):
            with self.assertRaises(AuthError) as failure:
                self.login()
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(len(self.manager._sessions), 0)
        self.assertFalse(self.path.exists())

    def test_reconfigure_storage_failure_fails_closed_until_a_persisted_login(self):
        session = self.login()
        updated = SimpleNamespace(**(vars(self.settings) | {'session_timeout': 60}))
        with patch.object(self.manager, '_save', side_effect=OSError('read-only storage')):
            with self.assertRaises(AuthError):
                self.manager.reconfigure(updated)
            self.assertIsNone(self.manager.authenticate(session['session_id']))
            self.assertFalse(self.manager.reconfigure(updated))
        replacement = self.login()
        self.assertIsNotNone(self.manager.authenticate(replacement['session_id']))
        self.assertIsNone(self.manager.authenticate(session['session_id']))

    def test_logout_revokes_session_across_web_restart(self):
        session = self.login()
        self.assertTrue(self.manager.logout(session['session_id']))
        self.assertFalse(self.manager.logout(session['session_id']))
        self.assertIsNone(self.new_manager().authenticate(session['session_id']))

    def test_bad_credentials_share_one_message_and_unknown_username_still_hashes(self):
        with patch('modules.web.auth.verify_password', return_value=False) as verify:
            for username in ('admin', 'unknown'):
                with self.assertRaises(AuthError) as failure:
                    self.manager.login(username, 'bad-password', 'client')
                self.assertEqual((failure.exception.status, failure.exception.message),
                                 (401, 'Incorrect username or password.'))
            self.assertEqual(verify.call_count, 2)

    def test_five_failures_per_client_are_limited_and_expire_monotonically(self):
        with patch('modules.web.auth.verify_password', return_value=False) as verify:
            for _ in range(5):
                with self.assertRaises(AuthError) as failure:
                    self.manager.login('admin', PASSWORD, 'client')
                self.assertEqual(failure.exception.status, 401)
            self.now += 1
            with self.assertRaises(AuthError) as failure:
                self.manager.login('admin', PASSWORD, 'client')
            self.assertEqual((failure.exception.status, failure.exception.retry_after), (429, 59))
            self.assertEqual(verify.call_count, 5)
            with self.assertRaises(AuthError) as failure:
                self.manager.login('admin', PASSWORD, 'another-client')
            self.assertEqual(failure.exception.status, 401)
            self.now += 59
            with self.assertRaises(AuthError) as failure:
                self.manager.login('admin', PASSWORD, 'client')
            self.assertEqual(failure.exception.status, 401)

    def test_rate_limit_entries_are_bounded_without_evicting_live_limits(self):
        with patch('modules.web.auth.verify_password', return_value=False):
            for index in range(MAX_CLIENTS):
                with self.assertRaises(AuthError):
                    self.manager.login('admin', PASSWORD, str(index))
            with self.assertRaises(AuthError) as failure:
                self.manager.login('admin', PASSWORD, 'extra')
            self.assertEqual(failure.exception.status, 429)
            self.assertEqual(len(self.manager._clients), MAX_CLIENTS)
            self.now += 60
            with self.assertRaises(AuthError) as failure:
                self.manager.login('admin', PASSWORD, 'extra')
            self.assertEqual(failure.exception.status, 401)
            self.assertEqual(len(self.manager._clients), 1)

    def test_session_count_is_bounded_and_oldest_session_is_evicted(self):
        with patch('modules.web.auth.verify_password', return_value=True):
            sessions = [self.login() for _ in range(MAX_SESSIONS+1)]
        self.assertEqual(len(self.manager._sessions), MAX_SESSIONS)
        self.assertEqual(len(json.loads(self.path.read_text())['sessions']), MAX_SESSIONS)
        self.assertIsNone(self.manager.authenticate(sessions[0]['session_id']))
        self.assertIsNotNone(self.manager.authenticate(sessions[-1]['session_id']))

    def test_hash_concurrency_is_limited_without_blocking_authenticated_polls(self):
        session = self.login()
        release, two_entered = threading.Event(), threading.Event()
        entered = []
        errors = []
        def hashing(password, encoded):
            entered.append(threading.get_ident())
            if len(entered) == 2:
                two_entered.set()
            release.wait(2)
            return False
        def login(client):
            try:
                self.manager.login('admin', PASSWORD, client)
            except AuthError as error:
                errors.append(error.status)
        with patch('modules.web.auth.verify_password', side_effect=hashing):
            workers = [threading.Thread(target=login, args=(str(index),)) for index in range(2)]
            try:
                for worker in workers:
                    worker.start()
                self.assertTrue(two_entered.wait(1))
                self.assertIsNotNone(self.manager.authenticate(session['session_id']))
                with self.assertRaises(AuthError) as failure:
                    self.manager.login('admin', PASSWORD, 'third')
                self.assertEqual(failure.exception.status, 429)
                self.assertEqual(len(entered), 2)
            finally:
                release.set()
                for worker in workers:
                    worker.join(2)
        self.assertEqual(errors, [401, 401])

    def test_credentials_rotated_during_password_hash_cannot_issue_a_session(self):
        updated = SimpleNamespace(**(vars(self.settings) | {'username': 'other'}))
        def hashing(password, encoded):
            self.manager.reconfigure(updated)
            return True
        with patch('modules.web.auth.verify_password', side_effect=hashing):
            with self.assertRaises(AuthError) as failure:
                self.login()
        self.assertEqual(failure.exception.status, 401)
        self.assertFalse(self.manager._sessions)

    def test_corrupt_untrusted_symlink_or_oversized_state_never_grants_access(self):
        session = self.login()
        valid = self.path.read_text()
        for data in ('bad json', '[]', '{}', valid.replace('test-boot', 'other-boot'), ' '*65537):
            self.path.write_text(data)
            self.assertIsNone(self.new_manager().authenticate(session['session_id']))
        self.path.write_text(valid)
        self.path.chmod(0o644)
        self.assertIsNone(self.new_manager().authenticate(session['session_id']))
        self.path.chmod(0o600)
        target = self.path.with_name('target.json')
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertIsNone(self.new_manager().authenticate(session['session_id']))

    def test_absent_password_configuration_does_not_create_state(self):
        settings = SimpleNamespace(**(vars(self.settings) | {'password_hash': ''}))
        manager = self.new_manager(settings=settings)
        self.assertFalse(manager.configured)
        self.assertIsNone(manager.authenticate('a'*43))
        with self.assertRaises(AuthError) as failure:
            self.login(manager=manager)
        self.assertEqual(failure.exception.status, 503)
        self.assertFalse(self.path.exists())

    def test_timeout_only_reconfiguration_does_not_create_a_session_store(self):
        settings = SimpleNamespace(**(vars(self.settings) | {'password_hash': ''}))
        manager = self.new_manager(settings=settings)
        updated = SimpleNamespace(**(vars(settings) | {'session_timeout': 3600}))
        with patch.object(manager, '_save', side_effect=AssertionError('No session store is required')):
            self.assertTrue(manager.reconfigure(updated))
            self.assertFalse(manager.reconfigure(updated))
        self.assertFalse(self.path.exists())

    def test_logout_disk_failure_is_reported_and_authentication_fails_closed(self):
        session = self.login()
        with patch.object(self.manager, '_save', side_effect=OSError('read-only storage')):
            with self.assertRaises(AuthError) as failure:
                self.manager.logout(session['session_id'])
        self.assertEqual(failure.exception.status, 503)
        self.assertIsNone(self.manager.authenticate(session['session_id']))


if __name__ == '__main__':
    unittest.main()
