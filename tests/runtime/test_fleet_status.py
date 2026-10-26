"""Public modem identity, current intent and observed data must remain consistent."""
import unittest
from modules.runtime.fleet_status import modem_snapshot, signal_summary


class FleetStatusTests(unittest.TestCase):
    def test_idle_detected_modem_uses_linux_identity_without_claiming_readable_qmi(self):
        entry = dict(detected=True, device={'path': '/dev/synthetic', 'product': 'Detected UE'},
                     state='IDLE', features={'connection': {'enabled': False}})
        snapshot = dict(state='IDLE', device=None, sample=None, modem_readable=True)
        result = modem_snapshot(snapshot, entry)
        self.assertEqual(result['device'], entry['device'])
        self.assertFalse(result['modem_readable'])
        self.assertIsNone(result['sample'])
        self.assertIsNone(snapshot['device'])

    def test_pending_replacement_does_not_label_old_ue_samples_as_new_hardware(self):
        entry = dict(detected=True, assignment_pending=True, device={'path': '/dev/new'},
                     state='STOPPING', features={'connection': {'pending': True}})
        old = dict(state='ONLINE', device={'path': '/dev/old'}, sample={'log_id': 50},
                   history=[{'sample_id': 50}], modem_readable=True)
        result = modem_snapshot(old, entry, manager_heartbeat=100, boot_id='test-boot')
        self.assertEqual(result['state'], 'STOPPING')
        self.assertEqual(result['device'], {'path': '/dev/new'})
        self.assertFalse(result['modem_readable'])
        self.assertIsNone(result['sample'])
        self.assertEqual(result['history'], [])
        self.assertEqual(old['sample']['log_id'], 50)

    def test_removed_modem_clears_live_values_while_other_profiles_keep_their_own_data(self):
        old = dict(state='ONLINE', sample={'log_id': 1}, history=[{'sample_id': 1}])
        missing = dict(detected=False, device=None, state='WAIT_DEVICE')
        self.assertIsNone(modem_snapshot(old, missing)['sample'])
        present = dict(detected=True, device={'path': '/dev/other'})
        self.assertEqual(modem_snapshot(old, present)['sample']['log_id'], 1)

    def test_replaced_worker_is_not_identified_by_a_reused_pid_or_old_snapshot(self):
        entry = dict(detected=True, modem_worker_id='new-worker', device={'path': '/dev/synthetic'})
        old = dict(state='ONLINE', sample={'log_id': 1},
                   processes={'modem': {'pid': 100, 'worker_id': 'old-worker'}})
        self.assertIsNone(modem_snapshot(old, entry)['sample'])
        current = dict(old, processes={'modem': {'pid': 100, 'worker_id': 'new-worker'}})
        self.assertEqual(modem_snapshot(current, entry)['sample']['log_id'], 1)
        idle = dict(entry, state='IDLE', modem_worker_id=None)
        self.assertIsNone(modem_snapshot(old, idle)['sample'])

    def test_idle_snapshot_from_an_older_profile_incarnation_is_not_reused(self):
        old = dict(state='IDLE', profile_instance_id='old-platform', sample=None,
                   history=[{'sample_id': 5}], processes={'modem': {'worker_id': None}})
        entry = dict(detected=True, state='IDLE', profile_instance_id='new-platform',
                     modem_worker_id=None, device={'path': '/dev/synthetic'})
        self.assertEqual(modem_snapshot(old, entry)['history'], [])
        current = dict(old, profile_instance_id='new-platform')
        self.assertEqual(modem_snapshot(current, entry)['history'], [{'sample_id': 5}])

    def test_a_dead_worker_cannot_retain_a_live_radio_or_online_claim(self):
        old = dict(state='ONLINE', modem_readable=True, sample={'log_id': 6},
                   radio={'rat': 'NR'}, network={'address': '192.0.2.2'}, history=[{'sample_id': 6}],
                   connection={'phase': 'ONLINE'})
        entry = dict(detected=True, device={'path': '/dev/synthetic'}, state='BACKOFF', modem_alive=False)
        value = modem_snapshot(old, entry)
        self.assertEqual(value['state'], 'BACKOFF')
        self.assertEqual(value['connection']['phase'], 'BACKOFF')
        self.assertIsNone(value['sample'])
        self.assertFalse(value['modem_readable'])
        self.assertEqual(value['radio'], {})
        self.assertEqual(value['network'], {})
        self.assertEqual(value['history'], [{'sample_id': 6}])


class SignalSummaryTests(unittest.TestCase):
    """The fleet list shows a reading only while it is current."""
    def record(self, **values):
        record = dict(history=[{'monotonic': 100.0, 'rsrp': -92, 'snr': 14, 'rat': 'LTE'}],
                      radio={'bands': [{'rat': 'lte', 'band': 3, 'label': 'B3'}], 'cells': [{'rat': 'lte', 'plmn': '21407'}]},
                      sample={'dynamic': {'nas_value_current_plmn_description': 'Movistar'}})
        record.update(values)
        return record

    def test_fresh_reading_reports_operator_technology_band_and_levels(self):
        self.assertEqual(signal_summary(self.record(), now=102.0),
                         dict(rat='LTE', band='B3', operator='Movistar', rsrp=-92, snr=14))

    def test_stale_samples_and_stopped_workers_never_look_current(self):
        self.assertIsNone(signal_summary(self.record(), now=111.0))
        self.assertIsNotNone(signal_summary(self.record(), now=111.0, interval=5))
        self.assertIsNone(signal_summary(self.record(), now=102.0, alive=False))
        self.assertIsNone(signal_summary(self.record(history=[]), now=102.0))
        self.assertIsNone(signal_summary({}, now=102.0))
        self.assertIsNone(signal_summary(self.record(history=[{'rsrp': -90}]), now=102.0), 'unknown age')

    def test_missing_network_name_falls_back_to_the_cell_plmn_and_rejects_non_numbers(self):
        result = signal_summary(self.record(sample=None, history=[{'monotonic': 100.0, 'rsrp': True, 'snr': float('nan'), 'rat': 'NR'}]), now=100.0)
        self.assertEqual(result, dict(rat='NR', band='B3', operator='21407', rsrp=None, snr=None))
