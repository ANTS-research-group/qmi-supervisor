"""Registration evidence and cause classification (M6).

GI-free on purpose: the same rules run in unit tests on any host. The episode
is fed by the Get Serving System and Get System Info responses the modems
already read while waiting for registration, so it costs no extra QMI.
"""
from datetime import datetime, timezone
import json

from modules.device.radio import format_plmn
from modules.runtime.diagnosis import format_duration

SERVICE_ORDER = {'none': 0, 'power-save': 0, 'limited': 1, 'limited-regional': 1, 'available': 2}
MAX_PLMNS = 16
MAX_REJECTS = 16
MAX_NETWORKS = 6
SILENT_FAILURES = 3
SUMMARY_MAX_BYTES = 4096

# 3GPP TS 24.301 (EMM) and TS 24.501 (5GMM). libqmi's NasRejectCause labels are
# EMM-only (and raise for some 5GMM values), so keep local integer tables.
EMM_CAUSES = {
    2: 'IMSI unknown in HSS', 3: 'illegal UE', 5: 'IMEI not accepted',
    6: 'illegal ME', 7: 'EPS services not allowed',
    8: 'EPS and non-EPS services not allowed',
    9: 'UE identity cannot be derived', 10: 'implicitly detached',
    11: 'PLMN not allowed', 12: 'tracking area not allowed',
    13: 'roaming not allowed in this tracking area',
    14: 'EPS services not allowed in this PLMN',
    15: 'no suitable cells in tracking area', 16: 'MSC temporarily not reachable',
    17: 'network failure', 18: 'CS domain not available', 19: 'ESM failure',
    20: 'MAC failure', 21: 'synchronization failure', 22: 'congestion',
    23: 'UE security capabilities mismatch', 24: 'security mode rejected',
    25: 'not authorized for this CSG', 26: 'non-EPS authentication unacceptable',
    35: 'requested service option not authorized', 40: 'no EPS bearer context activated',
    42: 'severe network failure', 43: 'semantically incorrect message',
    44: 'invalid mandatory information', 45: 'message type not implemented',
    46: 'message type incompatible with protocol state',
    47: 'information element not implemented', 48: 'conditional IE error',
    49: 'protocol error, unspecified',
}
NR5G_CAUSES = {
    3: 'illegal UE', 5: 'PEI not accepted', 6: 'illegal ME',
    7: '5GS services not allowed', 9: 'UE identity cannot be derived',
    10: 'implicitly deregistered', 11: 'PLMN not allowed',
    12: 'tracking area not allowed', 13: 'roaming not allowed in this tracking area',
    15: 'no suitable cells in tracking area', 20: 'MAC failure',
    21: 'synchronization failure', 22: 'congestion',
    23: 'UE security capabilities mismatch', 24: 'security mode rejected',
    26: 'non-5G authentication unacceptable', 27: 'N1 mode not allowed',
    28: 'restricted service area', 29: 'not authorized for this CAG',
    30: 'PLMN not allowed at this location', 31: 'restricted service area',
    62: 'no network slices available', 65: 'maximum PDU sessions reached',
    67: 'insufficient resources for this slice and DNN',
    69: 'insufficient resources for this slice', 71: 'ngKSI already in use',
    72: 'non-3GPP access not allowed',
    73: 'UE can only register with N1 mode for EPS services',
    74: 'pay token expired or not available',
    75: '5GS services not allowed', 76: 'non-3GPP access not allowed',
}
SUBSCRIBER_CAUSES = {2, 3, 6, 7, 8, 9, 62, 74, 75, 76}
INVALIDATED = {'cs-invalid': 'cs', 'ps-invalid': 'ps', 'cs-ps-invalid': 'cs-ps'}


def cause_label(rat, cause):
    """A short English label for one reject cause; unknown values keep '#<n>'."""
    try:
        number = int(cause)
    except (TypeError, ValueError):
        return f'#{cause}'
    table = NR5G_CAUSES if rat == 'nr5g' else EMM_CAUSES
    return table.get(number, f'#{number}')


def cause_class(cause):
    """subscriber, area, congestion or other, per the UI action families."""
    try:
        number = int(cause)
    except (TypeError, ValueError):
        return 'other'
    if number in SUBSCRIBER_CAUSES:
        return 'subscriber'
    if 11 <= number <= 15:
        return 'area'
    if number == 22:
        return 'congestion'
    return 'other'


def _plmn(value):
    text = '' if value is None else str(value).strip()
    return text if len(text) in (5, 6) and text.isdigit() else None


def _utc_text(moment):
    try:
        return moment.strftime('%Y-%m-%d %H:%M UTC')
    except (AttributeError, ValueError):
        return ''


class RegistrationEpisode:
    """Bounded evidence of one wait for registration."""

    def __init__(self, started=None, timeout=None, home=None, expected=None, utcnow=None):
        self.started = started
        self.last = started
        self.timeout = timeout
        self.home = _plmn(home)
        self.expected = _plmn(expected)
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.started_utc = self.utcnow().isoformat()
        self.samples = 0
        self.failures = 0
        self.failed_methods = {}
        self.camped = {}
        self.rejects = {}
        self.sim_reject = None
        self.restriction = None
        self.serving = {}
        self.serving_plmn = None
        self.registered_plmn = None

    def _touch(self, now):
        if self.started is None:
            self.started = now
        self.last = now

    def set_home(self, plmn):
        self.home = _plmn(plmn) or self.home
        return self.home

    def set_expected(self, plmn):
        self.expected = _plmn(plmn)
        return self.expected

    def observe_serving(self, values, now):
        """One Get Serving System response, already parsed by NASClient.serving."""
        if not isinstance(values, dict):
            return
        self._touch(now)
        self.samples += 1
        self.serving = {
            'registration': values.get('nas_value_serving_system_registration_state'),
            'ps': values.get('nas_value_serving_system_ps_attach_state'),
            'plmn': (_plmn(values.get('nas_value_current_plmn'))
                     or format_plmn(values.get('nas_value_current_plmn_mcc'),
                                    values.get('nas_value_current_plmn_mnc'))),
        }
        self.serving_plmn = self.serving['plmn']
        if self.serving_plmn:
            self._camped(self.serving_plmn, None, None, None, None, now)

    def observe_system_info(self, parsed, now):
        """One Get System Info response, already parsed by NASClient.system_info."""
        if not isinstance(parsed, dict):
            return
        self._touch(now)
        self.samples += 1
        if parsed.get('sim_reject'):
            self.sim_reject = parsed['sim_reject']
        if parsed.get('restriction'):
            self.restriction = parsed['restriction']
        for rat, values in (parsed.get('rats') or {}).items():
            if not isinstance(values, dict):
                continue
            plmn = _plmn(values.get('plmn'))
            service = values.get('service')
            if plmn:
                self._camped(plmn, service, rat, values.get('forbidden'),
                             values.get('roaming'), now)
            reject = values.get('reject')
            if isinstance(reject, dict) and reject.get('cause') is not None:
                self.observe_reject(rat, reject.get('domain'), reject.get('cause'), plmn, now)

    def observe_failure(self, method, cause, now):
        """A NAS registration query that did not answer."""
        self._touch(now)
        self.failures += 1
        name = str(method or 'unknown')
        self.failed_methods[name] = self.failed_methods.get(name, 0) + 1

    def observe_reject(self, rat, domain, cause, plmn, now):
        """A reject cause seen in system info (M6) or an indication (M16)."""
        self._touch(now)
        try:
            number = int(cause)
        except (TypeError, ValueError):
            return
        key = (str(rat or 'unknown'), str(domain or 'none'), number, _plmn(plmn))
        record = self.rejects.get(key)
        if record is None:
            if len(self.rejects) >= MAX_REJECTS:
                oldest = min(self.rejects, key=lambda item: self.rejects[item]['last'])
                self.rejects.pop(oldest)
            first = self.utcnow()
            record = self.rejects[key] = {'count': 0, 'first_utc': first,
                                          'since_text': _utc_text(first), 'last': now}
        record['count'] += 1
        record['last'] = now

    def _camped(self, plmn, service, rat, forbidden, roaming, now):
        record = self.camped.get(plmn)
        if record is None:
            if len(self.camped) >= MAX_PLMNS:
                oldest = min(self.camped, key=lambda item: self.camped[item]['last'])
                if self.camped[oldest]['last'] >= now:
                    return
                self.camped.pop(oldest)
            record = self.camped[plmn] = {'plmn': plmn, 'service': 'none', 'rats': set(),
                                          'forbidden': None, 'roaming': None,
                                          'first': now, 'last': now}
        if service and SERVICE_ORDER.get(service, -1) > SERVICE_ORDER.get(record['service'], -1):
            record['service'] = service
        if rat:
            record['rats'].add(rat)
        if forbidden is not None:
            record['forbidden'] = bool(forbidden)
        if roaming is not None:
            record['roaming'] = roaming
        record['last'] = now

    def registered(self, plmn=None):
        self.registered_plmn = _plmn(plmn) or self.serving_plmn or self.expected
        return self.registered_plmn

    def classify(self, now):
        """The first matching cause, as (code, params)."""
        started = self.started if self.started is not None else now
        duration = format_duration(max(0, now - started))
        if self.samples == 0 and self.failures >= SILENT_FAILURES:
            return 'registration.modem_silent', {'duration': duration, 'failures': self.failures}
        if self.sim_reject in INVALIDATED:
            return 'registration.sim_invalidated', {'domain': INVALIDATED[self.sim_reject]}
        if self.restriction == 'camped-only':
            return 'registration.restricted', {'restriction': self.restriction}
        expected = self.expected
        if (expected and self.serving.get('registration') == 'registered'
                and self.serving.get('ps') == 'attached' and self.serving.get('plmn')
                and self.serving['plmn'] != expected):
            return 'registration.wrong_network', {'plmn': self.serving['plmn'],
                                                  'expected': expected}
        if self.rejects:
            key, record = max(self.rejects.items(),
                              key=lambda item: (item[1]['count'], item[1]['last']))
            rat, domain, cause, plmn = key
            return 'registration.rejected', {
                'plmn': plmn or '-', 'rat': rat, 'domain': domain, 'cause': cause,
                'cause_label': cause_label(rat, cause),
                'cause_class': cause_class(cause), 'count': record['count'],
                'since': record['since_text']}
        home = self.home or expected
        networks = sorted(self.camped)[:MAX_NETWORKS]
        if home and home not in self.camped and self.camped:
            return 'registration.home_not_found', {
                'home': home, 'networks': networks, 'duration': duration,
                'sim_reject': self.sim_reject or 'unknown'}
        if not home and any(record['service'] in {'limited', 'limited-regional'}
                            for record in self.camped.values()):
            return 'registration.limited_service', {'networks': networks}
        if not self.camped:
            return 'registration.no_coverage', {'duration': duration}
        return 'registration.timeout', {'seconds': int(self.timeout or max(0, now - started))}

    def summary(self):
        """A bounded snapshot block, never the raw responses."""
        now = self.last if self.last is not None else (self.started or 0)
        code, params = self.classify(now)
        value = {
            'code': code, 'params': params, 'home': self.home, 'expected': self.expected,
            'camped': [{'plmn': record['plmn'], 'service': record['service'],
                        'rats': sorted(record['rats']), 'forbidden': record['forbidden'],
                        'roaming': record['roaming']}
                       for record in self.camped.values()][:MAX_PLMNS],
            'rejects': [{'rat': key[0], 'domain': key[1], 'cause': key[2], 'plmn': key[3],
                         'count': record['count'],
                         'since_utc': _iso(record['first_utc'])}
                        for key, record in self.rejects.items()][:MAX_REJECTS],
            'sim_reject': self.sim_reject, 'since_utc': self.started_utc,
            'samples': self.samples}
        while _size(value) > SUMMARY_MAX_BYTES and value['rejects']:
            value['rejects'].pop()
        while _size(value) > SUMMARY_MAX_BYTES and value['camped']:
            value['camped'].pop()
        return value


def _iso(moment):
    try:
        return moment.isoformat()
    except AttributeError:
        return ''


def _size(value):
    try:
        return len(json.dumps(value, ensure_ascii=True, default=str))
    except (TypeError, ValueError):
        return SUMMARY_MAX_BYTES + 1
