"""Next export delivery stays separate from private API/configuration files."""
from dataclasses import replace
import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from modules.runtime.settings import Settings
from modules.web.auth import hash_password
from modules.web.server import SESSION_COOKIE, STATIC_ROUTES, build_server, static_file, validate_static_export
from tests.web.static_fixture import make_export


class PublicRouteTests(unittest.TestCase):
    """The served routes and the Next application router must move together."""

    app = Path(__file__).resolve().parents[2] / 'frontend' / 'app'

    def page(self, directory):
        return directory.is_dir() and (directory/'page.tsx').exists()

    def dynamic(self, directory):
        return [child for child in directory.iterdir() if child.is_dir() and child.name.startswith('[') and self.page(child)] \
            if directory.is_dir() else []

    def test_top_level_routes_and_pages_agree(self):
        tops = {'' if page.parent == self.app else page.parent.relative_to(self.app).parts[0]
                for page in self.app.rglob('page.tsx')}
        self.assertEqual({route.split('/')[0] for route in STATIC_ROUTES}, tops,
                         'a renamed or removed page must be renamed in STATIC_ROUTES too')

    def test_every_declared_route_has_a_page(self):
        for route in STATIC_ROUTES:
            parts = route.split('/') if route else []
            directory = self.app.joinpath(*parts)
            self.assertTrue(self.page(directory) or self.dynamic(directory)
                            or self.dynamic(self.app.joinpath(*parts[:-1])),
                            f'{route or "/"} is served but frontend/app has no page for it')


class ExportValidationTests(unittest.TestCase):
    def test_complete_export_is_required_without_a_node_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'out'
            with self.assertRaisesRegex(ValueError, 'npm ci'):
                validate_static_export(root)
            make_export(root)
            with patch('subprocess.run', side_effect=AssertionError('No build on the target')):
                self.assertEqual(validate_static_export(root), root.resolve())
            (root/'metrics/charts/index.html').unlink()
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                validate_static_export(root)

    def test_symlinks_in_root_parents_and_export_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = make_export(parent/'out')
            (root/'linked.txt').symlink_to(parent/'private.ini')
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                validate_static_export(root)
            (root/'linked.txt').unlink()
            alias = parent/'alias'
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                validate_static_export(alias)
            with self.assertRaises(ValueError):
                validate_static_export(alias/'dashboard')

    def test_paths_are_never_decoded_or_resolved_outside_public_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_export(Path(directory)/'out').resolve()
            for name in ('config.ini', 'source.tsx', 'chunk.js.map', '.env', 'package-lock.yaml'):
                (root/name).write_text('private fixture')
            for path in ('/../private.html', '/dashboard/../index.html', '/%2e%2e/private.html', '/%69ndex.html',
                         '/dashboard%2findex.html', '/dashboard\\index.html', '//evil/index.html',
                         '/config.ini', '/source.tsx', '/chunk.js.map', '/.env', '/package-lock.yaml',
                         '/api/session', '/dashboard//index.html'):
                with self.subTest(path=path):
                    self.assertIsNone(static_file(root, path))
            (root/'secret.txt').symlink_to(Path(directory)/'private.txt')
            self.assertIsNone(static_file(root, '/secret.txt'))


class StaticHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.export = make_export(self.root/'out')
        self.password = 'static export password'
        self.encoded = hash_password(self.password)
        self.settings = replace(Settings(), web=replace(Settings().web, enabled=True, password_hash=self.encoded))
        self.server = build_server(self.settings, self.root/'config.ini', self.root/'status.json',
                                   static_root=self.export, server_address=('127.0.0.1', 0))
        session = self.server.auth.login('admin', self.password, 'test-client')
        self.cookie = {'Cookie': f'{SESSION_COOKIE}={session["session_id"]}'}
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            connection.request('GET', path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_direct_routes_and_rsc_payloads_are_public_but_api_remains_private(self):
        for path in ('/', '/dashboard', '/dashboard/', '/modem/', '/modem/?modem=second', '/metrics/charts/', '/metrics/signal/', '/metrics/performance/',
                     '/metrics/data/', '/configuration/modem/', '/configuration/telemetry/', '/configuration/web/', '/profile/'):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertIn('text/html', headers['Content-Type'])
            self.assertNotIn(b'static export password', body)
        for path in ('/dashboard.txt?_rsc=arbitrary', '/dashboard/index.txt?_rsc=value'):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(headers['Content-Type'].startswith('text/plain'))
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(body, b'exported RSC navigation payload')
        self.assertEqual(self.request('/api/status')[0], 401)
        self.assertEqual(self.request('/api/config')[0], 401)

    def test_hashed_assets_have_correct_mime_and_cache_without_duplicate_headers(self):
        for path, mime in (('/_next/static/chunks/0123456789abcdef.js', 'application/javascript'),
                           ('/_next/static/chunks/2khxl0v1q4i6n.js', 'application/javascript'),
                           ('/_next/static/v93U6dXeqVcT7Z_qvdmq7/_buildManifest.js', 'application/javascript'),
                           ('/_next/static/css/abcdef0123456789.css', 'text/css'),
                           ('/_next/static/fonts/abcdef0123456789.woff2', 'font/woff2')):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(headers['Content-Type'].startswith(mime))
            self.assertEqual(headers['Cache-Control'], 'public, max-age=31536000, immutable')
            self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertEqual(self.request('/', {'Host':f'evil.example:{self.server.server_port}'})[0], 403)

    def test_private_sources_and_new_symlinks_are_unavailable_even_after_startup(self):
        outside = self.root/'private.txt';outside.write_text('private configuration')
        (self.export/'private.txt').symlink_to(outside)
        (self.export/'project.ini').write_text('private configuration')
        for path in ('/private.txt', '/project.ini', '/%69ndex.html', '/../private.txt', '/node_modules/package.json'):
            self.assertNotEqual(self.request(path, self.cookie)[0], 200, path)
