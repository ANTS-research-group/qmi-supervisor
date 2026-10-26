from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from modules.device.radio import (active_band_label, format_plmn, lte_downlink_frequency_mhz,
                           nr_reference_frequency_mhz, radio_summary, LTE_DL_RANGES)


class RadioSummaryTests(unittest.TestCase):
    def sample(self, **dynamic):
        return {'dynamic': dynamic, 'timing': {'acquired_end_utc': '2026-10-20T09:28:06.535553+00:00'},
                'collection': {'requests': {
                    'nas.get_cell_location_info': {'received_at_utc': '2026-10-20T09:28:06.374344+00:00'},
                    'nas.get_rf_band_information': {'received_at_utc': '2026-10-20T09:28:06.406191+00:00'}}}}

    def nr_fixture(self):
        return self.sample(
            nas_value_serving_system_radio_interfaces=['5gnr'],
            nas_value_rf_bands=[dict(radio_interface='5gnr', active_band_class=None, active_channel=40128)],
            nas_value_rf_bands_extended=[dict(radio_interface='5gnr', active_band_class=None, active_channel=629952)],
            nas_value_rf_bandwidths=[dict(radio_interface='5gnr', bandwidth=None)],
            nas_value_nr5g_arfcn=629952, nas_value_nr5g_cell_information_plmn='99910',
            nas_value_nr5g_cell_information_tracking_area_code=101,
            nas_value_nr5g_cell_information_global_cell_id=772,
            nas_value_nr5g_cell_information_physical_cell_id=4)

    def test_live_nr_fixture_prefers_extended_and_never_infers_band(self):
        summary = radio_summary(self.nr_fixture())
        self.assertEqual(summary['rats'], ['nr'])
        cell = summary['cells'][0]
        self.assertEqual((cell['plmn'], cell['pci'], cell['tac'], cell['cell_id']), ('99910', 4, 101, 772))
        self.assertEqual(cell['channel'], 629952)
        self.assertEqual(cell['frequency_mhz'], 3449.28)
        self.assertEqual(cell['channel_type'], 'NR-ARFCN')
        self.assertIn('NR reference frequency', cell['frequency_label'])
        self.assertNotIn('band', cell)
        self.assertEqual(cell['observed_at'], '2026-10-20T09:28:06.374344+00:00')
        self.assertEqual(len(summary['bands']), 1)
        self.assertIsNone(summary['bands'][0]['band'])
        self.assertEqual(summary['bands'][0]['channel'], 629952)
        self.assertEqual(summary['bands'][0]['observed_at'], '2026-10-20T09:28:06.406191+00:00')

    def test_nr_raster_boundaries_use_exact_khz_arithmetic(self):
        for channel, expected in ((0, 0), (599999, 2999.995), (600000, 3000),
                                  (2016666, 24249.99), (2016667, 24250.08),
                                  (3279165, 99999.96), (629952, 3449.28)):
            with self.subTest(channel=channel):
                self.assertEqual(nr_reference_frequency_mhz(channel), expected)
        for channel in (None, -1, 3279166, 0xFFFFFFFF, True, 629952.0, '629952'):
            with self.subTest(channel=channel):
                self.assertIsNone(nr_reference_frequency_mhz(channel))

    def test_lte_frequency_uses_only_validated_downlink_ranges(self):
        for channel, frequency in ((0, 2110), (599, 2169.9), (600, 1930), (1300, 1815),
                                   (1650, 1850), (6150, 791), (66436, 2110), (68935, 651.9)):
            with self.subTest(channel=channel):
                self.assertEqual(lte_downlink_frequency_mhz(channel), frequency)
        for channel in (4950, 5009, 5380, 5729, 7400, 7499, 10360, 18000, 65535, 68936,
                        -1, None, True, '1300', 1300.0, 262144):
            with self.subTest(channel=channel):
                self.assertIsNone(lte_downlink_frequency_mhz(channel))
        for _, first, last, base in LTE_DL_RANGES:
            self.assertEqual(lte_downlink_frequency_mhz(first), base/1000)
            self.assertEqual(lte_downlink_frequency_mhz(last), (base+(last-first)*100)/1000)
        for left, right in zip(LTE_DL_RANGES, LTE_DL_RANGES[1:]):
            self.assertLess(left[2], right[1], 'Validated ranges must never overlap')

    def test_band_decode_uses_explicit_qmi_codes(self):
        for raw, expected in ((268, 'nr5g-77'), (269, 'nr5g-78'), (282, 'nr5g-12'),
                              (289, 'nr5g-48'), (315, 'nr5g-100'), (122, 'eutran-3'),
                              (161, 'eutran-66')):
            self.assertEqual(active_band_label(raw), expected)
        for raw in (None, 77, 78, 269.0, True, 9999):
            self.assertIsNone(active_band_label(raw))

    def test_raw_band_known_to_upstream_but_missing_in_gi_is_observed(self):
        sample = self.nr_fixture()
        sample['dynamic']['nas_value_rf_bands_extended'][0]['active_band_class_raw'] = 269
        summary = radio_summary(sample)
        self.assertEqual(summary['bands'][0]['band'], 'n78')
        self.assertEqual(summary['bands'][0]['raw'], 269)
        self.assertNotIn('band', summary['cells'][0])
        sample['dynamic']['nas_value_rf_bands_extended'][0]['active_band_class_raw'] = 9999
        summary = radio_summary(sample)
        self.assertEqual(summary['bands'][0]['raw'], 9999)
        self.assertIsNone(summary['bands'][0]['band'])

    def test_legacy_nr_can_report_band_but_never_channel_or_frequency(self):
        summary = radio_summary(self.sample(nas_value_rf_bands=[
            dict(radio_interface='5gnr', active_band_class_raw=269, active_channel=40128)]))
        self.assertEqual(summary['cells'], [])
        band = summary['bands'][0]
        self.assertEqual(band['band'], 'n78')
        self.assertEqual(band['raw_channel'], 40128)
        self.assertIsNone(band['channel'])
        self.assertIsNone(band['frequency_mhz'])

    def test_dedicated_nr_channel_overrides_extended_without_attaching_band(self):
        sample = self.nr_fixture()
        sample['dynamic']['nas_value_rf_bands_extended'][0].update(active_channel=640000, active_band_class_raw=268)
        summary = radio_summary(sample)
        self.assertEqual(summary['cells'][0]['channel'], 629952)
        self.assertEqual(summary['cells'][0]['channel_source'], 'nas_value_nr5g_arfcn')
        self.assertEqual(summary['bands'][0]['channel'], 640000)
        self.assertEqual(summary['bands'][0]['band'], 'n77')

    def test_extended_nr_fallback_requires_unique_channel(self):
        sample = self.nr_fixture()
        sample['dynamic']['nas_value_nr5g_arfcn'] = None
        summary = radio_summary(sample)
        self.assertEqual(summary['cells'][0]['channel'], 629952)
        self.assertEqual(summary['cells'][0]['channel_source'], 'nas_value_rf_bands_extended')
        sample['dynamic']['nas_value_rf_bands_extended'].append(
            dict(radio_interface='5gnr', active_band_class_raw=268, active_channel=640000))
        summary = radio_summary(sample)
        self.assertEqual(len(summary['bands']), 2)
        self.assertIsNone(summary['cells'][0]['channel'])
        self.assertIsNone(summary['cells'][0]['frequency_mhz'])

    def test_multi_rat_preserves_rf_lists_and_never_zips_bandwidths(self):
        summary = radio_summary(self.sample(
            nas_value_serving_system_radio_interfaces=['lte', '5gnr'],
            nas_value_rf_bands=[dict(radio_interface='lte', active_band_class='eutran-3', active_channel=1300)],
            nas_value_rf_bands_extended=[dict(radio_interface='5gnr', active_band_class_raw=269, active_channel=629952),
                                        dict(radio_interface='5gnr', active_band_class_raw=268, active_channel=640000)],
            nas_value_rf_bandwidths=[dict(radio_interface='5gnr', bandwidth=None, bandwidth_raw=9999)]))
        self.assertEqual(set(summary['rats']), {'lte', 'nr'})
        self.assertEqual(len(summary['bands']), 3)
        self.assertEqual(len(summary['bandwidths']), 1)
        self.assertEqual(summary['bandwidths'][0]['raw'], 9999)
        self.assertIsNone(summary['bandwidths'][0]['label'])
        self.assertFalse(any('bandwidth' in item for item in summary['bands']))
        self.assertNotIn('mode', summary)
        self.assertEqual(next(item for item in summary['bands'] if item['rat'] == 'lte')['frequency_mhz'], 1815)

    def test_bandwidth_enum_uses_upstream_nr_values_including_code_nine(self):
        summary = radio_summary(self.sample(nas_value_rf_bandwidths=[
            dict(radio_interface='5gnr', radio_interface_raw=12, bandwidth_raw=raw, bandwidth=None)
            for raw in (6, 9, 17, 18, 19, 24)]))
        self.assertEqual([item['bandwidth_mhz'] for item in summary['bandwidths']], [5, 20, 100, 200, 400, 70])
        self.assertEqual([item['raw'] for item in summary['bandwidths']], [6, 9, 17, 18, 19, 24])
        self.assertTrue(all(item['rat'] == 'nr' and item['radio_interface_raw'] == 12
                            and item['label'] is None and item['unit'] == 'MHz'
                            for item in summary['bandwidths']))
        self.assertEqual(summary['bands'], [])
        self.assertEqual(summary['cells'], [])

    def test_bandwidth_unknown_or_conflicting_rat_remains_unavailable(self):
        inputs = [('lte', 0, 1.4), ('lte', 5, 20), ('gsm', 20, .2), ('umts', 23, 10),
                  ('nr', 5, None), ('lte', 9, None), ('nr', 255, None),
                  ('nr', 9999, None), ('nr', None, None), ('nr', True, None)]
        for rat, raw, expected in inputs:
            with self.subTest(rat=rat, raw=raw):
                width = radio_summary(self.sample(nas_value_rf_bandwidths=[
                    dict(radio_interface=rat, bandwidth='preserved-label', bandwidth_raw=raw)]))['bandwidths'][0]
                self.assertEqual(width['bandwidth_mhz'], expected)
                self.assertEqual(width['label'], 'preserved-label')

    def test_lte_serving_is_matched_by_pci_and_neighbors_have_no_invented_identity(self):
        neighbor = dict(physical_cell_id=18, rsrp=-100, rsrq=-15, rssi=-80)
        serving = dict(physical_cell_id=12, rsrp=-90, rsrq=-10, rssi=-70)
        summary = radio_summary(self.sample(nas_value_lte_intrafrequency={
            'plmn': '001001', 'earfcn': 1300, 'serving_cell_id': 12, 'tracking_area_code': 1,
            'global_cell_id': 123, 'cells': [neighbor, serving]}, nas_value_lte_interfrequency={
                'frequencies': [{'earfcn': 1650, 'cells': [dict(physical_cell_id=18, rsrp=-95)]}]}))
        cell = summary['cells'][0]
        self.assertEqual((cell['pci'], cell['rsrp'], cell['frequency_mhz'], cell['plmn']), (12, -90, 1815, '001001'))
        self.assertEqual([item['relation'] for item in summary['neighbors']], ['intra', 'inter'])
        for item in summary['neighbors']:
            self.assertEqual(item['pci'], 18)
            self.assertIsNone(item['plmn'])
            self.assertIsNone(item['cell_id'])
            self.assertIsNone(item['tac'])
        self.assertEqual(summary['neighbors'][1]['frequency_mhz'], 1850)

    def test_lte_missing_serving_match_does_not_select_first_neighbor(self):
        summary = radio_summary(self.sample(nas_value_lte_intrafrequency={
            'earfcn': 1300, 'serving_cell_id': 12, 'cells': [{'physical_cell_id': 18, 'rsrp': -95}]}))
        self.assertEqual(summary['cells'][0]['pci'], 12)
        self.assertNotIn('rsrp', summary['cells'][0])
        self.assertEqual(len(summary['neighbors']), 1)

    def test_plmn_keeps_mnc_length_and_does_not_guess_ambiguous_integer(self):
        self.assertEqual(format_plmn(1, 1, True), '001001')
        self.assertEqual(format_plmn(1, 1, False), '00101')
        self.assertEqual(format_plmn(999, 10, True), '999010')
        self.assertEqual(format_plmn(999, 10, False), '99910')
        self.assertEqual(format_plmn(310, 260), '310260')
        self.assertIsNone(format_plmn(999, 10))
        self.assertIsNone(format_plmn(999, 100, False))
        sample = self.sample(nas_value_nr5g_arfcn=629952, nas_value_current_plmn_mcc=999,
                             nas_value_current_plmn_mnc=10)
        self.assertIsNone(radio_summary(sample)['cells'][0]['plmn'])
        sample['dynamic']['nas_value_current_plmn'] = '999010'
        self.assertEqual(radio_summary(sample)['cells'][0]['plmn'], '999010')

    def test_missing_malformed_and_invalid_values_are_unavailable(self):
        empty = dict(rats=[], cells=[], bands=[], bandwidths=[], neighbors=[])
        for sample in (None, {}, [], {'dynamic': None}, {'dynamic': 'bad'}):
            self.assertEqual(radio_summary(sample), empty)
        summary = radio_summary(self.sample(nas_value_serving_system_radio_interfaces=['none', None, 'unknown'],
            nas_value_lte_intrafrequency={'earfcn': 18000, 'serving_cell_id': 65535, 'cells': []},
            nas_value_rf_bands_extended=[None, 'bad']))
        self.assertIsNone(summary['cells'][0]['pci'])
        self.assertIsNone(summary['cells'][0]['frequency_mhz'])

    def test_summary_does_not_mutate_sample_and_is_json_serializable(self):
        sample = self.nr_fixture()
        before = deepcopy(sample)
        json.dumps(radio_summary(sample), allow_nan=False)
        self.assertEqual(sample, before)

    def test_fast_profile_can_report_rat_from_signal_without_inventing_cell(self):
        summary = radio_summary(self.sample(nas_value_5g_signal_strength_rsrp=-90,
                                            nas_value_lte_rsrp=-100))
        self.assertEqual(summary['rats'], ['nr', 'lte'])
        self.assertEqual(summary['cells'], [])


try:
    import gi
    gi.require_version('Qmi', '1.0')
    from gi.repository import Qmi
    from modules.services.nas_client import NASClient
except (ImportError, ValueError):
    NASClient = None


@unittest.skipIf(NASClient is None, 'Requires libqmi GI')
class RadioParserTests(unittest.TestCase):
    def test_rf_parser_preserves_raw_enums_without_new_csv_columns(self):
        from modules.telemetry.metrics import ListDynamicMetrics
        from modules.telemetry.collector import fields
        nr = SimpleNamespace(radio_interface=Qmi.NasRadioInterface(12), active_band_class=269, active_channel=629952)
        unknown = SimpleNamespace(radio_interface=Qmi.NasRadioInterface(12), active_band_class=9999, active_channel=640000)
        width = SimpleNamespace(radio_interface=Qmi.NasRadioInterface(12), bandwidth=9999)
        result = NASClient.bands(SimpleNamespace(get_extended_list=lambda: [nr, unknown], get_bandwidth_list=lambda: [width]))
        self.assertEqual(result['nas_value_rf_bands_extended'][0]['active_band_class'], 'nr5g-78')
        self.assertEqual(result['nas_value_rf_bands_extended'][0]['active_band_class_raw'], 269)
        self.assertEqual(result['nas_value_rf_bands_extended'][0]['radio_interface_raw'], 12)
        self.assertIsNone(result['nas_value_rf_bands_extended'][1]['active_band_class'])
        self.assertEqual(result['nas_value_rf_bandwidths'][0]['bandwidth_raw'], 9999)
        self.assertFalse(set(result)-set(fields(ListDynamicMetrics)))

    def test_pcs_status_publishes_exact_current_plmn(self):
        output = SimpleNamespace(get_serving_system=lambda:
            (Qmi.NasRegistrationState.REGISTERED, Qmi.NasAttachState.DETACHED,
             Qmi.NasAttachState.ATTACHED, Qmi.NasNetworkType(2), [Qmi.NasRadioInterface(12)]),
            get_current_plmn=lambda: (999, 10, ''),
            get_mnc_pcs_digit_include_status=lambda: (999, 10, True))
        self.assertEqual(NASClient.serving(output)['nas_value_current_plmn'], '999010')


if __name__ == '__main__':
    unittest.main()
