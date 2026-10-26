"""Platform-wide settings: supervisor policy and web runtime limits."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from modules.runtime.fleet_config import Configuration
from modules.runtime.settings import Settings, load_settings, validate_settings


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


if __name__ == '__main__':
    unittest.main()
