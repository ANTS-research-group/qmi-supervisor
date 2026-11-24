"""qmi-supervisor-configure token commands (root only)."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.deployment.provisioning import main
from modules.web.tokens import TokenStore


class ApiTokenCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'api-tokens.json'
        patcher = patch('modules.web.tokens.DEFAULT_TOKEN_PATH', str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        root = patch('modules.deployment.provisioning._running_as_root', return_value=True)
        root.start()
        self.addCleanup(root.stop)

    def run_cli(self, *arguments):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(list(arguments))
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def test_create_prints_the_token_once_and_stores_only_its_digest(self):
        code, out, err = self.run_cli('--api-token-create', 'grafana',
                                      '--api-token-scopes', 'read:status,read:metrics')
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        token = lines[0]
        self.assertRegex(token, r'^qms_[0-9a-f]{8}_[A-Za-z0-9_-]{43}$')
        self.assertNotIn(token, self.path.read_text())
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        record = json.loads(self.path.read_text())['tokens'][0]
        self.assertEqual(record['scopes'], ['read:status', 'read:metrics'])
        self.assertNotIn(record['digest'].split(':', 1)[1], token)
        self.assertIn('not shown again', err)

    def test_list_and_revoke_never_print_secrets_or_digests(self):
        _, out, _ = self.run_cli('--api-token-create', 'prometheus',
                                 '--api-token-scopes', 'read:metrics')
        token = out.strip()
        identifier = token.split('_')[1]
        code, listed, _ = self.run_cli('--api-token-list')
        self.assertEqual(code, 0)
        self.assertIn(identifier, listed)
        self.assertIn('prometheus', listed)
        self.assertIn('read:metrics', listed)
        self.assertNotIn(token, listed)
        self.assertNotIn('sha256:', listed)
        code, revoked, _ = self.run_cli('--api-token-revoke', identifier)
        self.assertEqual((code, revoked.strip()), (0, 'revoked'))
        self.assertIsNone(TokenStore(self.path).verify(token))
        self.assertEqual(self.run_cli('--api-token-revoke', 'deadbeef')[1].strip(), 'not found')

    def test_errors_are_refused_without_touching_the_store(self):
        self.assertEqual(self.run_cli('--api-token-create', 'bad',
                                      '--api-token-scopes', 'write:everything')[0], 2)
        self.assertEqual(self.run_cli('--api-token-create', 'bad')[0], 2)
        self.assertEqual(self.run_cli('--api-token-scopes', 'read:status')[0], 2)
        self.assertFalse(self.path.exists())

    def test_token_options_require_root_and_do_not_mix_with_install(self):
        with patch('modules.deployment.provisioning._running_as_root', return_value=False):
            code, _, err = self.run_cli('--api-token-list')
        self.assertEqual(code, 1)
        self.assertIn('requires root', err)
        self.assertEqual(self.run_cli('install', '--api-token-list')[0], 2)
