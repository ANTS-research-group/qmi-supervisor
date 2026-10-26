"""Tiny static export fixture: backend tests never need npm or generated builds."""
from pathlib import Path

from modules.web.server import STATIC_ROUTES


def make_export(root):
    root = Path(root)
    files = {route + '/index.html' if route else 'index.html':
             '<!doctype html><html><title>QMI ' + (route or 'home') + '</title></html>'
             for route in STATIC_ROUTES}
    files.update({'_next/static/chunks/0123456789abcdef.js': '// compiled browser application',
                  '_next/static/chunks/2khxl0v1q4i6n.js': '// compiled Turbopack content hash',
                  '_next/static/v93U6dXeqVcT7Z_qvdmq7/_buildManifest.js': '// per-build manifest',
                  '_next/static/css/abcdef0123456789.css': 'body{color:#123}',
                  '_next/static/fonts/abcdef0123456789.woff2': 'fake font bytes',
                  'dashboard/index.txt': 'exported RSC navigation payload',
                  'dashboard.txt': 'exported RSC navigation payload',
                  'favicon.ico': 'fake icon bytes'})
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root
