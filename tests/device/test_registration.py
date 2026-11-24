"""registration evidence and cause-specific timeouts, without GI."""
from datetime import datetime, timezone
import json
import unittest

from modules.device.registration import (
    RegistrationEpisode, cause_class, cause_label,
)


def utcnow():
    return datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def serving(registration='registration-denied', ps='detached', plmn='21403'):
    values = {'nas_value_serving_system_registration_state': registration,
              'nas_value_serving_system_ps_attach_state': ps}
    if plmn:
        values['nas_value_current_plmn'] = plmn
    return values


def system_info(service='limited', plmn='21403', forbidden=True, sim_reject='available',
                reject=None):
    values = {'sim_reject': sim_reject, 'rats': {
        'nr5g': {'service': service, 'true_service': service, 'domain': 'ps',
                 'forbidden': forbidden, 'roaming': 'on', 'plmn': plmn}}}
    if reject is not None:
        values['rats']['nr5g']['reject'] = {'domain': 'ps', 'cause': reject}
    return values


class RegistrationEpisodeTests(unittest.TestCase):
    def episode(self, **values):
        return RegistrationEpisode(started=0.0, timeout=180, utcnow=utcnow, **values)

    def test_modem_silent_needs_three_failures_and_no_observation(self):
        episode = self.episode()
        for _ in range(3):
            episode.observe_failure('get_serving_system', 'timeout', 10)
        self.assertEqual(episode.classify(10), ('registration.modem_silent',
                                                {'duration': '10 s', 'failures': 3}))
        episode.observe_serving(serving(), 11)
        self.assertNotEqual(episode.classify(11)[0], 'registration.modem_silent')

    def test_sim_invalidated_comes_from_the_sim_reject_state(self):
        episode = self.episode()
        episode.observe_system_info(system_info(sim_reject='cs-ps-invalid'), 10)
        self.assertEqual(episode.classify(10), ('registration.sim_invalidated',
                                                {'domain': 'cs-ps'}))

    def test_camped_only_restriction_is_named(self):
        episode = self.episode()
        parsed = system_info()
        parsed['restriction'] = 'camped-only'
        episode.observe_system_info(parsed, 10)
        self.assertEqual(episode.classify(10), ('registration.restricted',
                                                {'restriction': 'camped-only'}))

    def test_wrong_network_needs_an_attached_registration_on_another_plmn(self):
        episode = self.episode(expected='99910')
        episode.observe_serving(serving(registration='registered', ps='attached', plmn='21403'), 10)
        self.assertEqual(episode.classify(10), ('registration.wrong_network',
                                                {'plmn': '21403', 'expected': '99910'}))
        episode.observe_serving(serving(registration='registered', ps='attached', plmn='99910'), 11)
        self.assertNotEqual(episode.classify(11)[0], 'registration.wrong_network')

    def test_a_reject_cause_is_named_through_local_tables(self):
        episode = self.episode()
        episode.observe_reject('nr5g', 'ps', 11, '21403', 10)
        code, params = episode.classify(10)
        self.assertEqual(code, 'registration.rejected')
        self.assertEqual(params['cause'], 11)
        self.assertEqual(params['cause_label'], 'PLMN not allowed')
        self.assertEqual(params['cause_class'], 'area')
        self.assertEqual(params['plmn'], '21403')
        self.assertEqual(params['rat'], 'nr5g')
        self.assertEqual(params['count'], 1)
        self.assertTrue(params['since'])
        self.assertEqual(cause_label('nr5g', 199), '#199')
        self.assertEqual(cause_class(22), 'congestion')
        self.assertEqual(cause_class(7), 'subscriber')
        self.assertEqual(cause_class(99), 'other')

    def test_home_not_found_prefers_six_bounded_networks(self):
        episode = self.episode(home='99910')
        for index in range(16):
            episode.observe_system_info(system_info(plmn=f'214{(index % 100):02d}'), 10 + index)
        code, params = episode.classify(100)
        self.assertEqual(code, 'registration.home_not_found')
        self.assertEqual(params['home'], '99910')
        self.assertEqual(params['sim_reject'], 'available')
        self.assertEqual(len(params['networks']), 6)
        self.assertEqual(params['duration'], '1 min')

    def test_limited_service_without_home_and_no_coverage(self):
        episode = self.episode()
        episode.observe_system_info(system_info(service='limited'), 10)
        self.assertEqual(episode.classify(10)[0], 'registration.limited_service')
        empty = self.episode()
        self.assertEqual(empty.classify(10), ('registration.no_coverage',
                                              {'duration': '10 s'}))

    def test_the_i3_sequence_ends_in_home_not_found(self):
        """mocha1 9f173352: 4 days of limited service on 214-xx, home 999-10."""
        episode = self.episode(home='99910')
        networks = ['21403', '21407', '21401', '21404', '21405', '21406',
                    '26201', '26202', '20408', '24007', '23415', '20801',
                    '21409', '21410', '21411', '21412']
        for day, plmn in enumerate(networks):
            episode.observe_serving(serving(plmn=plmn), day * 86400)
            episode.observe_system_info(system_info(plmn=plmn), day * 86400 + 1)
        code, params = episode.classify(4 * 86400)
        self.assertEqual(code, 'registration.home_not_found')
        self.assertEqual(params['home'], '99910')
        self.assertEqual(params['duration'], '4 d 0 h')
        self.assertIn('21403', params['networks'])
        self.assertEqual(len(params['networks']), 6)

    def test_the_summary_is_bounded_and_never_carries_raw_responses(self):
        episode = self.episode(home='99910')
        for index in range(40):
            episode.observe_system_info(system_info(plmn=f'2{index:04d}'), index)
            episode.observe_reject('nr5g', 'ps', 111 + index, f'2{index:04d}', index)
        summary = episode.summary()
        self.assertLessEqual(len(json.dumps(summary)), 4096)
        self.assertLessEqual(len(summary['camped']), 16)
        self.assertLessEqual(len(summary['rejects']), 16)
        self.assertEqual(set(summary), {'code', 'params', 'home', 'expected', 'camped',
                                        'rejects', 'sim_reject', 'since_utc', 'samples',
                                        'context'})

    def test_registered_records_the_plmn_and_classifies_a_fallback_timeout(self):
        episode = self.episode()
        episode.observe_serving(serving(registration='registered', ps='attached'), 10)
        self.assertEqual(episode.registered(), '21403')
        # Camped on its own home without service and without any other cause.
        other = self.episode(home='99910')
        other.observe_system_info(system_info(plmn='99910', service='none',
                                              sim_reject=None), 10)
        code, params = other.classify(10)
        self.assertEqual(code, 'registration.timeout')
        self.assertEqual(params, {'seconds': 180})


if __name__ == '__main__':
    unittest.main()
