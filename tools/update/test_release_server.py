#!/usr/bin/env python3
"""Loopback GitHub-shaped release server for updater tests (stdlib only).

DIRECTORY holds one subdirectory per release, named by its tag; its files are
the assets and an optional release.json (not an asset) may set name, body,
prerelease, draft and published_at. Nothing runs at import time and the server
never binds a non-loopback address.

    python3 tools/update/test_release_server.py DIRECTORY [--host 127.0.0.1|::1] [--port 0] [--token TOKEN]
"""
import argparse
from datetime import datetime, timezone
import hashlib
import http.server
import json
import re
import socket
import sys
import time
import urllib.parse
from pathlib import Path

API_PREFIX = '/repos/test/qmi-supervisor'
DOWNLOAD_PREFIX = '/download/'


def _content_type(name):
    if name.endswith('.deb'):
        return 'application/x-debian-package'
    if name.endswith('.asc'):
        return 'application/pgp-signature'
    return 'application/octet-stream'


def release_items(server):
    """The GitHub-shaped release list, rebuilt from the directory on every request."""
    directory = Path(server.directory)
    base = server.base_url
    next_id = [1]
    items = []
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        tag = entry.name
        metadata = {}
        meta_file = entry / 'release.json'
        if meta_file.is_file():
            try:
                loaded = json.loads(meta_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict):
                metadata = loaded
        published = metadata.get('published_at')
        if not isinstance(published, str):
            published = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        assets = []
        for asset in sorted(entry.iterdir(), key=lambda path: path.name):
            if not asset.is_file() or asset.is_symlink() or asset.name == 'release.json':
                continue
            data = asset.read_bytes()
            asset_id = next_id[0]
            next_id[0] += 1
            assets.append({
                'id': asset_id,
                'name': asset.name,
                'size': len(data),
                'state': 'uploaded',
                'content_type': _content_type(asset.name),
                'browser_download_url': f'{base}{DOWNLOAD_PREFIX}{tag}/{asset.name}',
                'url': f'{base}{API_PREFIX}/releases/assets/{asset_id}',
                'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
            })
        items.append({
            'id': next_id[0],
            'tag_name': tag,
            'name': metadata.get('name', tag),
            'draft': bool(metadata.get('draft', False)),
            'prerelease': bool(metadata.get('prerelease', False)),
            'published_at': published,
            'body': metadata.get('body', ''),
            'html_url': f'{base}/releases/tag/{tag}',
            'assets': assets,
        })
        next_id[0] += 1
    items.sort(key=lambda item: item['published_at'], reverse=True)
    return items


class ReleaseServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, directory, token):
        self.directory = Path(directory)
        self.token = token
        self.log = []
        self.fault = None
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)
        host = self.server_address[0]
        shown = f'[{host}]' if ':' in host else host
        self.base_url = f'http://{shown}:{self.server_address[1]}'
        self.releases_url = self.base_url + API_PREFIX + '/releases'


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        entry = {'path': self.path, 'headers': {key: value for key, value in self.headers.items()}, 'status': None}
        self.server.log.append(entry)
        try:
            self._handle(entry)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authorized(self):
        token = self.server.token
        return token is None or self.headers.get('Authorization') == 'Bearer ' + token

    def _send(self, status, body, headers, entry):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(0 if status == 304 else len(body)))
        self.end_headers()
        if status != 304 and body:
            self.wfile.write(body)
        entry['status'] = status

    def _json(self, status, value, entry):
        self._send(status, json.dumps(value).encode('utf-8'), {'Content-Type': 'application/json'}, entry)

    def _items(self):
        return release_items(self.server)

    def _find_asset(self, asset_id):
        for item in self._items():
            for asset in item['assets']:
                if asset['id'] == asset_id:
                    return asset
        return None

    def _handle(self, entry):
        server = self.server
        request = urllib.parse.urlsplit(self.path)
        path = request.path

        if path == API_PREFIX + '/releases':
            if not self._authorized():
                self._json(401, {'message': 'Bad credentials'}, entry)
                return
            fault = server.fault
            if fault:
                delay = fault.get('delay')
                if delay:
                    time.sleep(delay)
                body = fault.get('body')
                if body is None:
                    body = json.dumps(self._items()).encode('utf-8')
                self._send(fault.get('status', 200), body, fault.get('headers') or {}, entry)
                return
            body = json.dumps(self._items()).encode('utf-8')
            etag = 'W/"' + hashlib.sha256(body).hexdigest()[:40] + '"'
            if self.headers.get('If-None-Match') == etag:
                self._send(304, b'', {'ETag': etag}, entry)
                return
            self._send(200, body, {'ETag': etag, 'Content-Type': 'application/json'}, entry)
            return

        match = re.fullmatch(re.escape(API_PREFIX) + r'/releases/assets/(\d+)', path)
        if match:
            if not self._authorized():
                self._json(401, {'message': 'Bad credentials'}, entry)
                return
            asset = self._find_asset(int(match.group(1)))
            if asset is None:
                self._json(404, {'message': 'Not Found'}, entry)
                return
            if 'application/octet-stream' in self.headers.get('Accept', ''):
                self._send(302, b'', {'Location': asset['browser_download_url']}, entry)
            else:
                self._json(200, asset, entry)
            return

        match = re.fullmatch(re.escape(DOWNLOAD_PREFIX) + r'([^/]+)/(.+)', path)
        if match:
            tag = urllib.parse.unquote(match.group(1))
            name = urllib.parse.unquote(match.group(2))
            root = Path(server.directory).resolve()
            base = (root / tag).resolve()
            candidate = (base / name).resolve()
            # Only a direct child file of a direct child release directory; absolute
            # names, '..', separators and symlinks pointing outside are refused.
            safe = (base.parent == root and '/' not in name and '\\' not in name
                    and not name.startswith('/') and candidate.parent == base
                    and not (base / name).is_symlink() and candidate.is_file())
            if not safe or name == 'release.json':
                self._json(404, {'message': 'Not Found'}, entry)
                return
            self._send(200, candidate.read_bytes(), {'Content-Type': _content_type(name)}, entry)
            return

        self._json(404, {'message': 'Not Found'}, entry)


def make_server(directory, *, host='127.0.0.1', port=0, token=None):
    """A loopback-only release server; tests call shutdown() and server_close()."""
    if host not in ('127.0.0.1', '::1'):
        raise ValueError("host must be '127.0.0.1' or '::1'")
    return ReleaseServer((host, port), _Handler, directory=directory, token=token)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Loopback GitHub-shaped release server for updater tests.')
    parser.add_argument('directory')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--token', default=None)
    args = parser.parse_args(argv)
    server = make_server(args.directory, host=args.host, port=args.port, token=args.token)
    print('RELEASES_URL=' + server.releases_url, flush=True)
    try:
        server.serve_forever(poll_interval=0.01)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
