"""The two on-demand update units and their staging by the package build."""
import configparser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / 'tools' / 'deployment'
PACKAGING = ROOT / 'tools' / 'packaging'
INSTALL = DEPLOYMENT / 'qmi-supervisor-update-install@.service'
ROLLBACK = DEPLOYMENT / 'qmi-supervisor-update-rollback.service'


def unit(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path, encoding='utf-8')
    return parser


class UpdateInstallUnitTests(unittest.TestCase):
    def setUp(self):
        self.parser = unit(INSTALL)

    def test_it_is_a_non_blocking_oneshot_that_lets_apt_finish(self):
        self.assertEqual(self.parser.get('Service', 'Type'), 'oneshot')
        self.assertEqual(self.parser.get('Service', 'KillMode'), 'process')
        self.assertEqual(self.parser.get('Service', 'TimeoutStartSec'), '60min')

    def test_execstart_uses_the_unescaped_instance_and_from_unit(self):
        value = self.parser.get('Service', 'ExecStart')
        self.assertTrue(value.endswith('install %I --from-unit'), value)
        self.assertIn('/usr/local/sbin/qmi-supervisor-update', value)

    def test_it_is_never_enabled_and_does_not_bind_to_the_service(self):
        text = INSTALL.read_text(encoding='utf-8')
        self.assertNotIn('[Install]', text)
        self.assertNotIn('PartOf', text)
        self.assertNotIn('BindsTo', text)
        self.assertNotIn('Requisite', text)


class UpdateRollbackUnitTests(unittest.TestCase):
    def setUp(self):
        self.parser = unit(ROLLBACK)

    def test_it_is_a_separate_manual_rollback_unit(self):
        self.assertEqual(self.parser.get('Service', 'Type'), 'oneshot')
        self.assertEqual(self.parser.get('Service', 'KillMode'), 'process')
        self.assertEqual(self.parser.get('Service', 'TimeoutStartSec'), '60min')
        value = self.parser.get('Service', 'ExecStart')
        self.assertTrue(value.endswith('rollback --from-unit'), value)

    def test_it_is_never_enabled_and_does_not_bind_to_the_service(self):
        text = ROLLBACK.read_text(encoding='utf-8')
        self.assertNotIn('[Install]', text)
        self.assertNotIn('PartOf', text)
        self.assertNotIn('BindsTo', text)
        self.assertNotIn('Requisite', text)


class BuildDebTests(unittest.TestCase):
    def test_the_build_stages_both_units(self):
        script = (PACKAGING / 'build-deb.sh').read_text(encoding='utf-8')
        self.assertIn('tools/deployment/qmi-supervisor-update-install@.service', script)
        self.assertIn('tools/deployment/qmi-supervisor-update-rollback.service', script)
        self.assertIn('"$stage/etc/systemd/system/"', script)


if __name__ == '__main__':
    unittest.main()
