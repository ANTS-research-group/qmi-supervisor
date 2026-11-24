"""Plugin manifests: validation, capability allow-lists and enable rewriting."""
from pathlib import Path
import tempfile
import unittest

from modules.runtime.plugins import (CAPABILITIES, load_manifest, load_manifests,
                                     set_enabled)

MANIFEST = """[plugin]
name = demo
description = Demo plugin
version = 1.0.0
exec = {exec}
args = --flag value
enabled = false
capabilities = read:events
os_capabilities = net_raw

[settings]
url = https://example.invalid/hook
secret = change-me
"""


class ManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root/'plugins.d'
        self.directory.mkdir()
        self.executable = self.root/'plugin.py'
        self.executable.write_text('#!/bin/sh\nexit 0\n')
        self.executable.chmod(0o755)

    def manifest(self, name='demo', **values):
        text = MANIFEST.format(exec=self.executable)
        for key, value in values.items():
            lines = []
            for line in text.splitlines(keepends=True):
                if line.strip().startswith(key + ' ='):
                    line = f'{key} = {value}\n'
                lines.append(line)
            text = ''.join(lines)
        path = self.directory/(name + '.ini')
        path.write_text(text)
        return path

    def test_a_valid_manifest_exposes_capabilities_and_settings(self):
        path = self.manifest()
        manifest = load_manifest(path, require_root=False)
        self.assertTrue(manifest.valid, manifest.error)
        self.assertEqual((manifest.name, manifest.version), ('demo', '1.0.0'))
        self.assertEqual(manifest.args, ('--flag', 'value'))
        self.assertEqual(manifest.capabilities, ('read:events',))
        self.assertEqual(manifest.os_capabilities, ('net_raw',))
        self.assertEqual(manifest.settings['url'], 'https://example.invalid/hook')
        self.assertIn('read:status', CAPABILITIES)

    def test_invalid_manifests_are_listed_with_their_reason(self):
        path = self.manifest(capabilities='write:all')
        manifest = load_manifest(path, require_root=False)
        self.assertFalse(manifest.valid)
        self.assertIn('unknown capability', manifest.error)
        wrong_name = self.manifest(name='other')
        self.assertIn('equal the file name', load_manifest(wrong_name, require_root=False).error)
        missing_exec = self.manifest()
        missing_exec.write_text(missing_exec.read_text().replace(
            str(self.executable), '/nonexistent/plugin.py'))
        self.assertIn('does not exist', load_manifest(missing_exec, require_root=False).error)
        bad_os = self.manifest()
        bad_os.write_text(bad_os.read_text().replace('net_raw', 'root_everything'))
        self.assertIn('unknown system capability', load_manifest(bad_os, require_root=False).error)

    def test_root_ownership_is_checked_only_when_requested(self):
        self.executable.chmod(0o777)
        self.assertFalse(load_manifest(self.manifest(), require_root=True).valid)
        self.assertTrue(load_manifest(self.manifest(), require_root=False).valid)

    def test_enable_rewrites_only_that_value(self):
        path = self.manifest()
        before = path.read_text().replace('enabled = false', 'enabled = true')
        set_enabled(path, True)
        self.assertEqual(path.read_text(), before)
        self.assertTrue(load_manifest(path, require_root=False).enabled)
        set_enabled(path, False)
        self.assertFalse(load_manifest(path, require_root=False).enabled)
        with self.assertRaises(ValueError):
            set_enabled(self.directory/'missing.ini', True)

    def test_the_directory_lists_valid_and_invalid_manifests(self):
        self.manifest()
        (self.directory/'bad.ini').write_text('[plugin]\nname = bad\n')
        manifests = load_manifests(self.directory, require_root=False)
        self.assertEqual([item.name for item in manifests], ['bad', 'demo'])
        self.assertFalse(manifests[0].valid)
        self.assertTrue(manifests[1].valid)
