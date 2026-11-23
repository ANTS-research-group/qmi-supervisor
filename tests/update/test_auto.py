"""The automatic-install policy: the window, every skip reason and the start."""
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modules.runtime.settings import Settings, UpdateSettings
from modules.update import auto, units
from modules.update.errors import UpdateError
from modules.update.state import UpdatePaths, new_state, write_state
from tests.update.fakes import FakeClock, FakeUpdateHost
from tests.update.test_install import asset_release

NOW = datetime(2026, 11, 21, 3, 30, 0, tzinfo=timezone.utc)
A, B, C = '1.0.0-1', '1.1.0-1', '1.2.0-1'
FAILED = ('failed', 'rolled_back', 'interrupted')


def settings(**overrides):
    values = {'mode': 'auto', 'channel': 'stable', 'require_signature': True,
              'window_start': '03:00', 'window_end': '05:00'}
    values.update(overrides)
    return Settings(update=UpdateSettings(**values))


def releases(*versions, prerelease=False, installable=True, signature=True):
    return [asset_release(version, prerelease=prerelease, signature=signature)[0]
            for version in versions]


class WindowTests(unittest.TestCase):
    def test_a_daytime_window(self):
        start, end = time(3, 0), time(5, 0)
        self.assertFalse(auto.in_window(time(2, 59), start, end))
        self.assertTrue(auto.in_window(time(3, 0), start, end))
        self.assertTrue(auto.in_window(time(4, 59), start, end))
        self.assertFalse(auto.in_window(time(5, 0), start, end))

    def test_a_window_across_midnight(self):
        start, end = time(23, 0), time(1, 0)
        self.assertTrue(auto.in_window(time(23, 30), start, end))
        self.assertTrue(auto.in_window(time(0, 30), start, end))
        self.assertFalse(auto.in_window(time(1, 30), start, end))
        self.assertFalse(auto.in_window(time(12, 0), start, end))

    def test_equal_bounds_never_match(self):
        moment = time(3, 0)
        self.assertFalse(auto.in_window(moment, moment, moment))

    def test_a_datetime_uses_its_local_time(self):
        start, end = auto.window_bounds(UpdateSettings(window_start='03:00',
                                                       window_end='05:00'))
        self.assertEqual((start, end), (time(3, 0), time(5, 0)))
        self.assertTrue(auto.in_window(datetime(2026, 1, 1, 4, 0), start, end))


class DecisionTests(unittest.TestCase):
    def decision(self, *, state, setting=None, installed=A, lines=(), rollback_ok=True,
                 in_progress=None, now=time(3, 30)):
        return auto.auto_decision(settings=setting or settings(), state=state,
                                  installed=installed, history_lines=list(lines),
                                  rollback_ok=rollback_ok, in_progress=in_progress, now=now)

    def test_the_reasons_are_checked_in_order(self):
        state = {'releases': releases(A, B)}
        self.assertEqual(self.decision(state=state, setting=settings(mode='notify')),
                         ('mode_not_auto', None))
        self.assertEqual(self.decision(state=state, setting=settings(channel='prerelease')),
                         ('channel_not_stable', None))
        self.assertEqual(self.decision(state=state,
                                       setting=settings(require_signature=False)),
                         ('signature_not_required', None))
        self.assertEqual(self.decision(state=state, in_progress={'step': 'install'}),
                         ('in_progress', None))
        self.assertEqual(self.decision(state={'releases': releases(A)},
                                       installed=B), ('no_newer_release', None))
        self.assertEqual(self.decision(state={'releases': releases(A, B)},
                                       rollback_ok=False), ('no_rollback_package', B))
        self.assertEqual(self.decision(state={'releases': releases(A, B)},
                                       now=time(2, 0)), ('outside_window', B))
        self.assertEqual(self.decision(state={'releases': releases(A, B)}), ('started', B))

    def test_the_earlier_reason_wins_when_several_apply(self):
        state = {'releases': releases(A, B)}
        self.assertEqual(self.decision(state=state, setting=settings(mode='notify'),
                                       in_progress={'step': 'install'}), ('mode_not_auto', None))
        self.assertEqual(self.decision(state=state, setting=settings(channel='prerelease'),
                                       in_progress={'step': 'install'}),
                         ('channel_not_stable', None))
        self.assertEqual(self.decision(state={'releases': releases(A)}, rollback_ok=False),
                         ('no_newer_release', None))
        self.assertEqual(self.decision(state=state, in_progress={'step': 'install'},
                                       now=time(2, 0)), ('in_progress', None))
        self.assertEqual(self.decision(state=state, rollback_ok=False, now=time(2, 0)),
                         ('no_rollback_package', B))

    def test_the_highest_candidate_that_failed_is_never_retried(self):
        state = {'releases': releases(A, B, C)}
        failed = {'reason': 'auto', 'result': 'failed', 'to': C}
        self.assertEqual(self.decision(state=state, lines=[failed]), ('failed_before', C))
        # A failure of a lower version does not block the highest one.
        lower = {'reason': 'auto', 'result': 'rolled_back', 'to': B}
        self.assertEqual(self.decision(state=state, lines=[lower]), ('started', C))

    def test_prereleases_and_unsigned_releases_are_ignored(self):
        prerelease = releases(B, prerelease=True)
        unsigned = releases(C, signature=False)
        self.assertEqual(self.decision(state={'releases': prerelease + unsigned}, installed=A),
                         ('no_newer_release', None))

    def test_a_history_failure_of_another_reason_does_not_block(self):
        state = {'releases': releases(A, B)}
        manual = {'reason': 'manual', 'result': 'failed', 'to': B}
        self.assertEqual(self.decision(state=state, lines=[manual]), ('started', B))


class RunAutoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.paths = UpdatePaths(
            config=root / 'etc/qmi-supervisor.ini',
            state_dir=root / 'var/lib/qmi-supervisor/update',
            lock=root / 'run/qmi-supervisor-update.lock',
            requests=root / 'run/qmi-supervisor-update',
            runtime=root / 'run/qmi-supervisor')
        self.paths.config.parent.mkdir(parents=True)
        self.paths.config.write_text('[update]\nmode = auto\n')
        self.clock = FakeClock(NOW)
        self.host = FakeUpdateHost(self.paths.runtime, self.clock, installed=A, online=())

    def state(self, versions=(A, B), **extra):
        document = new_state(A, NOW)
        document['releases'] = releases(*versions)
        document.update(extra)
        write_state(self.paths.state, document, now=NOW)
        return document

    def run_auto(self, document, setting=None, *, now_local=None):
        return auto.run_auto(self.paths, setting or settings(), document, runner=self.host,
                             now_local=now_local or datetime(2026, 11, 21, 3, 30),
                             utcnow=self.clock.utcnow, log=auto.LOG)

    def test_it_starts_the_unit_and_records_the_decision(self):
        document = self.state()
        reason, version = self.run_auto(document)
        self.assertEqual((reason, version), ('started', B))
        self.assertEqual([call for call in self.host.calls if call[0] == 'systemctl'],
                         [['systemctl', 'start', '--no-block', units.install_unit(B)]])
        request = units.request_path(self.paths, B)
        self.assertEqual(json.loads(request.read_text())['reason'], 'auto')
        saved = json.loads(self.paths.state.read_text())
        self.assertEqual(saved['auto']['reason'], 'started')
        self.assertEqual(saved['auto']['version'], B)
        self.assertEqual(saved['auto']['window'], '03:00–05:00')
        self.assertIn('checked_at_utc', saved['auto'])

    def test_a_skip_records_the_reason_and_starts_nothing(self):
        document = self.state()
        reason, version = self.run_auto(document, settings(window_start='06:00',
                                                           window_end='08:00'))
        self.assertEqual((reason, version), ('outside_window', B))
        self.assertEqual([call for call in self.host.calls if call[0] == 'systemctl'], [])
        saved = json.loads(self.paths.state.read_text())
        self.assertEqual(saved['auto']['reason'], 'outside_window')
        self.assertEqual(saved['auto']['window'], '06:00–08:00')

    def test_the_same_skip_reason_is_logged_at_info_only_once(self):
        document = self.state()
        outside = settings(window_start='06:00', window_end='08:00')
        self.run_auto(document, outside)
        document = json.loads(self.paths.state.read_text())
        with self.assertLogs('modules.update.auto', level='DEBUG') as logs:
            self.run_auto(document, outside)
        info = [line for line in logs.output
                if 'outside_window' in line and line.startswith('INFO:')]
        self.assertEqual(info, [])

    def test_a_refused_candidate_is_recorded_instead_of_started(self):
        document = self.state()
        self.host.status = 'iF'
        reason, version = self.run_auto(document)
        self.assertEqual(reason, 'package_state_broken')
        saved = json.loads(self.paths.state.read_text())
        self.assertEqual(saved['auto']['reason'], 'package_state_broken')
        self.assertEqual([call for call in self.host.calls if call[0] == 'systemctl'], [])

    def test_a_unit_that_cannot_start_is_recorded_and_its_request_removed(self):
        document = self.state()
        with patch.object(auto.units_module, 'start_unit',
                          side_effect=UpdateError('resolve', 'unit_start_failed', 'no systemd')):
            reason, version = self.run_auto(document)
        self.assertEqual(reason, 'unit_start_failed')
        saved = json.loads(self.paths.state.read_text())
        self.assertEqual(saved['auto']['reason'], 'unit_start_failed')
        self.assertFalse(units.request_path(self.paths, B).exists())

    def test_a_failed_version_is_not_restarted(self):
        document = self.state()
        # Record an automatic failure of B in the history file itself.
        from modules.update import history
        record = history.make_record(**{
            'id': 'b' * 32, 'request_id': None, 'started_at_utc': NOW.isoformat(),
            'at_utc': NOW.isoformat(), 'duration_s': 1, 'from': A, 'to': B, 'reason': 'auto',
            'result': 'failed', 'step_failed': 'install', 'code': 'install_failed',
            'detail': None, 'rollback_available': False, 'installed_after': A,
            'config_restored': False, 'signed_by': None})
        history.append(self.paths, record)
        reason, version = self.run_auto(document)
        self.assertEqual((reason, version), ('failed_before', B))
        self.assertEqual([call for call in self.host.calls if call[0] == 'systemctl'], [])


if __name__ == '__main__':
    unittest.main()
