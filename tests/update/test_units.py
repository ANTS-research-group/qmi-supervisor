"""Unit names, request files and the start of an updater unit."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from modules.update import units
from modules.update.errors import UpdateError
from modules.update.state import UpdatePaths

NOW = datetime(2026, 11, 21, 3, 10, 2, tzinfo=timezone.utc)

VECTORS = {
    '1.3.0-1': '1.3.0\\x2d1',
    '1.3.0~rc1-1': '1.3.0\\x7erc1\\x2d1',
    '1.2.0+1.g1bfa441-1': '1.2.0\\x2b1.g1bfa441\\x2d1',
    '1:2.0-1': '1:2.0\\x2d1',
    '.x': '\\x2ex',
    'a/b': 'a-b',
    'a b': 'a\\x20b',
}


class EscapeTests(unittest.TestCase):
    def test_the_vectors_match_systemd(self):
        for value, expected in VECTORS.items():
            self.assertEqual(units.escape_instance(value), expected, value)

    @unittest.skipIf(shutil.which('systemd-escape') is None, 'systemd-escape is not installed')
    def test_the_vectors_match_the_real_systemd_escape(self):
        for value in VECTORS:
            result = subprocess.run(['systemd-escape', '--', value], capture_output=True, text=True,
                                    check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), units.escape_instance(value), value)

    def test_install_unit_uses_the_escaped_instance(self):
        self.assertEqual(units.install_unit('1.4.0-1'),
                         'qmi-supervisor-update-install@1.4.0\\x2d1.service')


class RunningInUnitTests(unittest.TestCase):
    UNIT = 'qmi-supervisor-update-install@1.4.0\\x2d1.service'

    def test_cgroup_v2_line(self):
        text = '0::/system.slice/' + self.UNIT + '\n'
        self.assertTrue(units.running_in_unit(self.UNIT, text))

    def test_cgroup_v1_name_systemd_line(self):
        text = '1:name=systemd:/system.slice/' + self.UNIT + '\n'
        self.assertTrue(units.running_in_unit(self.UNIT, text))

    def test_another_unit_is_not_this_unit(self):
        text = '0::/system.slice/qmi-supervisor.service\n'
        self.assertFalse(units.running_in_unit(self.UNIT, text))

    def test_garbage_is_ignored(self):
        self.assertFalse(units.running_in_unit(self.UNIT, 'garbage\n\n::\n0::\n'))


class RequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = UpdatePaths(config=root / 'etc/qmi-supervisor.ini',
                                 state_dir=root / 'var/update',
                                 lock=root / 'run/qmi-supervisor-update.lock',
                                 requests=root / 'run/qmi-supervisor-update',
                                 runtime=root / 'run/qmi-supervisor')

    def utcnow(self, offset=0):
        return NOW + timedelta(seconds=offset)

    def test_a_request_round_trips_and_is_private(self):
        request_id = units.write_request(self.paths, '1.4.0-1', reason='auto',
                                         requested_by='timer', utcnow=self.utcnow)
        path = units.request_path(self.paths, '1.4.0-1')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        document = json.loads(path.read_text())
        self.assertEqual(document['request_id'], request_id)
        self.assertEqual(document['version'], '1.4.0-1')
        self.assertEqual(document['reason'], 'auto')
        self.assertEqual(document['requested_by'], 'timer')
        self.assertEqual(len(request_id), 32)
        consumed = units.take_request(self.paths, '1.4.0-1', utcnow=self.utcnow)
        self.assertEqual(consumed['request_id'], request_id)
        self.assertFalse(path.exists())

    def test_a_stale_request_is_ignored_and_deleted(self):
        units.write_request(self.paths, '1.4.0-1', reason='manual',
                            requested_by='cli', utcnow=lambda: self.utcnow(-601))
        path = units.request_path(self.paths, '1.4.0-1')
        self.assertIsNone(units.take_request(self.paths, '1.4.0-1', utcnow=self.utcnow))
        self.assertFalse(path.exists())

    def test_a_mismatching_request_is_ignored_and_deleted(self):
        path = units.request_path(self.paths, '1.4.0-1')
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, 0o700)
        path.write_text(json.dumps({'schema_version': 1, 'request_id': 'a' * 32,
                                    'version': '9.9.9-1', 'reason': 'manual',
                                    'requested_at_utc': '2026-11-21T03:10:02+00:00',
                                    'requested_by': 'cli'}))
        os.chmod(path, 0o600)
        self.assertIsNone(units.take_request(self.paths, '1.4.0-1', utcnow=self.utcnow))
        self.assertFalse(path.exists())

    def test_pending_request(self):
        self.assertFalse(units.pending_request(self.paths, utcnow=self.utcnow))
        units.write_request(self.paths, '1.4.0-1', reason='manual',
                            requested_by='cli', utcnow=self.utcnow)
        self.assertTrue(units.pending_request(self.paths, utcnow=lambda: self.utcnow(5)))
        self.assertFalse(units.pending_request(self.paths, utcnow=lambda: self.utcnow(121)))


class StartAndLiveTests(unittest.TestCase):
    def test_start_unit_argv_is_exact(self):
        calls = []

        def runner(args, *, timeout=None, env=None):
            calls.append((args, timeout))
            return subprocess.CompletedProcess(args, 0, '', '')

        units.start_unit('qmi-supervisor-update-install@1.4.0\\x2d1.service', runner=runner)
        self.assertEqual(calls, [(['systemctl', 'start', '--no-block',
                                   'qmi-supervisor-update-install@1.4.0\\x2d1.service'], 30)])

    def test_start_unit_failure_is_the_requesters_error(self):
        def runner(args, *, timeout=None, env=None):
            return subprocess.CompletedProcess(args, 1, '', 'line one\nline two\n')

        with self.assertRaises(UpdateError) as caught:
            units.start_unit('qmi-supervisor-update-rollback.service', runner=runner)
        self.assertEqual((caught.exception.step, caught.exception.code), ('resolve', 'unit_start_failed'))
        self.assertEqual(caught.exception.detail, 'line two')

    def test_transaction_live(self):
        tx = {'boot_id': 'boot-1', 'pid': 4242}
        self.assertTrue(units.transaction_live(tx, boot_id='boot-1', pid_alive=lambda pid: True))
        self.assertFalse(units.transaction_live(tx, boot_id='boot-2', pid_alive=lambda pid: True))
        self.assertFalse(units.transaction_live(tx, boot_id='boot-1', pid_alive=lambda pid: False))
        self.assertFalse(units.transaction_live(None, boot_id='boot-1', pid_alive=lambda pid: True))
        self.assertFalse(units.transaction_live({'boot_id': 'boot-1', 'pid': 'x'},
                                                boot_id='boot-1', pid_alive=lambda pid: True))


if __name__ == '__main__':
    unittest.main()
