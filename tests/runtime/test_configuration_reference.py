from dataclasses import asdict
from pathlib import Path
import re
import tempfile
import unittest

from modules.runtime.settings import Settings, load_settings
from modules.runtime.fleet_config import Configuration


class ConfigurationReferenceTests(unittest.TestCase):
    def test_every_validated_option_is_documented_once_in_its_section(self):
        source = (Path(__file__).resolve().parents[2]/'CONFIGURATION.md').read_text()
        actual = {}
        section = None
        for line in source.splitlines():
            match = re.fullmatch(r'## \[([a-z]+)\]', line)
            if match:
                section = match.group(1)
                actual[section] = []
            option = re.match(r'\| `([a-z_]+)` \|', line)
            if option and section:
                actual[section].append(option.group(1))
        expected = asdict(Settings())
        self.assertEqual(set(actual), set(expected))
        for section, values in expected.items():
            self.assertCountEqual(actual[section], list(values), section)

    def test_section_count_sentence_matches_the_settings(self):
        source = (Path(__file__).resolve().parents[2]/'CONFIGURATION.md').read_text()
        words = {6: 'six', 7: 'seven', 8: 'eight', 9: 'nine'}
        self.assertIn(f'Use the {words[len(asdict(Settings()))]} section names below', source)

    def test_signature_option_points_to_the_keyring_and_the_updates_guide(self):
        if 'require_signature' not in asdict(Settings()).get('update', {}):
            self.skipTest('no [update] require_signature option in this tree')
        source = (Path(__file__).resolve().parents[2]/'CONFIGURATION.md').read_text()
        row = next(line for line in source.splitlines() if line.startswith('| `require_signature` |'))
        self.assertIn('/opt/qmi-supervisor/keys/release-signing.gpg', row)
        self.assertIn('](INSTALLATION.md#updates)', row)

    def test_packaged_example_matches_parser_defaults(self):
        path = Path(__file__).resolve().parents[2]/'config/qmi-supervisor.ini.example'
        settings = load_settings(path)
        self.assertFalse(Configuration.load(path).profiles)
        actual, expected = asdict(settings), asdict(Settings())
        self.assertEqual({key: set(value) for key, value in actual.items()},
                         {key: set(value) for key, value in expected.items()})
        for section, values in expected.items():
            for option, value in values.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.assertAlmostEqual(float(actual[section][option]), float(value), msg=f'{section}.{option}')
                else:
                    self.assertEqual(actual[section][option], value, f'{section}.{option}')

    def test_four_objective_examples_and_multimodem_example_pass_real_validation(self):
        source = (Path(__file__).resolve().parents[2]/'CONFIGURATION.md').read_text()
        examples = re.findall(r'```ini\n(.*?)```', source, flags=re.S)
        self.assertEqual(len(examples), 5)
        combinations = set()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'sample.ini'
            for example in examples:
                path.write_text(example)
                settings = load_settings(path)
                configuration = Configuration.load(path)
                if configuration.profiles:
                    self.assertEqual(configuration.profiles['private'].modem.apn, 'tenten')
                combinations.add((settings.modem.mode, settings.telemetry.enabled))
                self.assertFalse(settings.web.enabled)
        self.assertEqual(combinations, {('connect', True), ('connect', False), ('monitor', True), ('monitor', False)})
