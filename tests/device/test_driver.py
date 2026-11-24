"""Driver registry and the read-only Quectel AT companion."""
from pathlib import Path
import unittest
from unittest.mock import patch

from modules.device.drivers import DRIVERS, available_drivers, load_driver, trusted_names
from modules.device.drivers.quectel_at import QuectelAtCompanion
from modules.runtime.settings import Settings


class DriverRegistryTests(unittest.TestCase):
    def test_only_listed_drivers_load(self):
        self.assertIn('qmi', DRIVERS)
        self.assertIn('qmi', available_drivers(Settings()))
        with self.assertRaises(ValueError):
            load_driver('mbim', Settings(), None, '/tmp/state.json')

    def test_trusted_names_are_read_from_the_system_section(self):
        from dataclasses import replace
        settings = replace(Settings(), system=replace(Settings().system,
                                                      trusted_drivers='vendor_x, other'))
        self.assertEqual(trusted_names(settings), ['vendor_x', 'other'])
        with self.assertRaises(ValueError):
            load_driver('vendor_x', settings, None, '/tmp/state.json')


class QuectelCompanionTests(unittest.TestCase):
    def test_only_quectel_hardware_attaches(self):
        self.assertTrue(QuectelAtCompanion.applies_to(
            type('Device', (), {'product': 'RM520N-GL', 'manufacturer': 'Quectel'})()))
        self.assertFalse(QuectelAtCompanion.applies_to(
            type('Device', (), {'product': 'Fibocom Modem', 'manufacturer': 'Fibocom'})()))

    def test_queries_are_parsed_without_writing_state(self):
        companion = QuectelAtCompanion()
        companion.descriptor = 1
        companion.port = '/dev/ttyUSB2'
        answers = {
            'AT+QSIMDET?': '+QSIMDET: 1,1\r\nOK\r\n',
            'AT+QUIMSLOT?': '+QUIMSLOT: 2\r\nOK\r\n',
            'AT+QSIMSTAT?': '+QSIMSTAT: 1,1\r\nOK\r\n',
        }
        with patch('modules.device.drivers.quectel_at._command',
                   side_effect=lambda descriptor, command, deadline: answers[command]):
            result = companion.observe('sim_not_ready')
        self.assertEqual(result['port'], '/dev/ttyUSB2')
        self.assertEqual(result['config'], {'enabled': True, 'level': 1})
        self.assertEqual(result['slot'], 2)
        self.assertEqual(result['inserted'], True)
        self.assertEqual(result['reason'], 'sim_not_ready')

    def test_insertion_is_tri_state(self):
        companion = QuectelAtCompanion()
        companion.descriptor = 1
        companion.port = '/dev/ttyUSB2'
        for status, expected in (('0', False), ('1', True), ('2', None)):
            answers = {'AT+QSIMDET?': 'OK\r\n', 'AT+QUIMSLOT?': 'OK\r\n',
                       'AT+QSIMSTAT?': '+QSIMSTAT: 1,' + status + '\r\nOK\r\n'}
            with patch('modules.device.drivers.quectel_at._command',
                       side_effect=lambda descriptor, command, deadline: answers[command]):
                self.assertIs(companion.observe('open')['inserted'], expected, status)

    def test_an_unparsed_answer_makes_no_claim(self):
        companion = QuectelAtCompanion()
        companion.descriptor = 1
        with patch('modules.device.drivers.quectel_at._command',
                   return_value='ERROR\r\n'):
            self.assertEqual(companion.observe('open'), {})
