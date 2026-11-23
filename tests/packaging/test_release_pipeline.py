"""Release pipeline (U5): package versions, release files, signing key tools and their documentation."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

try:
    import yaml
except ImportError:  # PyYAML is optional; the text checks below always run
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT/'tools/packaging'
WORKFLOW = ROOT/'.github/workflows/package-deb.yml'
KEYRING = PACKAGING/'keys/release-signing.gpg'
HAVE_GPG = all(shutil.which(name) for name in ('gpg', 'gpgv', 'gpgconf'))
HAVE_DPKG_DEB = shutil.which('dpkg-deb') is not None
HAVE_GIT = shutil.which('git') is not None
DOCUMENTS = ('README.md', 'INSTALLATION.md', 'CONFIGURATION.md',
             'tools/packaging/README.md', 'tools/packaging/keys/README.md')
NETWORK_TOOLS = ('gh', 'curl', 'wget', 'git', 'ssh', 'scp')


def temporary_directory(test):
    # Short paths under /tmp: gpg-agent sockets must stay below 108 bytes.
    directory = tempfile.TemporaryDirectory(prefix='qmi-release-', dir='/tmp')
    test.addCleanup(directory.cleanup)
    return Path(directory.name)


def openpgp_packets(data):
    """Yield (tag, body) for every OpenPGP packet (RFC 4880, section 4.2)."""
    offset = 0
    while offset < len(data):
        header = data[offset]
        if not header & 0x80:
            raise ValueError(f'no OpenPGP packet at byte {offset}')
        if header & 0x40:
            tag, first = header & 0x3F, data[offset + 1]
            if first < 192:
                length, start = first, offset + 2
            elif first < 224:
                length, start = ((first - 192) << 8) + data[offset + 2] + 192, offset + 3
            elif first == 255:
                length, start = int.from_bytes(data[offset + 2:offset + 6], 'big'), offset + 6
            else:
                raise ValueError('partial body lengths do not occur in keyrings')
        else:
            tag, kind = (header >> 2) & 0x0F, header & 0x03
            if kind == 3:
                raise ValueError('indeterminate lengths do not occur in keyrings')
            size = (1, 2, 4)[kind]
            length, start = int.from_bytes(data[offset + 1:offset + 1 + size], 'big'), offset + 1 + size
        body = data[start:start + length]
        if len(body) != length:
            raise ValueError('truncated OpenPGP packet')
        yield tag, body
        offset = start + length


def v4_fingerprints(data):
    """Fingerprints of the primary keys. gpgv 2.2 (Ubuntu 22.04) reads version 4 keys only."""
    fingerprints = []
    for tag, body in openpgp_packets(data):
        if tag in (5, 7):
            raise ValueError('the keyring contains a private key')
        if tag == 6:
            if body[0] != 4:
                raise ValueError(f'version {body[0]} key; gpgv 2.2 needs version 4 keys')
            fingerprints.append(hashlib.sha1(b'\x99' + len(body).to_bytes(2, 'big') + body).hexdigest().upper())
    return fingerprints


def markdown_lines(path):
    """Lines of a Markdown file outside fenced code blocks."""
    fenced = False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
            continue
        if not fenced:
            yield line


def anchors(path):
    """GitHub heading anchors of a Markdown file."""
    found, seen = set(), {}
    for line in markdown_lines(path):
        match = re.match(r'#{1,6}\s+(.*?)\s*$', line)
        if match:
            slug = re.sub(r'[^\w\- ]', '', match.group(1).lower()).replace(' ', '-')
            count = seen.get(slug, 0)
            found.add(slug if count == 0 else f'{slug}-{count}')
            seen[slug] = count + 1
    return found


def section(path, heading):
    """Text of one '## ' section, up to the next level-2 heading."""
    text = path.read_text()
    start = text.index(f'\n{heading}\n')
    end = text.find('\n## ', start + 1)
    return text[start:end if end >= 0 else len(text)]


def make_deb(directory, version):
    """A minimal qmi-supervisor package with the given version, in directory/build."""
    stage = directory/'deb-stage'
    shutil.rmtree(stage, ignore_errors=True)
    (stage/'DEBIAN').mkdir(parents=True)
    (stage/'DEBIAN').chmod(0o755)  # dpkg-deb rejects a 0700 control directory
    (stage/'DEBIAN/control').write_text(
        f'Package: qmi-supervisor\nVersion: {version}\nArchitecture: all\n'
        'Maintainer: Test <test@example.invalid>\nDescription: test package\n')
    output = directory/'build'
    output.mkdir(exist_ok=True)
    package = output/f'qmi-supervisor_{version}_all.deb'
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(stage), str(package)],
                   check=True, capture_output=True)
    return package


class WorkflowTests(unittest.TestCase):
    """.github/workflows/package-deb.yml, read as text: no network, no GitHub."""

    def setUp(self):
        self.text = WORKFLOW.read_text()

    def step(self, name):
        """Lines of one step of the deb job, from its '- name:' line to the next step."""
        lines = self.text.splitlines()
        start = lines.index(f'      - name: {name}')
        end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith('      - ')),
                   len(lines))
        return '\n'.join(lines[start:end])

    def test_release_files_are_staged_signed_and_then_attached(self):
        positions = [self.text.index(text) for text in (
            'run: ./tools/packaging/stage-release.sh build build/release',
            'run: ./tools/packaging/sign-release.sh build/release/SHA256SUMS',
            'uses: softprops/action-gh-release@v2',
            'ASSETS: ${{ steps.release.outputs.assets }}')]
        self.assertEqual(positions, sorted(positions))
        release = self.step('Attach the package and its checksums to the release')
        for text in ('id: release', 'files: build/release/*', 'fail_on_unmatched_files: true',
                     "prerelease: ${{ contains(github.ref_name, '-') }}"):
            self.assertIn(text, release)

    def test_tag_checks_signing_and_publishing_run_only_for_tags(self):
        for name in ('Check the tag name', 'Sign SHA256SUMS',
                     'Attach the package and its checksums to the release', 'Check the published asset names'):
            with self.subTest(step=name):
                self.assertIn("if: startsWith(github.ref, 'refs/tags/')", self.step(name))

    def test_secrets_reach_only_the_signing_step_through_its_environment(self):
        lines = [line for line in self.text.splitlines() if 'secrets.' in line]
        self.assertEqual([line.strip() for line in lines], [
            'RELEASE_SIGNING_KEY: ${{ secrets.RELEASE_SIGNING_KEY }}',
            'RELEASE_SIGNING_PASSPHRASE: ${{ secrets.RELEASE_SIGNING_PASSPHRASE }}'])
        signing = self.step('Sign SHA256SUMS')
        for line in lines:
            self.assertIn(line, signing)

    def test_run_scripts_interpolate_no_expressions(self):
        """Values reach the scripts through env:, never as ${{ }} text inside a script."""
        lines = self.text.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r'(\s+)(?:- )?run: (.*)$', line)
            if not match:
                continue
            script = [match.group(2)]
            if match.group(2) == '|':
                indent = len(match.group(1))
                for following in lines[index + 1:]:
                    if following.strip() and len(following) - len(following.lstrip()) <= indent:
                        break
                    script.append(following)
            with self.subTest(line=index + 1):
                self.assertNotIn('${{', '\n'.join(script))

    def test_tag_pattern_accepts_only_releases_and_release_candidates(self):
        pattern = re.search(r"TAG_PATTERN: '([^']+)'", self.text).group(1)
        for tag in ('v1.0.0', 'v1.2.0', 'v1.3.0-rc1', 'v2.10.3-rc12'):
            self.assertRegex(tag, pattern)
        for tag in ('v1.3.0-rc0', 'v1.3', 'v01.2.0', '1.2.0', 'v1.3.0-beta1', 'v1.3.0-rc1-fix', 'vtest'):
            self.assertIsNone(re.search(pattern, tag), tag)

    def test_release_scripts_are_executable(self):
        for name in ('build-deb.sh', 'stage-release.sh', 'sign-release.sh', 'make-signing-key.sh'):
            self.assertTrue(os.access(PACKAGING/name, os.X_OK), name)

    @unittest.skipIf(yaml is None, 'PyYAML is not installed')
    def test_workflow_is_valid_yaml_with_the_expected_release_step(self):
        document = yaml.safe_load(self.text)
        triggers = document.get('on', document.get(True))  # YAML 1.1 reads the key "on" as True
        self.assertEqual(triggers['push']['tags'], ['v*'])
        steps = document['jobs']['deb']['steps']
        release = next(step for step in steps
                       if str(step.get('uses', '')).startswith('softprops/action-gh-release@'))
        self.assertEqual(release['with']['files'], 'build/release/*')
        self.assertEqual(release['with']['prerelease'], "${{ contains(github.ref_name, '-') }}")
        self.assertEqual(document.get('permissions'), {'contents': 'write'},
                         'the release step needs contents: write')
        self.assertNotIn('pull_request', triggers)
        self.assertNotIn('pull_request_target', triggers)


@unittest.skipUnless(HAVE_DPKG_DEB, 'dpkg-deb is required')
class StageReleaseTests(unittest.TestCase):
    """tools/packaging/stage-release.sh with minimal packages built by dpkg-deb."""

    def stage(self, root, **environment):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GITHUB_')}
        env.update(environment)
        return subprocess.run(['sh', str(PACKAGING/'stage-release.sh'), str(root/'build'), str(root/'release')],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_release_candidate_gets_a_name_github_keeps_and_its_checksum(self):
        root = temporary_directory(self)
        package = make_deb(root, '1.3.0~rc1-1')
        result = self.stage(root, GITHUB_REF_TYPE='tag', GITHUB_REF_NAME='v1.3.0-rc1')
        self.assertEqual(result.returncode, 0, result.stderr)
        name = 'qmi-supervisor_1.3.0.rc1-1_all.deb'
        self.assertEqual(sorted(path.name for path in (root/'release').iterdir()), ['SHA256SUMS', name])
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        self.assertEqual((root/'release/SHA256SUMS').read_text(), f'{digest}  {name}\n')
        self.assertEqual((root/'release'/name).read_bytes(), package.read_bytes())

    def test_tag_build_refuses_a_package_of_another_version(self):
        for version in ('1.3.0~rc1-1', '1.3.0.dirty-1', '1.3.1-1'):
            with self.subTest(version=version):
                root = temporary_directory(self)
                make_deb(root, version)
                result = self.stage(root, GITHUB_REF_TYPE='tag', GITHUB_REF_NAME='v1.3.0')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('does not match tag v1.3.0', result.stderr)
                self.assertFalse((root/'release').exists())

    def test_packaging_revision_and_branch_builds_are_accepted(self):
        root = temporary_directory(self)
        make_deb(root, '1.3.0-2')
        result = self.stage(root, GITHUB_REF_TYPE='tag', GITHUB_REF_NAME='v1.3.0')
        self.assertEqual(result.returncode, 0, result.stderr)
        root = temporary_directory(self)
        make_deb(root, '1.2.0+4.gabc1234-1')
        result = self.stage(root, GITHUB_REF_TYPE='branch', GITHUB_REF_NAME='main')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root/'release/qmi-supervisor_1.2.0+4.gabc1234-1_all.deb').is_file())

    def test_exactly_one_package_is_required(self):
        root = temporary_directory(self)
        (root/'build').mkdir()
        self.assertNotEqual(self.stage(root).returncode, 0)
        make_deb(root, '1.3.0-1')
        make_deb(root, '1.3.0-2')
        result = self.stage(root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('exactly one', result.stderr)


@unittest.skipUnless(HAVE_GPG, 'gpg, gpgv and gpgconf are required')
class SigningTests(unittest.TestCase):
    """make-signing-key.sh and sign-release.sh with throwaway keys in temporary GnuPG homes.

    Never the user's key: HOME and every GnuPG home point into a temporary directory.
    """

    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix='qmi-signing-', dir='/tmp')
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        fake = cls.root/'fake-bin'
        fake.mkdir()
        cls.calls = cls.root/'network-calls.log'
        for name in NETWORK_TOOLS:
            tool = fake/name
            tool.write_text(f'#!/bin/sh\necho "{name} $*" >> "{cls.calls}"\nexit 1\n')
            tool.chmod(0o755)
        cls.environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(('RELEASE_', 'GNUPGHOME'))}
        cls.environment.update(HOME=str(cls.root), TMPDIR='/tmp',
                               PATH=f'{fake}{os.pathsep}{os.environ["PATH"]}')
        cls.keys = {name: cls.make_key(name, f'passphrase-{name}-0123456') for name in ('a', 'b')}

    @classmethod
    def make_key(cls, name, passphrase):
        home, keyring = cls.root/f'home-{name}', cls.root/f'{name}.gpg'
        agent = dict(cls.environment, GNUPGHOME=str(home))
        cls.addClassCleanup(subprocess.run, ['gpgconf', '--kill', 'gpg-agent'], env=agent, capture_output=True)
        result = subprocess.run(['sh', str(PACKAGING/'make-signing-key.sh'), '--home', str(home),
                                 '--keyring', str(keyring)], input=passphrase + '\n', env=cls.environment,
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        fingerprint = re.search(r'Fingerprint:\s+([0-9A-F]{40})', result.stdout).group(1)
        secret = subprocess.run(['gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase-fd', '0',
                                 '--armor', '--export-secret-keys', fingerprint], input=passphrase, env=agent,
                                capture_output=True, text=True, check=True, timeout=60).stdout
        return {'home': home, 'keyring': keyring, 'fingerprint': fingerprint, 'secret': secret,
                'passphrase': passphrase, 'output': result.stdout}

    def checksums(self):
        sums = temporary_directory(self)/'SHA256SUMS'
        sums.write_text(f'{"0" * 64}  qmi-supervisor_1.3.0.rc1-1_all.deb\n')
        return sums

    def sign(self, sums, keyring, secret=None, passphrase=None):
        env = dict(self.environment, RELEASE_KEYRING=str(keyring))
        if secret is not None:
            env['RELEASE_SIGNING_KEY'] = secret
        if passphrase is not None:
            env['RELEASE_SIGNING_PASSPHRASE'] = passphrase
        return subprocess.run(['sh', str(PACKAGING/'sign-release.sh'), str(sums)], env=env,
                              capture_output=True, text=True, timeout=120)

    def gpgv(self, keyring, sums):
        env = dict(self.environment, GNUPGHOME=str(temporary_directory(self)))
        return subprocess.run(['gpgv', '--keyring', str(keyring), f'{sums}.asc', str(sums)],
                              env=env, capture_output=True, text=True, timeout=60).returncode

    def test_key_script_writes_a_public_version_4_keyring_and_uploads_nothing(self):
        key = self.keys['a']
        self.assertFalse(self.calls.exists(), 'a network or VCS tool was called')
        self.assertEqual(v4_fingerprints(key['keyring'].read_bytes()), [key['fingerprint']])
        self.assertEqual(key['keyring'].stat().st_mode & 0o777, 0o644)
        self.assertEqual(key['home'].stat().st_mode & 0o777, 0o700)
        for text in ('Nothing was uploaded', 'RELEASE_SIGNING_KEY', 'RELEASE_SIGNING_PASSPHRASE',
                     f'Release signing key fingerprint: `{key["fingerprint"]}`.'):
            self.assertIn(text, key['output'])
        script = (PACKAGING/'make-signing-key.sh').read_text()
        for forbidden in ('--send-key', '--keyserver', '--recv-key'):
            self.assertNotIn(forbidden, script)

    def test_key_script_refuses_existing_files_and_short_passphrases(self):
        existing = self.keys['a']
        cases = (
            (['--home', str(existing['home']), '--keyring', str(self.root/'new-1.gpg')], 'long-enough-passphrase'),
            (['--home', str(self.root/'new-home-2'), '--keyring', str(existing['keyring'])], 'long-enough-passphrase'),
            (['--home', str(self.root/'new-home-3'), '--keyring', str(self.root/'new-3.gpg')], 'short'),
        )
        for arguments, passphrase in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(['sh', str(PACKAGING/'make-signing-key.sh'), *arguments],
                                        input=passphrase + '\n', env=self.environment,
                                        capture_output=True, text=True, timeout=60)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(v4_fingerprints(existing['keyring'].read_bytes()), [existing['fingerprint']])
        for name in ('new-1.gpg', 'new-3.gpg', 'new-home-2', 'new-home-3'):
            self.assertFalse((self.root/name).exists(), name)

    def test_signature_verifies_with_the_committed_keyring(self):
        key, sums = self.keys['a'], self.checksums()
        before = set(Path('/tmp').glob('qmi-sign.*'))
        result = self.sign(sums, key['keyring'], key['secret'], key['passphrase'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(Path('/tmp').glob('qmi-sign.*')), before, 'the temporary GnuPG home was kept')
        self.assertTrue(Path(f'{sums}.asc').read_text().startswith('-----BEGIN PGP SIGNATURE-----'))
        self.assertEqual(self.gpgv(key['keyring'], sums), 0)
        sums.write_text(sums.read_text().replace('0', '1', 1))
        self.assertNotEqual(self.gpgv(key['keyring'], sums), 0)

    def test_unsigned_release_only_without_keyring_and_secret(self):
        sums = self.checksums()
        result = self.sign(sums, self.root/'absent.gpg')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning::', result.stdout)
        self.assertFalse(Path(f'{sums}.asc').exists())

    def test_inconsistent_keys_fail_without_leaving_a_signature(self):
        a, b = self.keys['a'], self.keys['b']
        cases = {
            'keyring without secret': (a['keyring'], None, None),
            'secret without keyring': (self.root/'absent.gpg', a['secret'], a['passphrase']),
            'secret of another key': (b['keyring'], a['secret'], a['passphrase']),
            'wrong passphrase': (a['keyring'], a['secret'], 'not-the-passphrase'),
        }
        for name, (keyring, secret, passphrase) in cases.items():
            with self.subTest(name):
                sums = self.checksums()
                result = self.sign(sums, keyring, secret, passphrase)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(sorted(path.name for path in sums.parent.iterdir()), ['SHA256SUMS'])


@unittest.skipUnless(HAVE_GIT, 'git is required')
class PackageVersionTests(unittest.TestCase):
    """build-deb.sh --print-version in a throwaway git repository."""

    def test_versions_follow_the_tag_convention(self):
        repository = temporary_directory(self)
        (repository/'tools/packaging').mkdir(parents=True)
        shutil.copy2(PACKAGING/'build-deb.sh', repository/'tools/packaging/build-deb.sh')

        def git(*arguments):
            return subprocess.run(['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                                   '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false', *arguments],
                                  cwd=repository, check=True, capture_output=True, text=True).stdout.strip()

        def commit(name):
            (repository/name).write_text(name)
            git('add', name)
            git('commit', '-q', '-m', name)
            return git('rev-parse', '--short', 'HEAD')

        def version(*arguments, **environment):
            env = {key: value for key, value in os.environ.items() if key != 'REVISION'}
            env.update(environment)
            return subprocess.run(['sh', 'tools/packaging/build-deb.sh', '--print-version', *arguments],
                                  cwd=repository, env=env, check=True, capture_output=True,
                                  text=True, timeout=60).stdout.strip()

        git('init', '-q')
        commit('one')
        git('tag', '-a', 'v1.2.0', '-m', 'qmi-supervisor 1.2.0')
        self.assertEqual(version(), '1.2.0-1')
        head = commit('two')
        self.assertEqual(version(), f'1.2.0+1.g{head}-1')
        git('tag', '-a', 'v1.3.0-rc1', '-m', 'qmi-supervisor 1.3.0-rc1')
        self.assertEqual(version(), '1.3.0~rc1-1')
        head = commit('three')
        self.assertEqual(version(), f'1.3.0~rc1+1.g{head}-1')
        git('tag', '-a', 'v1.3.0', '-m', 'qmi-supervisor 1.3.0')
        self.assertEqual(version(), '1.3.0-1')
        self.assertEqual(version(REVISION='2'), '1.3.0-2')
        self.assertEqual(version('1.2.3-4'), '1.2.3-4')


@unittest.skipUnless(HAVE_DPKG_DEB, 'dpkg-deb is required')
class PackageKeyringTests(unittest.TestCase):
    """build-deb.sh run on a temporary copy of the application with an explicit version."""

    def copy_application(self):
        root = temporary_directory(self)
        source = root/'source'
        source.mkdir()
        shutil.copy2(ROOT/'qmi_supervisor.py', source)
        for name in ('README.md', 'INSTALLATION.md', 'CONFIGURATION.md', 'LICENSE'):
            if (ROOT/name).is_file():
                shutil.copy2(ROOT/name, source/name)
        for name in ('modules', 'tools', 'config'):
            shutil.copytree(ROOT/name, source/name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (source/'frontend/out').mkdir(parents=True)
        (source/'frontend/out/index.html').write_text('<!doctype html>\n')
        fake = root/'fake-bin'
        fake.mkdir()
        (fake/'npm').write_text('#!/bin/sh\necho "npm must not run in this test: $*" >&2\nexit 97\n')
        (fake/'npm').chmod(0o755)
        return root, source, fake

    def build(self, root, source, fake, version):
        (source/'frontend/out/.version').write_text(version + '\n')  # U1: the export matches this version
        env = dict(os.environ, OUTPUT_DIR=str(root/'out'), PATH=f'{fake}{os.pathsep}{os.environ["PATH"]}')
        result = subprocess.run(['sh', str(source/'tools/packaging/build-deb.sh'), version], env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        tree = root/'tree'
        subprocess.run(['dpkg-deb', '-x', str(root/'out'/f'qmi-supervisor_{version}_all.deb'), str(tree)],
                       check=True, capture_output=True)
        return result, tree

    def test_package_installs_the_committed_keyring(self):
        root, source, fake = self.copy_application()
        (source/'tools/packaging/keys').mkdir(parents=True, exist_ok=True)
        (source/'tools/packaging/keys/release-signing.gpg').write_bytes(b'test keyring bytes\n')
        result, tree = self.build(root, source, fake, '1.3.0~rc1-1')
        installed = tree/'opt/qmi-supervisor/keys/release-signing.gpg'
        self.assertEqual(installed.read_bytes(), b'test keyring bytes\n')
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        self.assertNotIn('release-signing.gpg is missing', result.stderr)

    def test_package_without_keyring_builds_with_a_warning(self):
        root, source, fake = self.copy_application()
        (source/'tools/packaging/keys/release-signing.gpg').unlink(missing_ok=True)
        result, tree = self.build(root, source, fake, '1.3.0-1')
        self.assertIn('release-signing.gpg is missing', result.stderr)
        self.assertFalse((tree/'opt/qmi-supervisor/keys').exists())


class KeyringDocumentationTests(unittest.TestCase):
    """The committed keyring (if any) and the fingerprint published in INSTALLATION.md agree."""

    def documented_fingerprints(self):
        text = (ROOT/'INSTALLATION.md').read_text()
        lines = re.findall(r'^Release signing key fingerprints?: (.+)$', text, flags=re.M)
        self.assertEqual(len(lines), 1, 'INSTALLATION.md needs exactly one "Release signing key fingerprint:" line')
        if lines[0] == 'not published yet.':
            return None
        self.assertRegex(lines[0], r'^`[0-9A-F]{40}`(?:, `[0-9A-F]{40}`)*\.$')
        return re.findall(r'[0-9A-F]{40}', lines[0])

    def test_installation_guide_publishes_the_fingerprint_of_the_committed_keyring(self):
        documented = self.documented_fingerprints()
        if KEYRING.exists():
            self.assertIsNotNone(documented, 'the keyring is committed: publish its fingerprint in INSTALLATION.md')
            self.assertCountEqual(documented, v4_fingerprints(KEYRING.read_bytes()))
        else:
            self.assertIsNone(documented, 'no keyring is committed: the line must say "not published yet."')

    def test_keys_directory_holds_only_the_readme_and_the_public_keyring(self):
        names = {path.name for path in (PACKAGING/'keys').iterdir()}
        self.assertIn('README.md', names)
        self.assertLessEqual(names, {'README.md', 'release-signing.gpg'})
        if KEYRING.exists():
            data = KEYRING.read_bytes()
            self.assertNotIn(b'PRIVATE KEY', data)
            self.assertTrue(v4_fingerprints(data))  # raises on a private key or a non-version-4 key


class DocumentationTests(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self):
        for name in DOCUMENTS:
            source = ROOT/name
            for line in markdown_lines(source):
                for target in re.findall(r'\]\(([^)\s]+)\)', line):
                    if re.match(r'[a-z][a-z0-9+.-]*:', target):
                        continue
                    with self.subTest(document=name, link=target):
                        path, _, anchor = target.partition('#')
                        resolved = (source.parent/path).resolve() if path else source
                        self.assertTrue(resolved.exists())
                        if anchor and resolved.suffix == '.md':
                            self.assertIn(anchor, anchors(resolved))

    def test_updates_section_explains_signatures_privacy_and_manual_installs(self):
        self.assertNotIn('[[FILL', (ROOT/'INSTALLATION.md').read_text())
        updates = section(ROOT/'INSTALLATION.md', '## Updates')
        for text in ('qmi-supervisor-update', 'SHA256SUMS', 'SHA256SUMS.asc', 'gpgv',
                     '/opt/qmi-supervisor/keys/release-signing.gpg', 'update.require_signature = false',
                     'qmi-supervisor/<version>', 'sudo apt install ./qmi-supervisor_', '--allow-downgrades',
                     'Release signing key fingerprint', '](CONFIGURATION.md#update)',
                     '/var/lib/qmi-supervisor/update/cache', 'sudo qmi-supervisor-update rollback',
                     'sent only to the configured update source'):
            self.assertIn(text, updates)

    def test_packaging_readme_documents_tags_release_files_and_the_key(self):
        text = (PACKAGING/'README.md').read_text()
        for snippet in ('| `v1.3.0-rc1-0-gabc1234` | `1.3.0~rc1-1` |', '--print-version', 'stage-release.sh',
                        'sign-release.sh', 'make-signing-key.sh', 'RELEASE_SIGNING_KEY',
                        'RELEASE_SIGNING_PASSPHRASE', 'SHA256SUMS.asc', 'qmi-supervisor_1.3.0.rc1-1_all.deb',
                        '](../../INSTALLATION.md#updates)', 'Rotation:'):
            self.assertIn(snippet, text)
        self.assertNotIn('only with an explicit tag name', text)
        self.assertIn('| `mode` | `off` |', (ROOT/'CONFIGURATION.md').read_text(),
                      'the documented default is the implemented one')


if __name__ == '__main__':
    unittest.main()
