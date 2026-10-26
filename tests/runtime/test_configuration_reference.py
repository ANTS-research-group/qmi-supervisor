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
