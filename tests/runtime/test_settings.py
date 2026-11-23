"""Platform-wide settings: supervisor policy and web runtime limits."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from modules.runtime.fleet_config import Configuration
from modules.runtime.settings import (
    Settings, UpdateSettings, check_update_url, load_settings, validate_settings,
)


class PlatformSettingsTests(unittest.TestCase):
    def test_system_and_web_runtime_limits_load_from_the_ini(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            path.write_text('[system]\nlog_level = DEBUG\n[web]\nenabled = true\nmax_workers = 4\n'
                            'address_refresh = 30\npoll_interval = 5\n')
            settings = load_settings(path)
            self.assertEqual(settings.system.log_level, 'DEBUG')
            self.assertEqual((settings.web.max_workers, settings.web.address_refresh, settings.web.poll_interval),
                             (4, 30.0, 5.0))

    def test_invalid_platform_values_are_rejected(self):
        defaults = Settings()
        for section, values in (
                ('system', {'log_level': 'TRACE'}),
                ('web', {'max_workers': 0}), ('web', {'max_workers': 33}), ('web', {'max_workers': True}),
                ('web', {'address_refresh': 4}), ('web', {'address_refresh': 601}),
                ('web', {'poll_interval': 0.5}), ('web', {'poll_interval': 31})):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    validate_settings(replace(defaults, **{section: replace(getattr(defaults, section), **values)}))

    def test_platform_sections_reject_named_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            for section in ('web', 'system'):
                with self.subTest(section=section):
                    path.write_text(f'[modem]\napn = internet\n[modem:private]\nserial = ue-2\n[{section}:private]\n')
                    with self.assertRaises(ValueError):
                        load_settings(path)

    def test_system_policy_is_written_once_and_shared_by_every_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            path.write_text('[modem]\napn = internet\n[modem:private]\nserial = ue-2\n[system]\nlog_level = WARNING\n')
            configuration = Configuration.load(path)
            self.assertEqual(configuration.profiles['private'].system.log_level, 'WARNING')
            changed = configuration.update('default', replace(configuration.base,
                system=replace(configuration.base.system, log_level='DEBUG')))
            rendered = changed.render()
            self.assertEqual(rendered.count('[system]'), 1)
            self.assertNotIn('[system:private]', rendered)
            self.assertIn('log_level = DEBUG', rendered)
            self.assertEqual(changed.profiles['private'].system.log_level, 'DEBUG')
            # A profile transaction cannot fork the platform section either.
            updated = changed.update('private', replace(changed.profiles['private'],
                system=replace(changed.profiles['private'].system, log_level='ERROR')))
            self.assertEqual(updated.base.system.log_level, 'ERROR')
            self.assertEqual(updated.profiles['private'].system.log_level, 'ERROR')

    def test_update_policy_loads_from_the_ini(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            path.write_text('[update]\nmode = notify\nchannel = prerelease\ncheck_interval_hours = 6\n'
                            'window_start = 23:00\nwindow_end = 01:00\nrequire_signature = false\n'
                            'auto_rollback = false\nallow_downgrade = false\n'
                            'source = http://127.0.0.1:8080/releases\n')
            settings = load_settings(path)
            self.assertEqual(settings.update, UpdateSettings(
                mode='notify', channel='prerelease',
                source='http://127.0.0.1:8080/releases', check_interval_hours=6,
                window_start='23:00', window_end='01:00', require_signature=False,
                auto_rollback=False, allow_downgrade=False))

    def test_invalid_update_values_are_rejected(self):
        defaults = Settings()
        for values in (
                {'mode': 'sometimes'}, {'channel': 'beta'},
                {'check_interval_hours': 0}, {'check_interval_hours': 169}, {'check_interval_hours': True},
                {'window_start': '3:00'}, {'window_start': '24:00'},
                {'window_start': '03:00', 'window_end': '03:00'},
                {'window_start': '03:00', 'window_end': '04:30'},
                {'source': 'http://example.com/x'}, {'source': 'https://user:pw@api.github.com/x'},
                {'source': 'ftp://x/y'}, {'source': 'http://localhost/x'},
                {'source': 'https://example.com/' + 'a' * 600},
                {'require_signature': 'yes'}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    validate_settings(replace(defaults, update=replace(defaults.update, **values)))

    def test_loopback_update_sources_are_valid_configuration(self):
        defaults = Settings()
        for source in ('http://127.0.0.1:8080/x', 'http://[::1]:8080/x'):
            with self.subTest(source=source):
                validated = validate_settings(replace(defaults, update=replace(defaults.update, source=source)))
                self.assertEqual(validated.update.source, source)

    def test_update_section_rejects_named_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            path.write_text('[modem]\napn = internet\n[modem:private]\nserial = ue-2\n[update:private]\n')
            with self.assertRaises(ValueError):
                load_settings(path)

    def test_update_policy_is_written_once_and_shared_by_every_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.ini'
            path.write_text('[modem]\napn = internet\n[modem:private]\nserial = ue-2\n[update]\nmode = notify\n')
            configuration = Configuration.load(path)
            self.assertEqual(configuration.profiles['private'].update.mode, 'notify')
            changed = configuration.update('default', replace(configuration.base,
                update=replace(configuration.base.update, channel='prerelease')))
            rendered = changed.render()
            self.assertEqual(rendered.count('[update]'), 1)
            self.assertNotIn('[update:private]', rendered)
            self.assertIn('channel = prerelease', rendered)
            self.assertEqual(changed.profiles['private'].update.channel, 'prerelease')
            # A profile transaction cannot fork the platform section either.
            updated = changed.update('private', replace(changed.profiles['private'],
                update=replace(changed.profiles['private'].update, mode='auto')))
            self.assertEqual(updated.base.update.mode, 'auto')
            self.assertEqual(updated.profiles['private'].update.mode, 'auto')


class UpdateUrlTests(unittest.TestCase):
    def test_an_uppercase_scheme_is_published_lowercase_for_the_contract(self):
        self.assertEqual(check_update_url('HTTPS://api.github.com/repos/x/releases'),
                         'https://api.github.com/repos/x/releases')
        self.assertEqual(check_update_url('Https://api.github.com/x'), 'https://api.github.com/x')


if __name__ == '__main__':
    unittest.main()
