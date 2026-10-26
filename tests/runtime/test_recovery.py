import json
from pathlib import Path
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from types import SimpleNamespace

from modules.device.device import Device
from modules.runtime.state import RuntimeState
from modules.runtime.settings import Settings

try:
    from modules.device.backend import QmiBackend
    from modules.services.wds_client import WDSClient
    from gi.repository import GLib, Qmi
except (ImportError, ValueError):
    QmiBackend = None


class CheckpointTests(unittest.TestCase):
    def test_atomic_checkpoint_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            state = RuntimeState(path)
            self.assertIsNone(state.load())
            state.save({'identity': '612EDE63', 'network': {'interface': 'wwan0'}})
            self.assertEqual(state.load()['identity'], '612EDE63')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_suffix('.tmp').exists())
            state.clear()
            self.assertIsNone(state.load())


@unittest.skipIf(QmiBackend is None, 'Requires OS libqmi GI bindings')
class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'devnum').write_text('7')
        self.device = Device('/dev/cdc-wdm0', 'wwan0', str(self.root), '612EDE63', 'FM160')
        discovery = patch('modules.device.backend.discover', return_value=self.device)
        self.addCleanup(discovery.stop)
        self.discover = discovery.start()
        self.backend = QmiBackend(Settings(), threading.Event(), self.root/'state.json')
        self.backend.device = self.device
        self.backend.network = Mock()
        self.backend.network.checkpoint.return_value = {'interface': 'wwan0', 'address': None}
        self.backend.session = Mock()
        client = SimpleNamespace(get_cid=lambda: 4)
        self.backend.session.clients = {Qmi.Service.WDS: client}
        self.backend.session.client.return_value = client
        self.backend.session.close.side_effect = lambda **_: self.backend.session.clients.clear()
        self.backend.wds = Mock(handle=None)
        self.backend.wds.stop.side_effect = lambda: setattr(self.backend.wds, 'handle', None)
        self.backend.state.save({'identity': '612EDE63', 'enumeration': '7', 'cid': 4, 'handle': 42,
                                 'network': {'interface': 'wwan0', 'address': '10.42.0.2/30'}})

    def test_recover_owned_bearer_and_host_config(self):
        self.backend.recover()
        self.backend.session.client.assert_called_once_with(Qmi.Service.WDS, 4)
        self.backend.wds.stop.assert_called_once()
        self.backend.network.close.assert_called_once()
        self.backend.session.release.assert_called_once_with(Qmi.Service.WDS)
        self.assertIsNone(self.backend.state.load())

    def test_reenumerated_modem_does_not_reuse_stale_cid(self):
        (self.root/'devnum').write_text('8')
        self.backend.wds.handle = 42  # Left in memory after an earlier uncertain stop.
        self.backend.recover()
        self.backend.session.client.assert_not_called()
        self.backend.wds.stop.assert_not_called()
        self.backend.network.close.assert_called_once()
        self.assertIsNone(self.backend.wds.handle)
        self.assertIsNone(self.backend.state.load())

    def test_same_serial_and_devnum_on_a_different_usb_port_never_reuses_cid(self):
        saved = self.backend.state.load()
        self.backend.state.save(dict(saved, usb_path='/sys/another-controller/usb1/1-2'))
        with self.assertRaisesRegex(RuntimeError, 'another physical modem'):
            self.backend.recover()
        self.backend.session.client.assert_not_called()
        self.backend.wds.stop.assert_not_called()
        self.backend.network.close.assert_not_called()
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_wrong_physical_identity_cannot_retire_another_modems_journal(self):
        saved = self.backend.state.load()
        self.backend.state.save(dict(saved, identity='another-modem'))
        with self.assertRaisesRegex(RuntimeError, 'another physical modem'):
            self.backend.recover()
        self.backend.session.client.assert_not_called()
        self.backend.network.close.assert_not_called()
        self.backend.close()
        self.assertEqual(self.backend.state.load()['identity'], 'another-modem')
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_new_checkpoint_includes_canonical_physical_topology(self):
        self.backend.checkpoint()
        self.assertEqual(self.backend.state.load()['usb_path'], str(self.root.resolve()))

    def test_checkpoint_keeps_the_opened_transport_even_after_usb_reenumeration(self):
        self.backend._opened_enumeration = '7'
        self.backend.wds.handle = 42
        (self.root/'devnum').write_text('8')
        self.backend.checkpoint()
        self.assertEqual(self.backend.state.load()['enumeration'], '7')

    def test_usb_reenumeration_invalidates_presence_even_when_device_metadata_is_identical(self):
        self.backend._opened_enumeration = '7'
        self.assertTrue(self.backend.present())
        (self.root/'devnum').write_text('8')
        self.assertFalse(self.backend.present())
        with self.assertRaisesRegex(RuntimeError, 'USB enumeration changed'):
            self.backend.session_enumeration()

    def test_transport_change_during_open_preserves_predecessor_and_never_adopts_its_cid(self):
        self.backend.session.open.side_effect = lambda *_: (self.root/'devnum').write_text('8')
        with self.assertRaisesRegex(RuntimeError, 'changed while opening'):
            self.backend.open(self.device)
        self.backend.session.client.assert_not_called()
        self.backend.close()
        saved = self.backend.state.load()
        self.assertEqual((saved['enumeration'], saved['cid'], saved['handle']), ('7', 4, 42))

    def test_identity_change_during_open_is_rejected_even_with_matching_device_number(self):
        self.discover.return_value = replace(self.device, serial='replacement')
        with self.assertRaisesRegex(RuntimeError, 'changed while opening'):
            self.backend.open(self.device)
        self.backend.session.client.assert_not_called()
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_close_does_not_stop_or_release_stale_client_ids_after_transport_changes(self):
        self.backend._opened_enumeration = '7'
        self.backend.wds.handle = 42
        gps = self.backend.gnss = Mock()
        (self.root/'devnum').write_text('8')
        self.backend.close()
        gps.close.assert_not_called()
        self.assertIsNone(self.backend.gnss)
        self.backend.wds.stop.assert_not_called()
        self.backend.session.close.assert_called_once_with(release_clients=False)
        saved = self.backend.state.load()
        self.assertEqual((saved['enumeration'], saved['cid'], saved['handle']), ('7', 4, 42))
        self.assertTrue(saved['cleanup_pending'])

    def test_legacy_transport_without_provenance_never_reuses_cid_even_when_devnum_matches(self):
        saved = self.backend.state.load()
        self.backend.state.save(dict(saved, usb_path=str(self.root.resolve()), legacy_transport_unverified=True))
        self.backend.recover()
        self.backend.session.client.assert_not_called()
        self.backend.wds.stop.assert_not_called()
        self.backend.network.close.assert_called_once()
        self.assertIsNone(self.backend.state.load())
        archive = RuntimeState(self.root/'legacy-context.json').load()
        self.assertEqual(archive['handle'], 42)
        self.assertTrue(archive['retired'])

    def test_lost_previous_session_does_not_block_new_connection(self):
        self.backend.session.client.side_effect = GLib.Error.new_literal(
            Qmi.protocol_error_quark(), 'Previous client is gone', Qmi.ProtocolError.INVALIDCLIENTID)
        self.backend.recover()
        self.assertIsNone(self.backend.state.load())
        self.backend.network.close.assert_called_once()

    def test_transport_failure_to_recover_cid_blocks_start_and_survives_following_close(self):
        self.backend.session.client.side_effect = TimeoutError('Transport did not respond')
        with self.assertRaises(TimeoutError):
            self.backend.recover()
        self.backend.wds.stop.assert_not_called()
        self.assertIsNone(self.backend.wds.handle)
        self.backend.close()
        saved = self.backend.state.load()
        self.assertEqual((saved['cid'], saved['handle']), (4, 42))
        self.assertTrue(saved['cleanup_pending'])
        self.backend.session.close.assert_called_once()

    def test_untyped_invalid_cid_text_does_not_discard_ownership(self):
        self.backend.session.client.side_effect = RuntimeError('Invalid CID')
        with self.assertRaisesRegex(RuntimeError, 'Invalid CID'):
            self.backend.recover()
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_unknown_qmi_protocol_error_does_not_mean_the_old_session_is_gone(self):
        self.backend.session.client.side_effect = GLib.Error.new_literal(
            Qmi.protocol_error_quark(), 'Internal modem failure', Qmi.ProtocolError.INTERNAL)
        with self.assertRaises(GLib.Error):
            self.backend.recover()
        self.assertTrue(self.backend.state.load()['cleanup_pending'])

    def test_absent_packet_errors_confirm_cleanup_through_real_wds_adapter(self):
        saved = self.backend.state.load()
        for code in (Qmi.ProtocolError.NOEFFECT, Qmi.ProtocolError.INVALIDHANDLE, Qmi.ProtocolError.OUTOFCALL):
            with self.subTest(code=code):
                self.backend.state.save(saved)
                self.backend.wds = WDSClient(self.backend.session)
                self.backend.session.request.side_effect = GLib.Error.new_literal(
                    Qmi.protocol_error_quark(), 'Packet context already absent', code)
                self.backend.recover()
                self.assertIsNone(self.backend.wds.handle)
                self.assertIsNone(self.backend.state.load())

    def test_real_wds_transport_failure_preserves_owned_recovery_handle(self):
        self.backend.wds = WDSClient(self.backend.session)
        self.backend.session.request.side_effect = TimeoutError('No modem response')
        with self.assertRaises(TimeoutError):
            self.backend.recover()
        self.assertEqual(self.backend.wds.handle, 42)
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_uncertain_stop_retains_handle_and_checkpoint_before_client_release(self):
        self.backend.wds.handle = 42
        self.backend.wds.stop.side_effect = TimeoutError('Stop result unknown')
        self.backend.close()
        self.assertFalse(self.backend.session.clients)
        saved = self.backend.state.load()
        self.assertEqual((saved['cid'], saved['handle'], saved['identity']), (4, 42, '612EDE63'))
        self.assertTrue(saved['cleanup_pending'])
        self.assertIn('Stop result unknown', saved['cleanup_error'])
        self.assertIsNone(self.backend.device)
        self.assertIsNone(self.backend.wds.handle)
        self.backend.session.close.assert_called_once_with(preserve_cids=(Qmi.Service.WDS,))

    def test_repeated_close_does_not_retry_old_handle_or_replace_its_diagnostic(self):
        self.backend.wds.handle = 42
        self.backend.wds.stop.side_effect = TimeoutError('Stop result unknown')
        self.backend.close()
        saved = self.backend.state.load()
        self.backend.close()
        self.backend.wds.stop.assert_called_once()
        self.assertEqual(self.backend.state.load(), saved)
        self.assertIsNone(self.backend.wds.handle)

    def test_normal_close_stops_owned_gps_and_releases_clients(self):
        gps = self.backend.gnss = Mock()
        self.backend.close()
        gps.close.assert_called_once()
        self.assertIsNone(self.backend.gnss)
        self.backend.session.close.assert_called_once_with()

    def test_uncertain_recovered_stop_keeps_original_checkpoint_and_raises(self):
        self.backend.wds.stop.side_effect = TimeoutError('Recovered stop timed out')
        with self.assertRaises(TimeoutError):
            self.backend.recover()
        self.backend.session.release.assert_not_called()
        self.assertEqual(self.backend.wds.handle, 42)
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_unconfirmed_stop_return_is_not_treated_as_success(self):
        self.backend.wds.stop.side_effect = None  # A broken adapter leaves its handle untouched.
        with self.assertRaisesRegex(RuntimeError, 'stop was not confirmed'):
            self.backend.recover()
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_cid_release_timeout_retains_trace_and_blocks_recovery_completion(self):
        self.backend.session.release.side_effect = TimeoutError('CID release result unknown')
        with self.assertRaises(TimeoutError):
            self.backend.recover()
        self.assertEqual(self.backend.state.load()['handle'], 42)
        self.assertTrue(self.backend.state.load()['cleanup_pending'])

    def test_definitively_absent_cid_during_release_is_successful_cleanup(self):
        self.backend.session.release.side_effect = GLib.Error.new_literal(
            Qmi.protocol_error_quark(), 'CID already released', Qmi.ProtocolError.INVALIDCLIENTID)
        self.backend.recover()
        self.assertIsNone(self.backend.state.load())

    def test_confirmed_close_clears_checkpoint(self):
        self.backend.wds.handle = 42
        self.backend.close()
        self.assertIsNone(self.backend.state.load())
        self.backend.session.close.assert_called_once()

    def test_close_without_opening_device_cannot_discard_predecessor_checkpoint(self):
        self.backend.device = None
        self.backend.close()
        self.assertEqual(self.backend.state.load()['handle'], 42)

    def test_host_cleanup_failure_keeps_checkpoint_and_still_closes_session(self):
        self.backend.network.close.side_effect = TimeoutError('Host cleanup failed')
        with self.assertRaises(TimeoutError):
            self.backend.close()
        self.assertTrue(self.backend.state.load()['cleanup_pending'])
        self.backend.session.close.assert_called_once()

    def test_uncertain_close_then_failed_open_preserves_old_cid_without_stopping_on_new_session(self):
        self.backend.wds = WDSClient(self.backend.session)
        self.backend.wds.handle = 42
        self.backend.session.request.side_effect = TimeoutError('Stop result unknown')
        self.backend.close()
        self.assertEqual(self.backend.state.load()['cid'], 4)
        self.backend.session.request.reset_mock()
        self.backend.session.open.side_effect = TimeoutError('Open result unknown')
        with self.assertRaises(TimeoutError):
            self.backend.open(self.device)
        self.backend.close()
        self.backend.session.request.assert_not_called()
        saved = self.backend.state.load()
        self.assertEqual((saved['cid'], saved['handle'], saved['enumeration']), (4, 42, '7'))
        self.assertIsNone(self.backend.wds.handle)

    def test_failed_open_after_usb_reenumeration_never_reassigns_old_handle(self):
        self.backend.wds = WDSClient(self.backend.session)
        self.backend.wds.handle = 42
        self.backend.session.request.side_effect = TimeoutError('Stop result unknown')
        self.backend.close()
        (self.root/'devnum').write_text('8')
        self.backend.session.request.reset_mock()
        self.backend.session.open.side_effect = TimeoutError('USB is not ready')
        with self.assertRaises(TimeoutError):
            self.backend.open(self.device)
        self.backend.close()
        self.backend.session.request.assert_not_called()
        self.assertEqual(self.backend.state.load()['enumeration'], '7')
        # A later successful open retires this old USB ownership without using
        # either the old CID or packet handle on the new enumeration.
        self.backend.session.open.side_effect = None
        self.backend.session.client.reset_mock()
        self.backend.open(self.device)
        self.backend.session.client.assert_not_called()
        self.assertIsNone(self.backend.state.load())

    def test_monitor_open_never_adopts_or_rewrites_a_saved_connect_session(self):
        self.backend.settings = replace(self.backend.settings,
            modem=replace(self.backend.settings.modem, mode='monitor'))
        self.backend.wds = WDSClient(self.backend.session)
        self.backend.wds.handle = 42
        saved = self.backend.state.path.read_bytes()
        with patch.object(self.backend.state, 'load', side_effect=AssertionError('Monitor read recovery state')):
            self.backend.open(self.device)
            self.backend.close()
        self.backend.session.request.assert_not_called()
        self.backend.session.client.assert_not_called()
        self.assertEqual(self.backend.state.path.read_bytes(), saved)
