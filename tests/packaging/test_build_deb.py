"""Run the real packaging script on a synthetic, prebuilt tree.

The tests use a `frontend/out` whose `.version` already matches the package
version, so no npm call, network access or rebuild of the shared export happens.
What is therefore not exercised here is the frontend rebuild path (only the
script itself can run that against the real checkout, because `next build` is
slow and needs the whole frontend). The staged tree and the resulting `.deb`
are real; the second test also runs the `git describe` derivation.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class BuildDebStagingTests(unittest.TestCase):
    def project(self, base, export_version):
        project = base / 'project'
        packaging = project / 'tools' / 'packaging'
        shutil.copytree(ROOT / 'tools' / 'packaging', packaging)
        shutil.copytree(ROOT / 'tools' / 'deployment', project / 'tools' / 'deployment')
        shutil.copytree(ROOT / 'tools' / 'update', project / 'tools' / 'update')
        (project / 'modules').mkdir(parents=True)
        (project / 'config').mkdir()
        for name in ('sdk', 'examples', 'doc'):
            shutil.copytree(ROOT / name, project / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(ROOT / 'qmi_supervisor.py', project / 'qmi_supervisor.py')
        for name in ('README.md', 'INSTALLATION.md', 'CONFIGURATION.md', 'LICENSE'):
            shutil.copy2(ROOT / name, project / name)
        export = project / 'frontend' / 'out'
        export.mkdir(parents=True)
        (export / 'index.html').write_text('<!doctype html><title>synthetic export</title>\n')
        (export / '.version').write_text(export_version + '\n')
        return project, packaging

    def build(self, base, project, packaging, *arguments, extra_env=None):
        (base / 'tmp').mkdir(exist_ok=True)
        output = base / 'dist'
        environment = {**os.environ, 'OUTPUT_DIR': str(output), 'TMPDIR': str(base / 'tmp'),
                       **(extra_env or {})}
        result = subprocess.run(['sh', str(packaging / 'build-deb.sh'), *arguments], cwd=project,
                                env=environment, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output

    def staged_files(self, base, package):
        extracted = base / 'extracted'
        subprocess.run(['dpkg-deb', '--extract', str(package), str(extracted)],
                       check=True, capture_output=True)
        return extracted / 'opt' / 'qmi-supervisor'

    def assert_identity(self, application, version):
        self.assertEqual((application / 'VERSION').read_text(encoding='utf-8'), version + '\n')
        info = json.loads((application / 'BUILD_INFO.json').read_text(encoding='utf-8'))
        self.assertEqual(set(info), {'version', 'commit', 'built_utc'})
        self.assertEqual(info['version'], version)
        self.assertIsInstance(info['commit'], str)
        self.assertRegex(info['built_utc'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        return info

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb is required to inspect the package')
    def test_an_explicit_version_is_staged_with_VERSION_and_BUILD_INFO(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project, packaging = self.project(base, '9.8.7-6')
            output = self.build(base, project, packaging, '9.8.7-6')
            package = output / 'qmi-supervisor_9.8.7-6_all.deb'
            self.assertTrue(package.is_file())
            info = self.assert_identity(self.staged_files(base, package), '9.8.7-6')
            self.assertEqual(info['commit'], '', 'a tree outside git has no commit')

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb is required to inspect the package')
    def test_a_different_export_version_is_rebuilt_with_the_package_version(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project, packaging = self.project(base, '0.0.1-1')
            bin_dir, record = base / 'bin', base / 'npm-called'
            bin_dir.mkdir()
            npm = bin_dir / 'npm'
            npm.write_text('#!/bin/sh\nprintf "%s\\n" "${QMI_SUPERVISOR_VERSION:-}" >> "$NPM_RECORD"\n'
                           'mkdir -p frontend/out\n'
                           'printf "<!doctype html><title>rebuilt export</title>\\n" > frontend/out/index.html\n')
            npm.chmod(0o755)
            path = str(bin_dir) + os.pathsep + os.environ.get('PATH', '')
            output = self.build(base, project, packaging, '9.8.7-6',
                                extra_env={'PATH': path, 'NPM_RECORD': str(record)})
            package = output / 'qmi-supervisor_9.8.7-6_all.deb'
            self.assertTrue(package.is_file(), list(output.iterdir()))
            # The stale export is rebuilt for this version, not reused.
            self.assertIn('rebuilt export', (project / 'frontend/out/index.html').read_text())
            self.assertEqual((project / 'frontend/out/.version').read_text(), '9.8.7-6\n')
            self.assertEqual(record.read_text().split(), ['9.8.7-6', '9.8.7-6'],
                             'QMI_SUPERVISOR_VERSION reaches both npm calls')
            self.assert_identity(self.staged_files(base, package), '9.8.7-6')

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb is required to inspect the package')
    def test_the_package_ships_the_update_units_and_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project, packaging = self.project(base, '9.8.7-6')
            output = self.build(base, project, packaging, '9.8.7-6')
            package = output / 'qmi-supervisor_9.8.7-6_all.deb'
            extracted = base / 'extracted-update'
            subprocess.run(['dpkg-deb', '--extract', str(package), str(extracted)],
                           check=True, capture_output=True)
            for name in ('qmi-supervisor-update-check.service',
                         'qmi-supervisor-update-check-manual.service',
                         'qmi-supervisor-update-check.timer',
                         'qmi-supervisor-update-install@.service',
                         'qmi-supervisor-update-rollback.service'):
                self.assertTrue((extracted / 'etc' / 'systemd' / 'system' / name).is_file(), name)
            wrapper = extracted / 'usr' / 'local' / 'sbin' / 'qmi-supervisor-update'
            self.assertTrue(wrapper.is_file())
            self.assertTrue(os.access(wrapper, os.X_OK), 'the wrapper is executable')
            self.assertEqual(
                wrapper.read_text(encoding='utf-8'),
                '#!/bin/sh\n'
                'exec /usr/bin/python3 -I -B /opt/qmi-supervisor/tools/update/updater.py "$@"\n')
            self.assertTrue(
                (extracted / 'opt' / 'qmi-supervisor' / 'tools' / 'update' / 'updater.py').is_file())

    @unittest.skipUnless(shutil.which('dpkg-deb') and shutil.which('git'),
                         'dpkg-deb and git are required')
    def test_git_describe_derives_the_package_version_and_records_the_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project, packaging = self.project(base, '1.3.0~rc1-1')
            commit = ['-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid']
            subprocess.run(['git', 'init', '-q'], cwd=project, check=True)
            subprocess.run(['git', 'add', '-A'], cwd=project, check=True)
            subprocess.run(['git', *commit, 'commit', '-qm', 'synthetic tree'], cwd=project, check=True)
            subprocess.run(['git', *commit, 'tag', '-a', 'v1.3.0-rc1', '-m', 'synthetic'],
                           cwd=project, check=True)
            output = self.build(base, project, packaging)
            package = output / 'qmi-supervisor_1.3.0~rc1-1_all.deb'
            self.assertTrue(package.is_file(), list(output.iterdir()))
            info = self.assert_identity(self.staged_files(base, package), '1.3.0~rc1-1')
            head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=project,
                                  text=True, capture_output=True, check=True).stdout.strip()
            self.assertEqual(info['commit'], head)


if __name__ == '__main__':
    unittest.main()
