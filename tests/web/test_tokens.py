"""scoped API tokens, digests at rest and constant-time verification."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.web.tokens import DEFAULT_TOKEN_PATH, SCOPES, TokenError, TokenStore


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def utc(self):
        return datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=self.now - 1000)


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'api-tokens.json'
        self.clock = Clock()
        self.store = TokenStore(self.path, clock=self.clock, utcnow=self.clock.utc)

    def test_a_created_token_is_returned_once_and_only_its_digest_is_stored(self):
        token = self.store.create('grafana', ['read:status', 'read:metrics'])
        self.assertRegex(token, r'^qms_[0-9a-f]{8}_[A-Za-z0-9_-]{43}$')
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['version'], 1)
        self.assertEqual(len(stored['tokens']), 1)
        record = stored['tokens'][0]
        self.assertNotIn(token.split('_')[2], self.path.read_text())
        self.assertRegex(record['digest'], r'^sha256:[0-9a-f]{64}$')
        self.assertEqual(record['scopes'], ['read:status', 'read:metrics'])
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        public = self.store.list()
        self.assertEqual(public[0]['name'], 'grafana')
        self.assertNotIn('digest', public[0])

    def test_verify_accepts_the_exact_token_only(self):
        token = self.store.create('prometheus', ['read:metrics'])
        record = self.store.verify(token)
        self.assertEqual(record['name'], 'prometheus')
        self.assertEqual(record['scopes'], ['read:metrics'])
        for invalid in (None, '', 'qms_', token + 'x', token[:-1], token.replace('qms_', 'qmsx'),
                        'qms_00000000_' + 'A' * 43):
            with self.subTest(invalid=str(invalid)[:24]):
                self.assertIsNone(self.store.verify(invalid))

    def test_revoked_and_expired_tokens_are_rejected(self):
        token = self.store.create('short', ['read:status'], expires_days=1)
        self.assertIsNotNone(self.store.verify(token))
        self.clock.now += 2 * 86400
        self.assertIsNone(self.store.verify(token))
        other = self.store.create('revoked', ['read:status'])
        self.assertTrue(self.store.revoke(other.split('_')[1]))
        self.assertIsNone(self.store.verify(other))
        self.assertFalse(self.store.revoke('deadbeef'))

    def test_unknown_scopes_and_bad_names_are_refused(self):
        for scopes in ([], ['write:everything'], ['read:status', 'read:identity']):
            with self.subTest(scopes=scopes), self.assertRaises(TokenError):
                self.store.create('name', scopes)
        for name in ('', 'x' * 65, 'bad\nname'):
            with self.subTest(name=name[:10]), self.assertRaises(TokenError):
                self.store.create(name, ['read:status'])
        with self.assertRaises(TokenError):
            self.store.create('name', ['read:status'], expires_days=0)

    def test_last_used_is_batched_to_once_per_minute(self):
        token = self.store.create('batch', ['read:status'])
        first = self.store.verify(token)['last_used_utc']
        self.assertIsNotNone(first)
        self.clock.now += 30
        self.assertEqual(self.store.verify(token)['last_used_utc'], first)
        self.clock.now += 31
        self.assertNotEqual(self.store.verify(token)['last_used_utc'], first)

    def test_an_untrusted_or_world_readable_file_grants_nothing(self):
        token = self.store.create('safe', ['read:status'])
        os.chmod(self.path, 0o644)
        self.assertIsNone(self.store.verify(token))
        self.assertEqual(self.store.list(), [])
        os.chmod(self.path, 0o600)
        link = self.path.with_name('link.json')
        link.symlink_to(self.path)
        self.assertEqual(TokenStore(link).list(), [])

    def test_the_default_path_is_the_application_configuration_directory(self):
        self.assertEqual(DEFAULT_TOKEN_PATH, '/etc/qmi-supervisor/api-tokens.json')
        self.assertEqual(set(SCOPES), {'read:status', 'read:events', 'read:metrics'})
