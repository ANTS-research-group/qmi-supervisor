import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from modules.device.attach_apn import AttachApn
    from gi.repository import GLib, Qmi
except (ImportError, ValueError):
    AttachApn = None

from modules.device.device import Device


@unittest.skipIf(AttachApn is None, 'Requires OS libqmi GI bindings')
class AttachApnTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'devnum').write_text('7')
        self.device = Device('/dev/fake', 'wwan0', str(self.root), 'modem-a', 'fake')
        self.path = self.root/'attach.json'
        self.current, self.pending = [7], None
        self.apn, self.mask, self.profile_name = '', Qmi.WdsApnTypeMask.DEFAULT, 'default'
        self.calls, self.modifications, self.errors = [], [], {}
        self.readback = None
        self.other_parameters = {'pdp_type': 'ipv4v6', 'authentication': 'none', 'username': 'retain'}
        self.session = SimpleNamespace(request=self.request)
        self.helper = AttachApn(self.session, self.device, self.path)

    def request(self, service, name, value=None):
        self.assertEqual(service, Qmi.Service.WDS)
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]
        if name == 'get_lte_attach_pdn_list':
            return SimpleNamespace(get_current_list=lambda: self.current, get_pending_list=lambda: self.pending)
        if name == 'get_profile_settings':
            profile_type, index = value.get_profile_id()
            self.assertEqual((int(profile_type), index), (0, self.current[0]))
            apn = self.readback if self.modifications and self.readback is not None else self.apn
            return SimpleNamespace(get_apn_name=lambda: apn, get_apn_type_mask=lambda: self.mask,
                                   get_profile_name=lambda: self.profile_name)
        if name == 'modify_profile':
            profile_type, index = value.get_profile_identifier()
            self.assertEqual((int(profile_type), index), (0, self.current[0]))
            journal = json.loads(self.path.read_text())
            self.assertEqual((journal['profile_id'], journal['requested_apn']), (index, 'internet'))
            # These real GI getters fail unless those TLVs were explicitly set.
            # Profile type/PDP/auth/credentials must survive the APN edit.
            with self.assertRaises(GLib.Error):
                value.get_apn_type_mask()
            with self.assertRaises(GLib.Error):
                value.get_pdp_type()
            self.apn = value.get_apn_name()
            self.modifications.append(value)
            return SimpleNamespace()
        raise AssertionError('Unexpected request: ' + name)

    def pending_journal(self):
        self.helper.ensure('internet')
        return json.loads(self.path.read_text())

    def test_current_default_profile_only_apn_is_changed_after_journal_and_rf_callback(self):
        def lower_rf():
            journal = json.loads(self.path.read_text())
            self.assertEqual((journal['identity'], journal['enumeration']), ('modem-a', '7'))
            self.assertFalse(self.modifications)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        before = dict(self.other_parameters)
        callback = Mock(side_effect=lower_rf)
        report = self.helper.ensure('internet', before_change=callback)
        self.assertEqual(report['status'], 'synchronized')
        self.assertEqual((report['profile_id'], report['profile_apn']), (7, 'internet'))
        self.assertTrue(report['changed'])
        self.assertTrue(report['needs_reregister'])
        self.assertEqual(self.calls, ['get_lte_attach_pdn_list', 'get_profile_settings',
                                     'modify_profile', 'get_profile_settings'])
        self.assertEqual(self.other_parameters, before)
        self.assertEqual(self.current, [7])
        callback.assert_called_once_with()
        self.assertTrue(self.path.exists())
        self.assertTrue(self.helper.mark_completed())
        self.assertFalse(self.path.exists())

    def test_already_matching_profile_has_no_write_no_rf_and_no_checkpoint(self):
        self.apn = 'INTERNET'
        before = Mock()
        report = self.helper.ensure('internet', before_change=before)
        self.assertEqual(report['status'], 'matched')
        self.assertFalse(report['needs_reregister'])
        self.assertFalse(self.modifications)
        self.assertFalse(self.path.exists())
        before.assert_not_called()

    def test_crash_pending_with_apn_already_stored_still_requires_registration_cycle(self):
        journal = self.pending_journal()
        restarted = AttachApn(self.session, self.device, self.path, enumeration='7')
        before = Mock()
        count = len(self.modifications)
        report = restarted.ensure('internet', before_change=before)
        self.assertEqual(report['status'], 'pending_reregister')
        self.assertTrue(report['recovered_pending'])
        self.assertTrue(report['needs_reregister'])
        self.assertEqual(len(self.modifications), count)
        self.assertEqual(json.loads(self.path.read_text())['token'], journal['token'])
        before.assert_called_once_with()
        self.assertTrue(restarted.mark_completed())

    def test_other_modem_pending_is_not_applied_or_completed(self):
        journal = self.pending_journal()
        replacement = Device('/dev/other', 'wwan1', str(self.root), 'modem-b', 'replacement')
        helper = AttachApn(self.session, replacement, self.path)
        before = Mock()
        report = helper.ensure('internet', before_change=before)
        self.assertTrue(report['ignored_pending'])
        self.assertFalse(report['needs_reregister'])
        self.assertEqual(json.loads(self.path.read_text())['token'], journal['token'])
        self.assertFalse(helper.mark_completed())
        before.assert_not_called()

    def test_new_transaction_for_replacement_uses_only_its_own_requested_apn(self):
        previous = self.pending_journal()
        replacement = Device('/dev/other', 'wwan1', str(self.root), 'modem-b', 'replacement')
        self.apn = 'replacement-old'
        helper = AttachApn(self.session, replacement, self.path)
        report = helper.ensure('internet')
        current = json.loads(self.path.read_text())
        self.assertTrue(report['ignored_pending'])
        self.assertTrue(report['replaced_pending'])
        self.assertEqual(current['identity'], 'modem-b')
        self.assertNotEqual(current['token'], previous['token'])
        self.assertFalse(self.helper.mark_completed())  # An old helper cannot erase the new journal.

    def test_new_usb_enumeration_does_not_apply_old_pending_to_matching_profile(self):
        journal = self.pending_journal()
        (self.root/'devnum').write_text('8')
        helper = AttachApn(self.session, self.device, self.path)
        report = helper.ensure('internet')
        self.assertTrue(report['ignored_pending'])
        self.assertFalse(report['needs_reregister'])
        self.assertEqual(json.loads(self.path.read_text())['token'], journal['token'])

    def test_pending_only_or_absent_current_never_guesses_a_profile(self):
        for current, pending, status in ((None, [4], 'fallback_pending_only'),
                                         ([], None, 'fallback_no_current_profile')):
            with self.subTest(status=status):
                self.current, self.pending, self.calls = current, pending, []
                report = self.helper.ensure('internet')
                self.assertEqual(report['status'], status)
                self.assertIsNone(report['profile_id'])
                self.assertEqual(self.calls, ['get_lte_attach_pdn_list'])

    def test_conflicting_pending_list_is_reported_without_using_it(self):
        self.pending = [4]
        report = self.helper.ensure('internet')
        self.assertEqual(report['status'], 'fallback_pending_list_conflict')
        self.assertEqual((report['current_profiles'], report['pending_profiles']), ([7], [4]))
        self.assertEqual(self.calls, ['get_lte_attach_pdn_list'])

    def test_ims_emergency_and_nondefault_profiles_are_not_modified(self):
        for mask, name, apn in ((Qmi.WdsApnTypeMask.IMS, 'default', ''),
                (Qmi.WdsApnTypeMask.DEFAULT | Qmi.WdsApnTypeMask.EMERGENCY, 'default', ''),
                (Qmi.WdsApnTypeMask.DEFAULT, 'IMS', ''),
                (Qmi.WdsApnTypeMask.DEFAULT, 'default', 'sos'), (0, 'internet', 'internet')):
            with self.subTest(mask=mask, name=name, apn=apn):
                self.mask, self.profile_name, self.apn = mask, name, apn
                callback = Mock()
                report = self.helper.ensure('internet', before_change=callback)
                self.assertEqual(report['status'], 'fallback_unsafe_profile')
                callback.assert_not_called()
                self.assertFalse(self.modifications)
                self.assertFalse(self.path.exists())

    def test_missing_profile_type_is_not_assumed_default(self):
        self.mask = None
        self.assertEqual(self.helper.ensure('internet')['status'], 'fallback_profile_type_unavailable')
        self.assertFalse(self.modifications)

    def test_known_unsupported_information_errors_fallback_but_transport_failure_raises(self):
        for code, status in ((Qmi.ProtocolError.NOTSUPPORTED, 'fallback_unsupported'),
                             (Qmi.ProtocolError.INFORMATIONUNAVAILABLE, 'fallback_information_unavailable')):
            with self.subTest(code=code):
                self.errors['get_lte_attach_pdn_list'] = GLib.Error.new_literal(
                    Qmi.protocol_error_quark(), 'Unavailable', code)
                self.assertEqual(self.helper.ensure('internet')['status'], status)
                self.assertFalse(self.path.exists())
        self.errors['get_lte_attach_pdn_list'] = TimeoutError('Transport timeout')
        with self.assertRaises(TimeoutError):
            self.helper.ensure('internet')

    def test_failed_mutation_or_rf_callback_leaves_journal_for_retry(self):
        self.errors['modify_profile'] = TimeoutError('Unknown write result')
        with self.assertRaises(TimeoutError):
            self.helper.ensure('internet')
        self.assertTrue(self.path.exists())
        self.assertEqual(self.apn, '')
        self.errors.clear()
        with self.assertRaisesRegex(RuntimeError, 'RF failed'):
            self.helper.ensure('internet', before_change=Mock(side_effect=RuntimeError('RF failed')))
        self.assertTrue(self.path.exists())
        self.assertFalse(self.modifications)

    def test_readback_mismatch_keeps_pending_checkpoint(self):
        self.readback = 'unexpected'
        with self.assertRaisesRegex(RuntimeError, 'readback did not match'):
            self.helper.ensure('internet')
        self.assertTrue(self.path.exists())
        self.assertFalse(self.helper.mark_completed())

    def test_unsupported_modify_after_journaling_is_not_inline_fallback(self):
        self.errors['modify_profile'] = GLib.Error.new_literal(
            Qmi.protocol_error_quark(), 'Unsupported mutation', Qmi.ProtocolError.NOTSUPPORTED)
        with self.assertRaisesRegex(RuntimeError, 'transaction could not be confirmed'):
            self.helper.ensure('internet')
        self.assertTrue(self.path.exists())
        self.assertFalse(self.helper.mark_completed())

    def test_owned_pending_cannot_silently_fallback_on_unsupported_or_conflicting_profile(self):
        previous = self.pending_journal()
        for failure in ('unsupported', 'conflict', 'missing_type', 'changed_profile'):
            with self.subTest(failure=failure):
                self.errors.clear()
                self.pending, self.mask, self.current = None, Qmi.WdsApnTypeMask.DEFAULT, [7]
                if failure == 'unsupported':
                    self.errors['get_lte_attach_pdn_list'] = GLib.Error.new_literal(
                        Qmi.protocol_error_quark(), 'Unavailable', Qmi.ProtocolError.NOTSUPPORTED)
                elif failure == 'conflict':
                    self.pending = [4]
                elif failure == 'missing_type':
                    self.mask = None
                else:
                    self.current = [8]
                with self.assertRaisesRegex(RuntimeError, 'checkpoint was retained'):
                    self.helper.ensure('internet')
                self.assertEqual(json.loads(self.path.read_text())['token'], previous['token'])
                self.assertFalse(self.helper.mark_completed())

    def test_unavailable_readback_after_write_is_not_inline_fallback(self):
        request = self.session.request
        def unavailable_after_modify(service, name, value=None):
            if name == 'get_profile_settings' and self.modifications:
                raise GLib.Error.new_literal(Qmi.protocol_error_quark(), 'Readback unavailable',
                                            Qmi.ProtocolError.INFORMATIONUNAVAILABLE)
            return request(service, name, value)
        self.session.request = unavailable_after_modify
        with self.assertRaisesRegex(RuntimeError, 'transaction could not be confirmed'):
            self.helper.ensure('internet')
        self.assertEqual(len(self.modifications), 1)
        self.assertTrue(self.path.exists())
        self.assertFalse(self.helper.mark_completed())

    def test_monitor_mode_does_not_read_modem_or_checkpoint(self):
        with patch.object(self.helper.state, 'load', side_effect=AssertionError('Must not load')):
            report = self.helper.ensure('internet', mode='monitor')
        self.assertEqual(report['status'], 'monitor')
        self.assertFalse(self.calls)

    def test_journal_failure_prevents_any_profile_or_rf_change(self):
        callback = Mock()
        with patch.object(self.helper.state, 'save', side_effect=OSError('Storage unavailable')):
            with self.assertRaises(OSError):
                self.helper.ensure('internet', before_change=callback)
        callback.assert_not_called()
        self.assertFalse(self.modifications)
