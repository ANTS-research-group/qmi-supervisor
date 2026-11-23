"""URL policy and bounded detail of the HTTP client (no network is contacted)."""
import os
import unittest
from unittest.mock import patch

from modules.update import transport


class TransportPolicyTests(unittest.TestCase):
    def test_check_url_accepts_only_https_and_allowed_loopback(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(transport.check_url('https://api.github.com/x'), 'https://api.github.com/x')
            for url in ('http://example.com/x', 'ftp://example.com/x', 'https://user:pw@host/x',
                        'https://host/x#f', 'https://host/a b', 'http://localhost/x'):
                with self.subTest(url=url), self.assertRaises(transport.UpdateCheckError) as caught:
                    transport.check_url(url)
                self.assertEqual(caught.exception.code, 'bad_source')
            for url in ('http://127.0.0.1:9/x', 'http://[::1]:9/x'):
                with self.subTest(url=url), self.assertRaises(transport.UpdateCheckError):
                    transport.check_url(url)

    def test_loopback_http_needs_the_exact_test_variable(self):
        with patch.dict(os.environ, {transport.TEST_SOURCE_ENV: '1'}):
            self.assertEqual(transport.check_url('http://127.0.0.1:9/x'), 'http://127.0.0.1:9/x')
            self.assertEqual(transport.check_url('http://[::1]:9/x'), 'http://[::1]:9/x')
        with patch.dict(os.environ, {transport.TEST_SOURCE_ENV: 'yes'}):
            with self.assertRaises(transport.UpdateCheckError):
                transport.check_url('http://127.0.0.1:9/x')

    def test_bounded_cuts_at_256_bytes_without_breaking_utf8(self):
        value = transport.bounded('ñ' * 200)
        self.assertLessEqual(len(value.encode('utf-8')), 256)
        value.encode('utf-8')  # valid
        self.assertEqual(transport.bounded('a' * 10 + '\x00\x01\x7f'), 'a' * 10)

    def test_redirect_code_is_reported(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(transport.UpdateCheckError) as caught:
                transport.check_url('http://example.com/x', code='bad_redirect')
        self.assertEqual(caught.exception.code, 'bad_redirect')


if __name__ == '__main__':
    unittest.main()
