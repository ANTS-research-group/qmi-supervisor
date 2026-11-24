"""Packaging of the update check: systemd units, wrappers and maintainer scripts."""
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
UNITS = ROOT / 'tools' / 'update'
PACKAGING = ROOT / 'tools' / 'packaging'
DEBIAN = PACKAGING / 'debian'

SERVICE = 'qmi-supervisor-update-check.service'
TIMER = 'qmi-supervisor-update-check.timer'
WRAPPER_LINE = 'exec /usr/bin/python3 -I -B /opt/qmi-supervisor/tools/update/updater.py "$@"'


def read(path):
    return path.read_text(encoding='utf-8')


def unit_values(path):
    """(section, key) -> value, ignoring comments, for a systemd unit file."""
    values = {}
    section = None
    for line in read(path).splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line.startswith(';'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        if '=' in line:
            key, _, value = line.partition('=')
            values[(section, key.strip())] = value.strip()
    return values


def case_branches(text):
    """Map every `label)` branch of every `case` statement to its body lines."""
    branches = {}
    label = None
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r'[A-Za-z0-9_|*-]+\)', stripped):
            label = stripped[:-1]
            branches.setdefault(label, [])
            continue
        if stripped in (';;', 'esac'):
            label = None
            continue
        if label is not None:
            branches[label].append(line)
    return branches


class UpdateUnitTests(unittest.TestCase):
    def test_timer_runs_after_boot_and_every_half_hour(self):
        values = unit_values(UNITS / TIMER)
        self.assertEqual(values[('Timer', 'OnBootSec')], '2min')
        self.assertEqual(values[('Timer', 'OnUnitActiveSec')], '30min')
        self.assertEqual(values[('Timer', 'RandomizedDelaySec')], '5min')
        self.assertEqual(values[('Install', 'WantedBy')], 'timers.target')

    def test_timer_has_no_persistent_key(self):
        # Persistent= has no effect on a monotonic timer (no OnCalendar=).
        self.assertNotRegex(read(UNITS / TIMER), r'(?m)^\s*Persistent\s*=')

    def test_check_service_runs_the_wrapper_once(self):
        values = unit_values(UNITS / SERVICE)
        self.assertEqual(values[('Service', 'Type')], 'oneshot')
        self.assertEqual(values[('Service', 'ExecStart')],
                         '/usr/local/sbin/qmi-supervisor-update check --auto')
        self.assertEqual(values[('Service', 'StateDirectory')], 'qmi-supervisor/update')
        self.assertEqual(values[('Service', 'StateDirectoryMode')], '0755')

    def test_check_service_is_not_enabled_and_never_injects_the_test_source(self):
        text = read(UNITS / SERVICE)
        self.assertNotIn('[Install]', text)
        self.assertNotIn('QMI_SUPERVISOR_UPDATE_TEST_SOURCE', text)

    def test_the_manual_check_unit_runs_plain_check_and_is_packaged(self):
        values = unit_values(UNITS / 'qmi-supervisor-update-check-manual.service')
        self.assertEqual(values[('Service', 'Type')], 'oneshot')
        self.assertEqual(values[('Service', 'ExecStart')],
                         '/usr/local/sbin/qmi-supervisor-update check')
        self.assertNotIn('[Install]', read(UNITS / 'qmi-supervisor-update-check-manual.service'))
        script = read(PACKAGING / 'build-deb.sh')
        self.assertIn('qmi-supervisor-update-check-manual.service', script)

    def test_the_sandboxed_check_can_write_the_run_lock_and_request_directory(self):
        # The updater keeps the single lock and the request files in /run (tmpfs).
        self.assertEqual(unit_values(UNITS / SERVICE)[('Service', 'ReadWritePaths')], '/run')


class BuildDebScriptTests(unittest.TestCase):
    def test_build_deb_stages_the_update_units_and_wrapper(self):
        script = read(PACKAGING / 'build-deb.sh')
        self.assertIn(SERVICE, script)
        self.assertIn(TIMER, script)
        self.assertIn('tools/update/$unit', script)
        self.assertIn(WRAPPER_LINE, script)
        self.assertEqual(script.count(WRAPPER_LINE), 1)
        self.assertIn('qmi-supervisor-update" <<\'WRAPPER\'', script)
        # The wrapper is made executable twice: after creation and after chmod -R.
        self.assertEqual(script.count('"$stage/usr/local/sbin/qmi-supervisor-update"'), 3)


class MaintainerScriptTests(unittest.TestCase):
    def test_postinst_enables_and_starts_the_timer_on_configure(self):
        text = read(DEBIAN / 'postinst')
        configure = text.index('if [ "$1" = configure ]; then')
        restart = text.index('systemctl restart qmi-supervisor.service')
        enable = text.index('systemctl enable qmi-supervisor-update-check.timer')
        start = text.index('systemctl start qmi-supervisor-update-check.timer')
        self.assertLess(configure, enable, 'the timer is enabled inside the configure branch')
        self.assertLess(enable, start)
        self.assertLess(start, restart, 'the timer starts before the supervisor is restarted')
        self.assertEqual(text.count('systemctl start qmi-supervisor-update-check.timer'), 1)

    def test_prerm_stops_the_timer_only_when_removing(self):
        branches = case_branches(read(DEBIAN / 'prerm'))
        stopping = [line for body in branches.values() for line in body
                    if 'systemctl stop qmi-supervisor-update-check' in line]
        self.assertEqual(len(stopping), 1)
        self.assertIn('systemctl stop qmi-supervisor-update-check.timer '
                      'qmi-supervisor-update-check.service', stopping[0])
        self.assertIn(stopping[0], branches['remove|deconfigure'])
        self.assertNotIn('qmi-supervisor-update-check',
                         '\n'.join(branches['remove|upgrade|deconfigure']))
        # Upgrades keep the timer, so no branch that covers upgrade stops it.
        for label, body in branches.items():
            if 'upgrade' in label.split('|') and label != 'remove|deconfigure':
                self.assertNotIn('systemctl stop qmi-supervisor-update-check', '\n'.join(body))

    def test_postrm_purge_removes_the_token_and_remove_keeps_it(self):
        branches = case_branches(read(DEBIAN / 'postrm'))
        purge = '\n'.join(branches['purge'])
        remove = '\n'.join(branches['remove'])
        self.assertIn('rm -f /etc/qmi-supervisor.ini /etc/qmi-supervisor-update-token', purge)
        self.assertNotIn('/etc/qmi-supervisor-update-token', remove)
        for body in (remove, purge):
            self.assertIn('systemctl disable qmi-supervisor-update-check.timer', body)
            self.assertIn('rm -f /etc/systemd/system/timers.target.wants/'
                          'qmi-supervisor-update-check.timer', body)
        self.assertIn('systemctl reset-failed qmi-supervisor-update-check.service', remove)


class ShellSyntaxTests(unittest.TestCase):
    def test_shell_scripts_parse(self):
        for path in (PACKAGING / 'build-deb.sh', DEBIAN / 'postinst',
                     DEBIAN / 'prerm', DEBIAN / 'postrm'):
            result = subprocess.run(['sh', '-n', str(path)], text=True, capture_output=True,
                                    check=False)
            self.assertEqual(result.returncode, 0, f'{path}: {result.stderr}')


@unittest.skipIf(shutil.which('systemd-analyze') is None
                 or not Path('/run/systemd/system').is_dir(),
                 'systemd-analyze and a running systemd are required')
class SystemdVerifyTests(unittest.TestCase):
    def test_units_pass_systemd_analyze_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name in (SERVICE, TIMER):
                text = read(UNITS / name).replace(
                    'ExecStart=/usr/local/sbin/qmi-supervisor-update check --auto',
                    'ExecStart=/bin/true')
                (directory / name).write_text(text, encoding='utf-8')
            result = subprocess.run(
                ['systemd-analyze', 'verify',
                 str(directory / SERVICE), str(directory / TIMER)],
                text=True, capture_output=True, check=False)
            lines = (result.stdout + result.stderr).splitlines()
            ours = [line for line in lines if 'qmi-supervisor-update' in line]
            self.assertEqual(ours, [], '\n'.join(lines))


class PurgeTests(unittest.TestCase):
    def test_purge_removes_the_run_lock_and_request_directory(self):
        purge = '\n'.join(case_branches(read(DEBIAN / 'postrm')).get('purge', []))
        self.assertIn('/run/qmi-supervisor-update', purge)
        self.assertIn('/run/qmi-supervisor-update.lock', purge)


if __name__ == '__main__':
    unittest.main()
