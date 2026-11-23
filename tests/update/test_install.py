"""The install transaction: resolve, download, verify, prepare, backup, snapshot,
install, restart, health, and the automatic rollback."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

from modules.runtime.settings import Settings, UpdateSettings
from modules.update import install, units as update_units
from modules.update.state import UpdatePaths, new_state, write_state
from tests.update.fakes import FakeClock, FakeOpener, FakeUpdateHost, TEST_FINGERPRINT

NOW = datetime(2026, 11, 21, 3, 10, 2, tzinfo=timezone.utc)
A, B, RC = '1.0.0-1', '1.1.0-1', '2.0.0~rc1-1'


def asset_release(version, *, deb_bytes=None, signature=True, prerelease=False,
                  sums=True, listed_name=None, wrong_digest=False, sums_size=None):
    name = f'qmi-supervisor_{version}_all.deb'
    deb_bytes = deb_bytes if deb_bytes is not None else f'package {version}\n'.encode()
    digest = hashlib.sha256(deb_bytes).hexdigest()
    if wrong_digest:
        digest = '0' * 64
    listed = listed_name if listed_name is not None else name
    sums_bytes = f'{digest}  {listed}\n'.encode()
    base = 'https://example.invalid/' + version
    sums_asset = None
    if sums:
        sums_asset = {'name': 'SHA256SUMS', 'size': sums_size if sums_size is not None else len(sums_bytes),
                      'url': base + '/SHA256SUMS', 'api_url': None}
    sig_asset = ({'name': 'SHA256SUMS.asc', 'size': 14, 'url': base + '/SHA256SUMS.asc',
                  'api_url': None} if signature else None)
    deb = {'name': name, 'version': version, 'size': len(deb_bytes), 'url': base + '/' + name,
           'api_url': None, 'sha256': digest}
    release = {'tag': 'v' + version.split('-')[0], 'version': version.split('-')[0], 'name': 'x',
               'published_utc': None, 'prerelease': prerelease, 'html_url': None, 'notes': '',
               'notes_truncated': False, 'deb': deb, 'sums': sums_asset, 'signature': sig_asset,
               'installable': True, 'reason': None}
    files = {deb['url']: deb_bytes, base + '/SHA256SUMS': sums_bytes}
    if sig_asset:
        files[sig_asset['url']] = b'fake signature'
    return release, files


class StopOnCall(FakeOpener):
    def __init__(self, files, stop):
        super().__init__(files)
        self.stop = stop

    def __call__(self, url, *, accept, timeout):
        self.stop.set()
        return super().__call__(url, accept=accept, timeout=timeout)


class InstallTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = UpdatePaths(
            config=root / 'etc/qmi-supervisor.ini',
            state_dir=root / 'var/lib/qmi-supervisor/update',
            lock=root / 'run/qmi-supervisor-update.lock',
            requests=root / 'run/qmi-supervisor-update',
            runtime=root / 'run/qmi-supervisor',
            keyring=root / 'keys/release-signing.gpg')
        self.paths.config.parent.mkdir(parents=True)
        self.paths.config.write_text('[web]\nenabled = true\n')
        self.paths.keyring.parent.mkdir(parents=True)
        self.paths.keyring.write_bytes(b'binary keyring')
        self.settings = Settings(update=UpdateSettings(
            require_signature=True, auto_rollback=True, allow_downgrade=True))
        self.clock = FakeClock(NOW)
        self.host = FakeUpdateHost(self.paths.runtime, self.clock)
        self.files = {}

    def add_release(self, version, **kwargs):
        release, files = asset_release(version, **kwargs)
        self.files.update(files)
        return release

    def save_state(self, *, installed=A, releases, **extra):
        document = new_state(installed, NOW)
        document['releases'] = list(releases)
        document.update(extra)
        write_state(self.paths.state, document, now=NOW)
        return document

    def run_with_state(self, version, *, reason, opener=None, stop=None, settings=None):
        installer = install.Installer(
            self.paths, settings or self.settings, runner=self.host,
            open_url=opener or FakeOpener(self.files), keyring=self.paths.keyring,
            clock=self.clock.monotonic, utcnow=self.clock.utcnow, sleep=self.clock.sleep,
            stop=stop)
        return installer.run(version, reason=reason, unit=None)

    def run_install(self, version, *, releases, reason='manual', request_id=None, opener=None,
                    stop=None, settings=None):
        self.save_state(releases=releases)
        installer = install.Installer(
            self.paths, settings or self.settings, runner=self.host,
            open_url=opener or FakeOpener(self.files), keyring=self.paths.keyring,
            clock=self.clock.monotonic, utcnow=self.clock.utcnow, sleep=self.clock.sleep,
            stop=stop)
        return installer.run(version, reason=reason, request_id=request_id,
                             unit=update_units.install_unit(version))

    def assert_no_apt(self):
        self.assertEqual([call for call in self.host.calls if call[0] == 'apt-get'], [])

    def assert_installed(self, version):
        self.assertEqual((self.host.status, self.host.installed), ('ii', version))


class HappyPathTests(InstallTestCase):
    def test_install_a_to_b(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        release_b = self.add_release(B)
        steps = []
        real_write = install.state_module.write_state

        def capture(path, document, *, now):
            tx = document.get('in_progress')
            if isinstance(tx, dict):
                steps.append(tx.get('step'))
            return real_write(path, document, now=now)

        with patch.object(install.state_module, 'write_state', side_effect=capture):
            record = self.run_install(B, releases=[release_a, release_b])

        self.assertEqual(record['result'], 'success')
        self.assertEqual((record['from'], record['to']), (A, B))
        self.assertEqual(record['signed_by'], TEST_FINGERPRINT)
        self.assertIsNone(record['step_failed'])
        self.assertIsNone(record['code'])
        self.assertEqual(record['installed_after'], B)
        self.assertEqual(record['rollback_available'], True)

        dense = [step for index, step in enumerate(steps) if index == 0 or step != steps[index - 1]]
        self.assertEqual(dense, ['resolve', 'download', 'verify', 'prepare', 'backup',
                                 'snapshot', 'install', 'restart', 'health'])

        apt = [call for call in self.host.calls if call[0] == 'apt-get']
        self.assertEqual(len(apt), 1)
        self.assertEqual(apt[0][1:], [
            'install', '-y', '--no-install-recommends', '-o', 'DPkg::Lock::Timeout=120',
            '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold',
            '--', str(self.paths.cache / B / f'qmi-supervisor_{B}_all.deb')])

        document = json.loads(self.paths.state.read_text())
        self.assertIsNone(document['in_progress'])
        self.assertEqual(document['last_result']['result'], 'success')
        self.assertEqual(document['rollback']['available'], True)
        self.assertEqual(document['rollback']['version'], A)
        self.assertEqual(document['rollback_ready'], True)
        self.assertEqual(document['current_version'], B)
        self.assertEqual(document['history'][0]['id'], record['id'])

        cache_names = {entry.name for entry in self.paths.cache.iterdir() if entry.is_dir()}
        self.assertEqual(cache_names, {A, B})
        backups = [entry for entry in self.paths.backups.iterdir() if entry.is_dir()]
        self.assertEqual(len(backups), 1)
        meta = json.loads((backups[0] / 'meta.json').read_text())
        self.assertEqual((meta['from'], meta['to']), (A, B))
        self.assertEqual((backups[0] / 'qmi-supervisor.ini').read_text(),
                         '[web]\nenabled = true\n')
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((backups[0] / 'qmi-supervisor.ini').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.paths.cache / B).stat().st_mode), 0o700)
        cached_deb = self.paths.cache / B / f'qmi-supervisor_{B}_all.deb'
        self.assertEqual(stat.S_IMODE(cached_deb.stat().st_mode), 0o600)
        self.assert_installed(B)

    def test_a_request_id_is_recorded(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        release_b = self.add_release(B)
        record = self.run_install(B, releases=[release_a, release_b], request_id='c' * 32)
        self.assertEqual(record['request_id'], 'c' * 32)
        self.assertEqual(json.loads(self.paths.state.read_text())['last_result']['request_id'],
                         'c' * 32)

    def test_a_kept_package_is_reused_by_the_rollback(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        release_b = self.add_release(B)
        self.assertEqual(self.run_install(B, releases=[release_a, release_b])['result'], 'success')
        # A rollback needs no download: both packages are already verified in the cache.
        opener = FakeOpener({})
        before = self.clock.monotonic()
        record = self.run_with_state(A, reason='rollback', opener=opener)
        self.assertEqual(record['result'], 'success')
        self.assertEqual(record['reason'], 'rollback')
        self.assertEqual(opener.urls, [])
        self.assertGreaterEqual(self.clock.monotonic() - before, 30.0)
        self.assertLess(self.clock.monotonic() - before, 45.0)


class ResolveRefusalTests(InstallTestCase):
    def refusal(self, version, *, releases, installed=A, settings=None, host_kwargs=None,
                state_extra=None):
        self.host = FakeUpdateHost(self.paths.runtime, self.clock, installed=installed,
                                   **(host_kwargs or {}))
        document = self.save_state(installed=installed, releases=releases, **(state_extra or {}))
        installer = install.Installer(
            self.paths, settings or self.settings, runner=self.host,
            open_url=FakeOpener(self.files), keyring=self.paths.keyring,
            clock=self.clock.monotonic, utcnow=self.clock.utcnow, sleep=self.clock.sleep)
        record = installer.run(version, reason='manual', unit=None)
        self.assert_no_apt()
        self.assertEqual(json.loads(self.paths.state.read_text())['last_result']['result'], 'failed')
        return record, document

    def test_invalid_version(self):
        release_b = self.add_release(B)
        record, _ = self.refusal('1.3.0/1', releases=[release_b])
        self.assertEqual(record['code'], 'invalid_version')
        self.assertEqual(record['step_failed'], 'resolve')

    def test_not_a_package_install(self):
        release_b = self.add_release(B)
        record, _ = self.refusal(B, releases=[release_b], installed=A,
                                 host_kwargs={'status': 'none'})
        self.assertEqual(record['code'], 'not_package_install')

    def test_package_state_broken(self):
        release_b = self.add_release(B)
        record, _ = self.refusal(B, releases=[release_b], host_kwargs={'status': 'iF'})
        self.assertEqual(record['code'], 'package_state_broken')

    def test_already_installed(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        record, _ = self.refusal(A, releases=[release_a])
        self.assertEqual(record['code'], 'already_installed')

    def test_unknown_version(self):
        release_b = self.add_release(B)
        record, _ = self.refusal('9.9.9-1', releases=[release_b])
        self.assertEqual(record['code'], 'unknown_version')

    def test_no_checksums(self):
        release_b, files = asset_release(B, sums=False)
        self.files.update(files)
        record, _ = self.refusal(B, releases=[release_b])
        self.assertEqual(record['code'], 'no_checksums')

    def test_unsigned_only_when_signatures_are_required(self):
        release_b, files = asset_release(B, signature=False)
        self.files.update(files)
        record, _ = self.refusal(B, releases=[release_b])
        self.assertEqual(record['code'], 'unsigned')
        permissive = Settings(update=UpdateSettings(require_signature=False, auto_rollback=True,
                                                    allow_downgrade=True))
        self.host = FakeUpdateHost(self.paths.runtime, self.clock)
        document = self.save_state(releases=[release_b])
        installer = install.Installer(self.paths, permissive, runner=self.host,
                                      open_url=FakeOpener(self.files), keyring=self.paths.keyring,
                                      clock=self.clock.monotonic, utcnow=self.clock.utcnow,
                                      sleep=self.clock.sleep)
        self.assertEqual(installer.run(B, reason='manual')['result'], 'success')

    def test_prerelease_needs_the_prerelease_channel(self):
        release_rc = self.add_release(RC, prerelease=True)
        record, _ = self.refusal(RC, releases=[release_rc])
        self.assertEqual(record['code'], 'prerelease_channel')

    def test_downgrade_is_disabled_by_policy(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        policy = Settings(update=UpdateSettings(require_signature=True, auto_rollback=True,
                                                allow_downgrade=False))
        record, _ = self.refusal(A, releases=[release_a], installed='2.0.0-1', settings=policy)
        self.assertEqual(record['code'], 'downgrade_disabled')

    def test_a_confirmed_downgrade_passes_allow_downgrades(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        self.host = FakeUpdateHost(self.paths.runtime, self.clock, installed='2.0.0-1')
        self.save_state(installed='2.0.0-1', releases=[release_a])
        installer = install.Installer(self.paths, self.settings, runner=self.host,
                                      open_url=FakeOpener(self.files), keyring=self.paths.keyring,
                                      clock=self.clock.monotonic, utcnow=self.clock.utcnow,
                                      sleep=self.clock.sleep)
        record = installer.run(A, reason='manual')
        self.assertEqual(record['result'], 'success')
        apt = [call for call in self.host.calls if call[0] == 'apt-get']
        self.assertEqual(len(apt), 1)
        self.assertIn('--allow-downgrades', apt[0])
        self.assert_installed(A)


class DownloadAndVerifyFailureTests(InstallTestCase):
    def transaction(self, release_b, *, settings=None):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        return self.run_install(B, releases=[release_a, release_b], settings=settings)

    def assert_untouched(self):
        self.assert_installed(A)
        self.assertEqual(self.host.main_pid, 4242)
        self.assert_no_apt()

    def test_http_error(self):
        release_b = self.add_release(B)
        self.files[release_b['deb']['url']] = urllib.error.HTTPError(
            release_b['deb']['url'], 404, 'Not Found', {}, None)
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'download_failed')
        self.assertNotIn('https://', json.dumps(record))
        self.assert_untouched()

    def test_oversize_download(self):
        release_b = self.add_release(B, sums_size=4)
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'download_too_large')
        self.assert_untouched()

    def test_checksum_mismatch_deletes_the_package(self):
        release_b = self.add_release(B, wrong_digest=True)
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'checksum_mismatch')
        self.assertFalse((self.paths.cache / B / f'qmi-supervisor_{B}_all.deb').exists())
        self.assert_untouched()

    def test_checksum_missing(self):
        release_b = self.add_release(B, listed_name='some-other.deb')
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'checksum_missing')
        self.assert_untouched()

    def test_bad_signature(self):
        release_b = self.add_release(B)
        self.host.gpgv = 'bad'
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'bad_signature')
        self.assert_untouched()

    def test_missing_keyring_stops_before_download(self):
        release_b = self.add_release(B)
        self.paths.keyring.unlink()
        self.save_state(releases=[release_b])
        opener = FakeOpener(self.files)
        installer = install.Installer(
            self.paths, self.settings, runner=self.host, open_url=opener,
            keyring=self.paths.keyring, clock=self.clock.monotonic,
            utcnow=self.clock.utcnow, sleep=self.clock.sleep)
        record = installer.run(B, reason='manual')
        self.assertEqual(record['code'], 'no_keyring')
        self.assertEqual([url for url in opener.urls if url.endswith('.deb')], [],
                         'the package must not be requested without a keyring')
        self.assert_untouched()

    def test_package_mismatch(self):
        release_b = self.add_release(B)
        deb = self.paths.cache / B / f'qmi-supervisor_{B}_all.deb'
        self.host.packages[str(deb)] = {'Package': 'qmi-supervisor', 'Version': '9.9.9-1',
                                        'Architecture': 'all'}
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'package_mismatch')
        self.assert_untouched()

    def test_package_incomplete(self):
        release_b = self.add_release(B)
        self.host.incomplete = True
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'package_incomplete')
        self.assert_untouched()

    def test_config_incompatible(self):
        release_b = self.add_release(B)
        self.host.config_rc = 2
        record = self.transaction(release_b)
        self.assertEqual(record['code'], 'config_incompatible')
        self.assert_untouched()


class InstallAndRollbackFailureTests(InstallTestCase):
    def releases(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        return [release_a, self.add_release(B)]

    def test_installed_version_not_a_release_proceeds_without_rollback(self):
        releases = self.releases()[1:]  # only B is known; A came from somewhere else
        self.host.after_install = {'service': 'failed'}
        record = self.run_install(B, releases=releases)
        self.assertEqual(record['result'], 'failed')
        self.assertEqual(record['code'], 'health_failed')
        self.assertEqual(record['installed_after'], B)
        self.assertFalse(record['rollback_available'])
        self.assertNotIn('--allow-downgrades',
                         [arg for call in self.host.calls if call[0] == 'apt-get' for arg in call])
        self.assertEqual([call for call in self.host.calls if call[0] == 'systemctl'
                          and 'stop' in call], [])

    def test_dpkg_busy_changes_nothing(self):
        self.host.install_mode = 'lock'
        record = self.run_install(B, releases=self.releases())
        self.assertEqual((record['result'], record['code']), ('failed', 'dpkg_busy'))
        self.assert_installed(A)
        self.assertEqual(len([call for call in self.host.calls if call[0] == 'apt-get']), 1)

    def test_install_failure_changes_nothing(self):
        self.host.install_mode = 'error'
        record = self.run_install(B, releases=self.releases())
        self.assertEqual((record['result'], record['code']), ('failed', 'install_failed'))
        self.assert_installed(A)
        self.assertEqual(len([call for call in self.host.calls if call[0] == 'apt-get']), 1)

    def test_a_broken_install_is_retried_with_dpkg(self):
        self.host.install_mode = 'broken'
        record = self.run_install(B, releases=self.releases())
        self.assertEqual((record['result'], record['code']), ('failed', 'rollback_failed'))
        apt = [call for call in self.host.calls if call[0] == 'apt-get']
        self.assertEqual(len(apt), 2)
        self.assertIn('--allow-downgrades', apt[1])
        self.assertIn(str(self.paths.cache / A / f'qmi-supervisor_{A}_all.deb'), apt[1])
        dpkg = [call for call in self.host.calls if call[0] == 'dpkg' and '-i' in call]
        self.assertEqual(len(dpkg), 1)

    def test_health_failure_rolls_back_and_restores_the_configuration(self):
        original = self.paths.config.read_text()

        def change_config(host, version):
            if version == B:
                self.paths.config.write_text('[update]\nmode = notify\n')

        self.host.install_hook = change_config
        self.host.after_install_steps = [{'service': 'failed'}, {'service': 'active'}]
        record = self.run_install(B, releases=self.releases())
        self.assertEqual((record['result'], record['code']), ('rolled_back', 'health_failed'))
        self.assertEqual(record['step_failed'], 'restart')
        self.assertTrue(record['config_restored'])
        self.assert_installed(A)
        self.assertEqual(self.paths.config.read_text(), original)
        backups = [entry for entry in self.paths.backups.iterdir() if entry.is_dir()]
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / 'qmi-supervisor.ini.rejected').exists())
        calls = self.host.calls
        stop_index = next(index for index, call in enumerate(calls)
                          if call[0] == 'systemctl' and 'stop' in call)
        rollback_apt = next(index for index, call in enumerate(calls)
                            if call[0] == 'apt-get' and '--allow-downgrades' in call)
        self.assertLess(stop_index, rollback_apt)

    def test_an_unchanged_configuration_is_not_rewritten(self):
        self.host.after_install_steps = [{'service': 'failed'}, {'service': 'active'}]
        record = self.run_install(B, releases=self.releases())
        self.assertEqual(record['result'], 'rolled_back')
        self.assertFalse(record['config_restored'])
        backups = [entry for entry in self.paths.backups.iterdir() if entry.is_dir()]
        self.assertFalse((backups[0] / 'qmi-supervisor.ini.rejected').exists())

    def test_modems_that_do_not_come_back_roll_back(self):
        self.host.after_install_steps = [{'online': ()}, {'online': ()}]
        record = self.run_install(B, releases=self.releases())
        self.assertEqual((record['result'], record['code']), ('rolled_back', 'modems_not_back'))
        self.assertEqual(record['step_failed'], 'health')
        self.assertIn('default', record['detail'])
        self.assert_installed(A)

    def test_auto_rollback_off_does_not_wait_for_modems(self):
        self.settings = Settings(update=UpdateSettings(require_signature=True, auto_rollback=False,
                                                       allow_downgrade=True))
        self.host.after_install_steps = [{'online': ()}]
        record = self.run_install(B, releases=self.releases())
        self.assertEqual(record['result'], 'success')
        self.assertLess(self.clock.monotonic() - 1000.0, 600)

    def test_a_failed_rollback_install_starts_the_service(self):
        self.host.install_modes = ['ok', 'error', 'error']
        self.host.after_install_steps = [{'service': 'failed'}, {'service': 'active'}]
        record = self.run_install(B, releases=self.releases())
        self.assertEqual((record['result'], record['code']), ('failed', 'rollback_failed'))
        self.assertEqual(record['step_failed'], 'rollback')
        self.assertIn('after health_failed:', record['detail'])
        self.assertTrue(any(call[0] == 'systemctl' and call[1] == 'start'
                            and call[-1] == 'qmi-supervisor.service' for call in self.host.calls))

    def test_a_failed_rollback_health_check_is_rollback_failed(self):
        # The rollback package installs and the service starts, but the web worker
        # never returns: the rollback's own health check fails.
        self.host.after_install_steps = [{'service': 'failed'}, {'web': False}]
        record = self.run_install(B, releases=self.releases())
        self.assertEqual((record['result'], record['code']), ('failed', 'rollback_failed'))
        self.assertEqual(record['step_failed'], 'rollback')


class ChecksumNameTests(unittest.TestCase):
    def test_the_published_dotted_name_and_the_local_tilde_name_are_both_found(self):
        version = '1.3.0~rc1-1'
        dotted = 'qmi-supervisor_1.3.0.rc1-1_all.deb'
        local = 'qmi-supervisor_1.3.0~rc1-1_all.deb'
        self.assertEqual(install.checksum_name({dotted: 'x'}, version), dotted)
        self.assertEqual(install.checksum_name({local: 'x'}, version), local)
        self.assertEqual(install.checksum_name({dotted: 'x'}, version, dotted), dotted)
        self.assertIsNone(install.checksum_name({'other.deb': 'x'}, version))


class RollbackAndRecoveryTests(InstallTestCase):
    def releases(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        return [release_a, self.add_release(B)]

    def test_a_failed_attempt_keeps_the_previous_rollback_package(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        release_b = self.add_release(B)
        release_c = self.add_release('1.2.0-1')
        self.assertEqual(self.run_install(B, releases=[release_a, release_b])['result'], 'success')
        document = json.loads(self.paths.state.read_text())
        self.assertEqual(document['rollback']['version'], A)
        document['releases'] = [release_a, release_b, release_c]
        write_state(self.paths.state, document, now=NOW)
        self.host.install_mode = 'lock'
        record = self.run_with_state('1.2.0-1', reason='manual')
        self.assertEqual((record['result'], record['code']), ('failed', 'dpkg_busy'))
        document = json.loads(self.paths.state.read_text())
        self.assertEqual(document['rollback']['version'], A,
                         'a retryable failure must not forget the kept version')
        self.assertTrue((self.paths.cache / A).is_dir(), 'and its verified package stays')

    def test_a_failed_configuration_restore_starts_the_service(self):
        self.host.after_install_steps = [{'service': 'failed'}]
        with patch.object(install.Installer, '_restore_config',
                          side_effect=install.UpdateError('rollback', 'rollback_failed', 'missing')):
            record = self.run_install(B, releases=self.releases())
        self.assertEqual(record['code'], 'rollback_failed')
        self.assertIn(['systemctl', 'start', 'qmi-supervisor.service'], self.host.calls,
                      'a failed rollback must not leave the service stopped')

    def test_a_failed_repair_keeps_the_package_its_hint_names(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        release_b = self.add_release(B)
        self.assertEqual(self.run_install(B, releases=[release_a, release_b])['result'], 'success')
        self.host.status, self.host.installed = 'iF', B
        self.host.install_mode = 'error'
        record = self.run_with_state(A, reason='repair')
        self.assertEqual(record['code'], 'repair_failed')
        document = json.loads(self.paths.state.read_text())
        package = self.paths.cache / A / f'qmi-supervisor_{A}_all.deb'
        self.assertIn(str(package), document['repair']['manual_command'])
        self.assertTrue(package.is_file(), 'the hint must name a file that still exists')

    def test_a_backward_clock_does_not_prune_this_transactions_backup(self):
        self.paths.backups.mkdir(parents=True, mode=0o700)
        future = '20990101T000000Z-aaaaaaaa'
        for index in range(5):
            directory = self.paths.backups / f'{future[:-8]}{index:08d}'
            directory.mkdir(parents=True)
        record = self.run_install(B, releases=self.releases())
        self.assertEqual(record['result'], 'success', record)
        backups = [entry for entry in self.paths.backups.iterdir() if entry.is_dir()]
        self.assertEqual(len(backups), 5)
        meta = [json.loads((entry / 'meta.json').read_text()) for entry in backups
                if (entry / 'meta.json').exists()]
        self.assertIn(B, [item['to'] for item in meta], 'the new backup survives its own pruning')

    def test_a_repair_can_use_a_cached_package_when_the_package_is_absent(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        release_b = self.add_release(B)
        self.assertEqual(self.run_install(B, releases=[release_a, release_b])['result'], 'success')
        document = json.loads(self.paths.state.read_text())
        plan = install.resolve(self.paths, self.settings, document, A, reason='repair',
                               installed=None)
        self.assertEqual((plan.version, plan.reason, plan.downgrade), (A, 'repair', False))
        self.assertIsNotNone(plan.cached)

    def test_the_health_budget_starts_after_apt_finishes(self):
        seen = []
        real = install.health.wait_service
        # apt takes 300 s; the health budget must start there, not at its start.
        self.host.install_hook = lambda host, version: self.clock.sleep(300)

        def capture(runtime, runner, *, after, deadline_from=None, **kwargs):
            seen.append((after, deadline_from))
            return None

        with patch.object(install.health, 'wait_service', side_effect=capture):
            record = self.run_install(B, releases=self.releases())
        self.assertEqual(record['result'], 'success', record)
        self.assertEqual(len(seen), 1)
        after, deadline_from = seen[0]
        self.assertLess(after, 1300.0, 'the transaction start, not the apt end')
        self.assertGreaterEqual(deadline_from, 1300.0, 'the health budget starts when apt ends')


class InterruptionAndCrashTests(InstallTestCase):
    def releases(self):
        release_a, files_a = asset_release(A)
        self.files.update(files_a)
        return [release_a, self.add_release(B)]

    def test_stop_during_download_interrupts_without_apt(self):
        stop = threading.Event()
        releases = self.releases()
        record = self.run_install(B, releases=releases, opener=StopOnCall(self.files, stop),
                                  stop=stop)
        self.assertEqual((record['result'], record['code']), ('interrupted', 'interrupted'))
        self.assertEqual(record['step_failed'], 'download')
        self.assert_no_apt()

    def test_stop_during_apt_lets_it_finish_then_interrupts_at_install(self):
        stop = threading.Event()
        self.host.install_hook = lambda host, version: stop.set()
        record = self.run_install(B, releases=self.releases(), stop=stop)
        self.assertEqual((record['result'], record['code']), ('interrupted', 'interrupted'))
        self.assertEqual(record['step_failed'], 'install')
        self.assertEqual(record['installed_after'], B)
        self.assertLess(self.clock.monotonic() - 1000.0, 30, 'no health wait')

    def test_a_crash_during_health_is_an_internal_error_and_rolls_back(self):
        self.host.after_install_steps = [{'service': 'active'}, {'service': 'active'}]
        real = install.health.wait_service
        seen = []

        def flaky(*args, **kwargs):
            if not seen:
                seen.append(True)
                raise RuntimeError('boom')
            return real(*args, **kwargs)

        with patch.object(install.health, 'wait_service', side_effect=flaky):
            record = self.run_install(B, releases=self.releases())
        self.assertEqual(record['code'], 'internal_error')
        self.assertEqual(record['result'], 'rolled_back')
        self.assert_installed(A)

    def test_privacy_of_the_detail(self):
        self.host.install_mode = 'error'
        self.host.install_stderr = 'E: fetch https://example.invalid/x failed for 123456789012345'
        record = self.run_install(B, releases=self.releases())
        self.assertEqual(record['code'], 'install_failed')
        history_text = self.paths.history.read_text()
        last_result = json.loads(self.paths.state.read_text())['last_result']
        self.assertNotIn('https://example.invalid/x', json.dumps(last_result))
        self.assertNotIn('123456789012345', history_text)
        self.assertNotIn('123456789012345', json.dumps(last_result))
        self.assertIn('<url>', history_text)
        self.assertIn('<number>', history_text)


if __name__ == '__main__':
    unittest.main()
