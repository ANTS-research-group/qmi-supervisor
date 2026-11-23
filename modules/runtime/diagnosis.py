"""Failure identity, severity and journal policy.

This module deliberately imports no GLib so the rules can be unit-tested on
any host. Hardware errors reach it as plain exceptions carrying the libqmi
domain and code as attributes.
"""
import contextlib
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
import time

# Thresholds live here on purpose (one documented place, not configuration).
PERSISTENT_ATTEMPTS = 3
PERSISTENT_SECONDS = 120
ALWAYS_PERSISTENT_SECONDS = 600
LOG_FIRST_ATTEMPTS = 3
LOG_SUMMARY_COUNTS = (10, 100, 1000)
LOG_SUMMARY_SECONDS = 3600
DETAIL_MAX_BYTES = 256
PARAMS_MAX_ITEMS = 16
DIAGNOSIS_MAX_BYTES = 4096
SNAPSHOT_EXTRA_MAX_BYTES = 16384
STREAK_SAVE_SECONDS = 60
HEALTH_REFRESH_SECONDS = 600
BOOT_GRACE_SECONDS = 90
MAX_TRANSIENTS_IN_A_ROW = 2
PROBE_BACKOFF_SECONDS = (600, 3600, 21600)

STATE_ORDER = {'WAIT_DEVICE': 0, 'OPEN': 1, 'WAIT_REGISTRATION': 2,
               'CONNECT': 3, 'CONFIGURE_IP': 4, 'ONLINE': 5}

# Single source of truth for the published diagnosis codes. The categories and
# the version that first shipped each code form the machine-readable contract
# (doc/schemas/codes.json); POLICIES below is derived from this table.
CODES = {
    'qmi.no_answer': {'category': 'qmi', 'policy': 'default', 'since': 'v1.3.0'},
    'qmi.endpoint_hangup': {'category': 'qmi', 'policy': 'default', 'since': 'v1.3.0'},
    'qmi.error': {'category': 'qmi', 'policy': 'default', 'since': 'v1.3.0'},
    'unknown': {'category': 'qmi', 'policy': 'default', 'since': 'v1.3.0'},
    'device.removed': {'category': 'device', 'policy': 'default', 'since': 'v1.3.0'},
    'device.locked': {'category': 'device', 'policy': 'default', 'since': 'v1.3.0'},
    'host.modemmanager_active': {'category': 'host', 'policy': 'immediate', 'since': 'v1.3.0'},
    'host.udhcpc_missing': {'category': 'host', 'policy': 'immediate', 'since': 'v1.3.0'},
    'config.pending': {'category': 'config', 'policy': 'default', 'since': 'v1.3.0'},
    'worker.restarting': {'category': 'worker', 'policy': 'default', 'since': 'v1.3.0'},
    'worker.starting': {'category': 'worker', 'policy': 'info', 'since': 'v1.3.0'},
    'session.lost': {'category': 'session', 'policy': 'default', 'since': 'v1.3.0'},
    'ip.incomplete': {'category': 'session', 'policy': 'default', 'since': 'v1.3.0'},
    'telemetry.stalled': {'category': 'telemetry', 'policy': 'default', 'since': 'v1.3.0'},
    'recovery.saved_client_unanswered': {'category': 'recovery', 'policy': 'default', 'since': 'v1.3.0'},
    'recovery.cleanup_unconfirmed': {'category': 'recovery', 'policy': 'default', 'since': 'v1.3.0'},
    'recovery.host_cleanup_unconfirmed': {'category': 'recovery', 'policy': 'default', 'since': 'v1.3.0'},
    'recovery.foreign_record': {'category': 'recovery', 'policy': 'immediate', 'since': 'v1.3.0'},
    'sim.absent': {'category': 'sim', 'policy': 'after_ladder', 'since': 'v1.3.0'},
    'sim.no_atr': {'category': 'sim', 'policy': 'after_ladder', 'since': 'v1.3.0'},
    'sim.contact_lost': {'category': 'sim', 'policy': 'after_ladder', 'since': 'v1.3.0'},
    'sim.card_error': {'category': 'sim', 'policy': 'after_ladder', 'since': 'v1.3.0'},
    'sim.not_reported': {'category': 'sim', 'policy': 'after_ladder', 'since': 'v1.3.0'},
    'sim.initializing': {'category': 'sim', 'policy': 'info', 'since': 'v1.3.0'},
    'sim.pin_required': {'category': 'sim', 'policy': 'immediate', 'since': 'v1.3.0'},
    'sim.puk_required': {'category': 'sim', 'policy': 'immediate', 'since': 'v1.3.0'},
    'sim.blocked': {'category': 'sim', 'policy': 'immediate', 'since': 'v1.3.0'},
    'sim.network_locked': {'category': 'sim', 'policy': 'immediate', 'since': 'v1.3.0'},
    'sim.app_illegal': {'category': 'sim', 'policy': 'immediate', 'since': 'v1.3.0'},
    'sim.in_inactive_slot': {'category': 'sim', 'policy': 'immediate', 'since': 'v1.3.0'},
    'sim.operator_action_required': {'category': 'sim', 'policy': 'immediate', 'since': 'v1.3.0'},
    'registration.modem_silent': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'registration.sim_invalidated': {'category': 'registration', 'policy': 'immediate', 'since': 'v1.3.0'},
    'registration.rejected': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'registration.home_not_found': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'registration.limited_service': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'registration.no_coverage': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'registration.restricted': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'registration.wrong_network': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'registration.timeout': {'category': 'registration', 'policy': 'default', 'since': 'v1.3.0'},
    'startup.transient': {'category': 'startup', 'policy': 'info', 'since': 'v1.3.0'},
    'connect.rejected': {'category': 'connect', 'policy': 'default', 'since': 'v1.3.0'},
}

# Derived: code -> 'immediate' | 'after_ladder' | 'info'; anything else is 'default'.
POLICIES = {code: entry['policy'] for code, entry in CODES.items()
            if entry['policy'] != 'default'}

# English journal phrasing only; the UI catalogue lives in frontend/lib/diagnosis.ts.
LOG_TITLES = {
    'qmi.no_answer': 'the modem did not answer in time',
    'qmi.endpoint_hangup': 'the connection to the modem closed while waiting for an answer',
    'qmi.error': 'the modem refused a request',
    'unknown': 'an unexpected error',
    'device.removed': 'the modem left the USB bus or was enumerated again',
    'device.locked': 'another process is using this modem',
    'host.modemmanager_active': 'ModemManager is running and would compete for the modem',
    'host.udhcpc_missing': 'DHCP needs udhcpc, which is not installed',
    'config.pending': 'a configuration change could not be applied yet',
    'worker.restarting': 'the modem process stopped and is being restarted',
    'worker.starting': 'the modem process is starting',
    'session.lost': 'the data session or its IPv4 configuration was lost',
    'ip.incomplete': 'the data session is up but IPv4 is not configured on the host',
    'telemetry.stalled': 'monitoring has stored no sample',
    'recovery.saved_client_unanswered':
        'the modem does not answer the data session left by a previous run',
    'recovery.cleanup_unconfirmed': 'the stop of a previous data session could not be confirmed',
    'recovery.host_cleanup_unconfirmed':
        'the host network of a previous run could not be cleaned',
    'recovery.foreign_record': 'the saved connection record belongs to another modem',
    'sim.absent': 'no SIM card detected',
    'sim.no_atr': 'the SIM card does not answer',
    'sim.contact_lost': 'the modem lost contact with the SIM',
    'sim.card_error': 'the SIM card reports an error',
    'sim.not_reported': 'the modem does not report any SIM slot',
    'sim.initializing': 'the SIM is starting',
    'sim.pin_required': 'the SIM is locked with a PIN',
    'sim.puk_required': 'the SIM is blocked and needs its PUK',
    'sim.blocked': 'the SIM is permanently blocked',
    'sim.network_locked': 'the modem is locked to another network or SIM',
    'sim.app_illegal': 'the network has invalidated this SIM',
    'sim.in_inactive_slot': 'the SIM is in a slot the modem does not use',
    'sim.operator_action_required': 'the SIM still fails after modem restarts',
    'registration.modem_silent': 'the modem does not answer network queries',
    'registration.sim_invalidated': 'the network has invalidated this SIM',
    'registration.rejected': 'the network rejects this SIM',
    'registration.home_not_found': 'the modem has not camped on its home network',
    'registration.limited_service': 'no network accepts this SIM here',
    'registration.no_coverage': 'no mobile network found',
    'registration.restricted': 'the modem is set to camp only, without registering',
    'registration.wrong_network': 'the modem is registered on the wrong network',
    'registration.timeout': 'network registration timed out',
    'startup.transient': 'the modem is still starting after a new USB connection',
    'connect.rejected': 'the network rejected the data session',
}

# Set while a failure streak is active; the repeat limiter folds log lines only then.
STREAK_ACTIVE = threading.Event()


def stall_seconds(interval):
    return max(30, 10 * interval)


def log_label(device=None, serial=None, profile=None):
    """Journal/health label: the modem serial, with an IMEI-shaped value hashed."""
    value = (getattr(device, 'serial', None) or serial or profile or '')
    text = str(value)
    if re.fullmatch(r'[0-9]{15}', text):
        return 'sn-' + hashlib.sha256(text.encode()).hexdigest()[:8]
    return text


def public_identifier(value):
    """An identifier safe to publish: hash any IMEI-shaped run inside it.

    Automatic profile ids embed the USB serial ('auto-quectel-356938035643809'),
    so the plain string is not safe even when it is not a bare IMEI.
    """
    text = '' if value is None else str(value)
    if re.fullmatch(r'[0-9]{15}', text):
        return log_label(serial=text)
    return re.sub(r'[0-9]{15}', lambda match: log_label(serial=match.group(0)), text)


def public_device(device):
    """The device record with an IMEI-shaped serial replaced by its D3 label."""
    if device is None:
        return None
    value = asdict(device)
    label = log_label(serial=getattr(device, 'serial', None))
    if label:
        value['serial'] = label
    return value


def bounded_text(value, limit=DETAIL_MAX_BYTES):
    """Bound JSON-escaped bytes too: one emoji expands to twelve ASCII bytes."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return ''
    prefix, size = [], 0
    for character in text:
        cost = len(json.dumps(character, ensure_ascii=True)) - 2
        if size + cost > limit:
            return ''.join(prefix)
        prefix.append(character)
        size += cost
    return text


def bounded_params(params):
    """Keep only JSON-safe scalars, cut lists and bound string values."""
    result = {}
    try:
        items = list((params or {}).items())[:PARAMS_MAX_ITEMS]
    except Exception:
        return result
    for key, value in items:
        try:
            name = str(key)
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                result[name] = value
            elif isinstance(value, str):
                result[name] = bounded_text(value)
            elif isinstance(value, (list, tuple)):
                result[name] = [bounded_text(item) if isinstance(item, str) else item
                                for item in list(value)[:PARAMS_MAX_ITEMS]]
            else:
                result[name] = bounded_text(value)
        except Exception:
            continue
    return result


def annotate(error, code, **params):
    """Attach a diagnosis code to an exception; the innermost annotation wins."""
    if getattr(error, 'diagnosis', None) is None:
        try:
            error.diagnosis = {'code': code, 'params': params}
        except (AttributeError, TypeError):
            pass
    return error


@contextlib.contextmanager
def stage(name):
    """Tag the exception that crosses this block with the operation name."""
    try:
        yield
    except Exception as error:
        if getattr(error, 'operation', None) is None:
            try:
                setattr(error, 'operation', name)
            except (AttributeError, TypeError):
                pass
        raise


def _chain(error, depth=5):
    seen, current, result = set(), error, []
    while current is not None and len(result) < depth and id(current) not in seen:
        seen.add(id(current))
        result.append(current)
        cause = getattr(current, '__cause__', None)
        if cause is None and not getattr(current, '__suppress_context__', False):
            cause = getattr(current, '__context__', None)
        current = cause
    return result


def cause_of(error):
    """Classify the mechanism from attributes and types, never from message text."""
    try:
        if isinstance(error, TimeoutError):
            return 'timeout'
        domain, code = getattr(error, 'domain', None), getattr(error, 'code', None)
        if domain is not None and code is not None:
            try:
                number = int(code)
            except (TypeError, ValueError):
                number = None
            if domain == 'qmi_core_error_quark':
                if number == 2:
                    return 'timeout'
                if number == 0:
                    return 'core_failed'
            return f'{domain}:{number if number is not None else code}'
        errno = getattr(error, 'errno', None)
        if isinstance(error, OSError) and errno is not None:
            return f'os:{int(errno)}'
        return type(error).__name__
    except Exception:
        return 'unknown'


def classify(error, state):
    """Return {code, cause, operation, step, params, detail} or None for a shutdown."""
    if isinstance(error, InterruptedError):
        return None
    try:
        chain = _chain(error)
        code = params = None
        for item in chain:
            diagnosis = getattr(item, 'diagnosis', None)
            if isinstance(diagnosis, dict) and diagnosis.get('code'):
                code, params = diagnosis['code'], dict(diagnosis.get('params') or {})
                break
        operation = None
        for item in chain:
            value = getattr(item, 'operation', None)
            if value:
                operation = value
                break
        cause = None
        for item in reversed(chain):
            candidate = cause_of(item)
            if (candidate in {'timeout', 'core_failed'} or candidate.startswith('qmi_')
                    or candidate.startswith('os:')):
                cause = candidate
                break
        if cause is None:
            cause = cause_of(error)
        if code is None:
            if cause == 'timeout':
                code = 'qmi.no_answer'
            elif cause == 'core_failed':
                code = 'qmi.endpoint_hangup'
            elif cause.startswith('qmi_'):
                code = 'qmi.error'
            else:
                code = 'unknown'
        return {'code': code, 'cause': cause, 'operation': operation, 'step': state,
                'params': bounded_params(params or {}), 'detail': bounded_text(error)}
    except Exception:
        return {'code': 'unknown', 'cause': 'unknown', 'operation': None, 'step': state,
                'params': {}, 'detail': ''}


def _iso(value):
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f'{days} d {hours} h'
    if hours:
        return f'{hours} h {minutes} min'
    if minutes:
        return f'{minutes} min'
    return f'{seconds} s'


def bounded_diagnosis(value, limit=DIAGNOSIS_MAX_BYTES):
    """JSON-safe diagnosis object, small enough for every status response."""
    if not isinstance(value, dict):
        return None
    result = dict(value)
    detail = value.get('detail')
    result['detail'] = bounded_text(detail or '')
    result['params'] = bounded_params(value.get('params'))
    if diagnosis_size(result) <= limit:
        return result
    for size in (128, 64, 0):
        result['detail'] = bounded_text(detail or '', size)
        if diagnosis_size(result) <= limit:
            return result
    params = result['params']
    for bound in (64, 32):
        result['params'] = {key: (bounded_text(item, bound) if isinstance(item, str) else
                                  [bounded_text(entry, bound) if isinstance(entry, str) else entry
                                   for entry in item][:PARAMS_MAX_ITEMS] if isinstance(item, list) else item)
                            for key, item in params.items()}
        if diagnosis_size(result) <= limit:
            return result
    result['params'] = {}
    return result


def diagnosis_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=True, default=str))
    except (TypeError, ValueError):
        return DIAGNOSIS_MAX_BYTES + 1


class FailureTracker:
    """One active failure streak plus the last closed one."""

    def __init__(self, clock=time.monotonic, utcnow=None):
        self.clock = clock
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.previous = None
        self.attempts = 0
        self._failing_since = self._failing_since_utc = None
        self._reset_streak()

    def _reset_streak(self):
        self.signature = None
        self.code = self.step = self.operation = None
        self.params, self.detail = {}, ''
        self.count = 0
        self.since = self.last = None
        self.since_utc = self.last_utc = None
        self.severity = None
        self.escalated = False
        self.last_summary = self.last_summary_utc = None

    @property
    def active(self):
        return self.signature is not None and self.count > 0

    def failing_seconds(self):
        if self._failing_since is None:
            return 0
        return max(0, int(self.clock() - self._failing_since))

    def since_text(self):
        return self.since_utc.strftime('%Y-%m-%d %H:%M:%S') if self.since_utc else '-'

    def failing_since_text(self):
        return (self._failing_since_utc.strftime('%Y-%m-%d %H:%M:%S')
                if self._failing_since_utc else '-')

    def record(self, failure):
        now, utc = self.clock(), self.utcnow()
        code = failure.get('code') or 'unknown'
        signature = (failure.get('step'), failure.get('operation'), code)
        if self._failing_since is None:
            self._failing_since, self._failing_since_utc = now, utc
            self.attempts = 0
        if self.signature is not None and signature != self.signature:
            self.previous = {'code': self.code, 'resolution': 'changed', 'ended_utc': _iso(utc)}
            self._reset_streak()
        if self.count == 0:
            self.since = now
            self.since_utc = utc
        self.signature = signature
        self.code, self.step, self.operation = code, failure.get('step'), failure.get('operation')
        self.params = bounded_params(failure.get('params'))
        self.detail = bounded_text(failure.get('detail') or '')
        self.count += 1
        self.attempts += 1
        self.last, self.last_utc = now, utc
        self.severity = self._severity(code)
        escalated = self.severity == 'error' and not self.escalated
        if escalated:
            self.escalated = True
        STREAK_ACTIVE.set()
        return {'kind': self._kind(now), 'count': self.count, 'attempts': self.attempts,
                'severity': self.severity, 'escalated': escalated}

    def _kind(self, now):
        if self.count <= LOG_FIRST_ATTEMPTS:
            return 'logged'
        if self.count in LOG_SUMMARY_COUNTS:
            self.last_summary, self.last_summary_utc = now, self.utcnow()
            return 'summary'
        if (self.count > LOG_SUMMARY_COUNTS[-1] and self.last_summary is not None
                and now - self.last_summary >= LOG_SUMMARY_SECONDS):
            self.last_summary, self.last_summary_utc = now, self.utcnow()
            return 'summary'
        return 'folded'

    def _severity(self, code):
        policy = POLICIES.get(code, 'default')
        if policy == 'info':
            return 'info'
        if policy == 'immediate':
            return 'error'
        if policy == 'after_ladder' and self.params.get('ladder_tried'):
            return 'error'
        failing = self.failing_seconds()
        if ((self.attempts >= PERSISTENT_ATTEMPTS and failing >= PERSISTENT_SECONDS)
                or failing >= ALWAYS_PERSISTENT_SECONDS):
            return 'error'
        return 'warning'

    def step_passed(self, state):
        """A later state was reached; close the streak but not the failure episode."""
        if not self.active:
            return False
        if STATE_ORDER.get(str(state), -1) <= STATE_ORDER.get(str(self.step), -1):
            return False
        self.previous = {'code': self.code, 'resolution': 'step_passed', 'ended_utc': _iso(self.utcnow())}
        code = self.code
        self._reset_streak()
        self.code = code
        STREAK_ACTIVE.clear()
        return True

    def success(self):
        if self.signature is None and not self.attempts:
            return None
        summary = {'attempts': self.attempts, 'duration_s': self.failing_seconds(),
                   'code': self.code}
        self.previous = {'code': self.code, 'resolution': 'recovered', 'ended_utc': _iso(self.utcnow())}
        self._reset_streak()
        self.attempts = 0
        self._failing_since = self._failing_since_utc = None
        STREAK_ACTIVE.clear()
        return summary

    def snapshot(self):
        if not self.active:
            return {'active': False, 'previous': self.previous} if self.previous else None
        self.severity = self._severity(self.code)
        return {'code': self.code, 'severity': self.severity, 'active': True,
                'step': self.step, 'operation': self.operation, 'params': dict(self.params),
                'count': self.count, 'since_utc': _iso(self.since_utc),
                'last_utc': _iso(self.last_utc), 'attempts': self.attempts,
                'failing_since_utc': _iso(self._failing_since_utc),
                'failing_seconds': self.failing_seconds(), 'transient': False,
                'previous': self.previous, 'detail': self.detail,
                'observed_at_utc': _iso(self.utcnow())}

    def to_record(self):
        if not self.active and not self.attempts:
            return None
        return {'code': self.code, 'step': self.step, 'operation': self.operation,
                'signature': list(self.signature) if self.signature else None,
                'params': dict(self.params), 'detail': self.detail, 'count': self.count,
                'attempts': self.attempts, 'since_utc': _iso(self.since_utc),
                'last_utc': _iso(self.last_utc),
                'failing_since_utc': _iso(self._failing_since_utc),
                'error_logged': self.escalated,
                'last_summary_utc': _iso(self.last_summary_utc),
                'previous': self.previous}

    def from_record(self, record):
        """Restore wall-clock times and rebuild the monotonic ones."""
        if not record:
            return self
        now_utc, now = self.utcnow(), self.clock()

        def monotonic(value):
            parsed = _parse(value)
            if parsed is None:
                return now
            return now - max(0.0, (now_utc - parsed).total_seconds())

        self.code = record.get('code')
        self.step, self.operation = record.get('step'), record.get('operation')
        self.params = bounded_params(record.get('params'))
        self.detail = bounded_text(record.get('detail') or '')
        self.count = int(record.get('count') or 0)
        self.attempts = int(record.get('attempts') or 0)
        self.since_utc, self.since = _parse(record.get('since_utc')), monotonic(record.get('since_utc'))
        self.last_utc, self.last = _parse(record.get('last_utc')), monotonic(record.get('last_utc'))
        self._failing_since_utc = _parse(record.get('failing_since_utc'))
        self._failing_since = (monotonic(record.get('failing_since_utc'))
                               if self._failing_since_utc is not None else None)
        self.previous = record.get('previous')
        self.signature = ((self.step, self.operation, self.code) if self.count > 0 else None)
        self.severity = self._severity(self.code) if self.count > 0 else None
        self.escalated = bool(record.get('error_logged', self.severity == 'error'))
        self.last_summary_utc = _parse(record.get('last_summary_utc'))
        self.last_summary = (monotonic(record.get('last_summary_utc'))
                             if self.last_summary_utc is not None else None)
        if self.count > 0:
            STREAK_ACTIVE.set()
        return self
