import assert from 'node:assert/strict';
import test from 'node:test';
import {
  CODE_TEXT, OPERATION_LABEL, VALUE_LABEL, attentionTone, collectIssues, diagnosisIssue,
  formatPlmn, issueText, issueTone,
} from '../lib/diagnosis.ts';
import type { Issue, IssueSources, IssueTone } from '../lib/diagnosis.ts';
import { getLocale, messages, setLocale, t } from '../lib/messages.ts';
import type { ModemRecord } from '../lib/types';

const entry = (values: Partial<ModemRecord> = {}): ModemRecord => ({
  id: 'default', detected: true, configured: true, state: 'ONLINE',
  connection_enabled: true, monitoring_enabled: true, ...values,
});

/** Every code published by modules/runtime/diagnosis.py LOG_TITLES/POLICIES. */
const BACKEND_CODES = [
  'qmi.no_answer', 'qmi.endpoint_hangup', 'qmi.error', 'unknown', 'device.removed', 'device.locked',
  'host.modemmanager_active', 'host.udhcpc_missing', 'config.pending', 'worker.restarting',
  'worker.starting', 'session.lost', 'ip.incomplete', 'telemetry.stalled',
  'recovery.saved_client_unanswered', 'recovery.cleanup_unconfirmed',
  'recovery.host_cleanup_unconfirmed', 'recovery.foreign_record',
  'sim.absent', 'sim.no_atr', 'sim.contact_lost', 'sim.card_error', 'sim.not_reported',
  'sim.initializing', 'sim.pin_required', 'sim.puk_required', 'sim.blocked', 'sim.network_locked',
  'sim.app_illegal', 'sim.in_inactive_slot', 'sim.operator_action_required',
  'registration.modem_silent', 'registration.sim_invalidated', 'registration.rejected',
  'registration.home_not_found', 'registration.limited_service', 'registration.no_coverage',
  'registration.restricted', 'registration.wrong_network', 'registration.timeout',
  'startup.transient', 'connect.rejected',
];

test('every backend diagnosis code exists with a non-empty title', () => {
  for (const [code, text] of Object.entries(CODE_TEXT)) {
    assert.ok(text.title.trim().length > 0, `empty title for ${code}`);
  }
  for (const code of BACKEND_CODES) {
    const text = CODE_TEXT[code];
    assert.ok(text, `missing code ${code}`);
    assert.ok(text.title.trim().length > 0, `empty title for ${code}`);
  }
});

test('every message ID referenced by the tables exists in the catalogue', () => {
  const known = new Set<string>(Object.keys(messages));
  const missing = new Set<string>();
  const require = (id: string | undefined) => { if (id && !known.has(id)) missing.add(id); };

  for (const text of Object.values(CODE_TEXT)) {
    require(text.title);
    require(text.explanation);
    require(text.action);
  }
  for (const label of Object.values(OPERATION_LABEL)) require(label);
  for (const table of Object.values(VALUE_LABEL)) for (const label of Object.values(table)) require(label);

  assert.deepEqual([...missing].sort(), [],
    'These message IDs are missing from frontend/lib/messages.ts: ' + [...missing].sort().join(', '));
});

test('a PLMN code is split after its MCC and shorter or longer values stay safe', () => {
  assert.equal(formatPlmn('99910'), '999-10');
  assert.equal(formatPlmn('999100'), '999-100');
  assert.equal(formatPlmn('999'), '999');
  assert.equal(formatPlmn(''), '');
  assert.equal(formatPlmn('1'), '1');
});

test('an inactive diagnosis produces no issue', () => {
  assert.equal(diagnosisIssue({ code: 'qmi.no_answer', severity: 'error', active: false }), null);
  assert.equal(diagnosisIssue(null), null);
  assert.equal(diagnosisIssue(undefined), null);
  assert.equal(diagnosisIssue(null, '   '), null);
});

test('an unknown code with an error becomes the generic issue and the raw error stays in technical detail', () => {
  const marker = 'RAW-ERROR-MARKER /dev/cdc-wdm0';
  const issue = diagnosisIssue({ code: 'not.a.code', severity: 'error', active: true, step: 'OPEN' }, marker);
  assert.ok(issue);
  assert.equal(issue.title, 'This modem could not finish its last step.');
  assert.equal(issue.tone, 'warning', 'a coded failure unknown to the UI is never bad on its own');
  assert.equal(issue.explanation, undefined);
  assert.equal(issue.action, undefined);
  assert.equal(issue.technical?.detail, marker);
  assert.equal(issue.technical?.code, 'not.a.code');
  assert.equal(issue.technical?.step, 'OPEN');
  const withoutDetail = { ...issue, technical: { ...issue.technical, detail: undefined } };
  assert.ok(!JSON.stringify(withoutDetail).includes(marker), 'the raw error must not leak outside technical.detail');
});

test('a missing diagnosis with an error becomes the generic issue too', () => {
  const issue = diagnosisIssue(null, 'boom');
  assert.ok(issue);
  assert.equal(issue.id, 'diagnosis.generic');
  assert.equal(issue.tone, 'warning');
  assert.equal(issue.technical?.detail, 'boom');
  assert.equal(issue.title, 'This modem could not finish its last step.');
});

test('a known code translates its operation and enum parameters', () => {
  const issue = diagnosisIssue({
    code: 'registration.rejected', severity: 'warning', active: true, step: 'WAIT_REGISTRATION',
    operation: 'register.wait', params: { plmn: '99910', rat: 'lte', cause: '#22' },
    failing_since_utc: '2026-01-01T00:00:00Z', attempts: 4,
  }, 'raw qmi text', { nextSeconds: 30, label: 'modem[18d94aeb]' });
  assert.ok(issue);
  assert.equal(issue.tone, 'warning');
  assert.equal(issue.title, '{plmn} rejects this SIM on {rat}: {cause}');
  assert.equal(issue.vars?.plmn, '99910', 'values pass through as published; formatPlmn is a separate helper');
  assert.equal(issue.vars?.rat, 'LTE');
  assert.equal(issue.vars?.cause, '#22', 'unknown enum values pass through');
  assert.equal(issue.vars?.operation, 'waiting for network registration');
  assert.equal(issue.vars?.step, 'WAIT_REGISTRATION');
  assert.equal(issue.since, '2026-01-01T00:00:00Z');
  assert.equal(issue.attempts, 4);
  assert.equal(issue.nextSeconds, 30);
  assert.equal(issue.technical?.label, 'modem[18d94aeb]');
  assert.equal(issue.technical?.detail, 'raw qmi text');
  assert.equal(issue.technical?.operation, 'register.wait', 'the technical row keeps the raw operation tag');
});

test('attentionTone keeps only the highest non-info tone', () => {
  const issue = (id: string, tone: IssueTone): Issue => ({ id, tone, title: id });
  assert.equal(attentionTone([]), null);
  assert.equal(attentionTone([issue('a', 'info')]), null);
  assert.equal(attentionTone([issue('a', 'info'), issue('b', 'warning')]), 'warning');
  assert.equal(attentionTone([issue('a', 'warning'), issue('b', 'info')]), 'warning');
  assert.equal(attentionTone([issue('a', 'warning'), issue('b', 'bad')]), 'bad');
});

test('issueTone uses the diagnosis severity and never makes a plain error bad', () => {
  assert.equal(issueTone(entry({ diagnosis: { code: 'x', severity: 'error', active: true } })), 'bad');
  assert.equal(issueTone(entry({ diagnosis: { code: 'x', severity: 'warning', active: true } })), 'warning');
  assert.equal(issueTone(entry({ diagnosis: { code: 'x', severity: 'info', active: true } })), 'info');
  assert.equal(issueTone(entry({ error: 'actor failed' })), 'warning');
  assert.equal(issueTone(entry({ error: 'fleet failed', error_source: 'fleet' })), 'warning');
  assert.equal(issueTone(entry({ diagnosis: { code: 'x', severity: 'error', active: false } })), null);
  assert.equal(issueTone(entry()), null);
});

test('collectIssues sorts bad before warning before info and collects each source', () => {
  const sources: IssueSources = {
    snapshot: { state: 'WAIT_REGISTRATION' },
    entry: entry({
      state: 'WAIT_REGISTRATION',
      diagnosis: {
        code: 'qmi.no_answer', severity: 'error', active: true, step: 'CONNECT', operation: 'connect.start',
        params: {}, failing_since_utc: '2026-01-01T09:00:00Z', attempts: 3,
      },
      features: { telemetry: { stall: { since_utc: '2026-01-01T10:00:00Z', reason_code: 'modem_absent' } } },
    }),
    config: { telemetry: { enabled: false } },
    apn: { warning: 'The modem does not report the effective APN.', tone: 'warning' },
    routes: {
      conflict: { network: '10.20.0.0/24', overlaps: '192.168.1.0/24', dev: 'eth0' },
      error: 'RTNETLINK answers: File exists',
    },
    traffic: { enabled: true, error: 'sysfs read failed' },
    gps: { enabled: true, error: 'gpsd is not running' },
    attachFallback: 'The data session used another initial attach profile.',
    modemId: 'default',
    nextSeconds: 15,
  };
  const issues = collectIssues(sources);
  const toneOrder: Record<IssueTone, number> = { bad: 0, warning: 1, info: 2 };
  for (let index = 1; index < issues.length; index += 1) {
    assert.ok(toneOrder[issues[index - 1].tone] <= toneOrder[issues[index].tone],
      `issue ${issues[index].id} is out of order`);
  }
  const byId = Object.fromEntries(issues.map(issue => [issue.id, issue]));
  assert.equal(issues[0].id, 'diagnosis.qmi.no_answer');
  assert.equal(byId['diagnosis.qmi.no_answer'].tone, 'bad');

  const stall = byId['telemetry.stalled'];
  assert.ok(stall);
  assert.equal(stall.tone, 'warning', 'a stall is never an info issue');
  assert.equal(stall.title, 'Monitoring has stored no sample since {time}');
  assert.equal(stall.vars?.time, '2026-01-01 10:00 UTC');
  assert.equal(stall.explanation, 'The modem is not detected.');

  assert.equal(byId['monitoring.disabled']?.tone, 'info');
  assert.equal(byId['monitoring.disabled']?.title, 'Monitoring is disabled');
  assert.equal(byId['monitoring.disabled']?.explanation, 'The session can continue without acquiring new samples.');
  assert.equal(byId['registration.waiting']?.tone, 'info');
  assert.equal(byId['apn.warning']?.title, 'The modem does not report the effective APN.');
  assert.equal(byId['apn.attach_fallback']?.tone, 'info');

  const conflict = byId['routes.conflict'];
  assert.ok(conflict);
  assert.equal(conflict.tone, 'warning');
  assert.equal(conflict.title, 'Route conflict');
  assert.equal(conflict.vars?.network, '10.20.0.0/24');
  assert.deepEqual(conflict.link, { href: '/configuration/modem/?modem=default&section=routing', label: 'Review the routes' });
  assert.equal(byId['routes.error']?.link?.href, '/configuration/web/?section=log&modem=default');

  assert.equal(byId['traffic.error']?.tone, 'info');
  assert.equal(byId['traffic.error']?.technical?.detail, 'sysfs read failed');
  assert.equal(byId['gps.error']?.tone, 'info');
  assert.equal(byId['gps.error']?.technical?.detail, 'gpsd is not running');
});

test('a stall while the modem cannot be read explains the failing step', () => {
  const sources: IssueSources = {
    snapshot: {
      state: 'CONNECT',
      diagnosis: { code: 'qmi.no_answer', severity: 'warning', active: true, step: 'CONNECT', params: {} },
      features: { telemetry: { stall: { since_utc: '2026-02-03T04:05:00Z', reason_code: 'modem_not_readable' } } },
    },
    config: null,
  };
  const stall = collectIssues(sources).find(issue => issue.id === 'telemetry.stalled');
  assert.ok(stall);
  assert.equal(stall.explanation, 'The modem cannot be read while its connection keeps failing (step {step}). Fix that problem first.');
  assert.equal(stall.vars?.step, 'CONNECT');
});

test('a registration diagnosis replaces the waiting notice, and disabled traffic or GPS adds no issue', () => {
  const sources: IssueSources = {
    snapshot: {
      state: 'WAIT_REGISTRATION',
      diagnosis: { code: 'registration.timeout', severity: 'warning', active: true, params: { seconds: 60 } },
    },
    config: null,
    traffic: { enabled: false, error: 'not measured' },
    gps: { enabled: false, error: 'not measured' },
  };
  const issues = collectIssues(sources);
  assert.deepEqual(issues.map(issue => issue.id), ['diagnosis.registration.timeout']);
  assert.ok(!issues.some(issue => issue.id === 'registration.waiting'));
  assert.ok(!issues.some(issue => issue.id === 'traffic.error'));
  assert.ok(!issues.some(issue => issue.id === 'gps.error'));
});

test('Spanish also translates the interpolated operation and enum values', () => {
  try {
    setLocale('es');
    const operation = diagnosisIssue({
      code: 'qmi.no_answer', severity: 'warning', active: true, operation: 'register.wait', params: {},
    });
    assert.equal(operation?.vars?.operation, t('waiting for network registration'));
    assert.notEqual(operation?.vars?.operation, 'waiting for network registration');
    const card = diagnosisIssue({
      code: 'sim.card_error', severity: 'warning', active: true, params: { slot: 1, error: 'power-down' },
    });
    assert.equal(card?.vars?.error, t('powered down'));
    assert.notEqual(card?.vars?.error, 'powered down');
    assert.equal(card?.vars?.slot, 1, 'numbers pass through unchanged');
  } finally {
    setLocale('en');
  }
});

test('a placeholder without a value never reaches the panel', () => {
  // Discovery failures have no operation; the coded explanation would render '{operation}'.
  const issue = diagnosisIssue({ code: 'unknown', severity: 'warning', active: true, params: {} });
  assert.ok(issue);
  assert.equal(issue.explanation, undefined);
  assert.equal(issueText('The step failed while {operation}.'), 'The step failed while.');
  assert.equal(issueText('This happened while {operation}.', { operation: 'opening the modem' }),
    'This happened while opening the modem.');
});

test('a stall without a failing step keeps the generic cause and no borrowed retry', () => {
  const issues = collectIssues({
    snapshot: { state: 'BACKOFF', retry_in_seconds: 30,
      features: { telemetry: { stall: { since_utc: '2026-01-01T10:00:00Z', reason_code: 'modem_not_readable' } } } },
    config: null,
  });
  const stall = issues.find(issue => issue.id === 'telemetry.stalled');
  assert.ok(stall);
  assert.equal(stall.explanation, 'The modem did not complete the acquisition.');
  assert.equal(stall.nextSeconds, undefined, 'the connection retry is not monitoring\'s');
});

test('a fleet-level error is shown before an actor diagnosis', () => {
  const fleet_error = entry({ error: 'bad configuration', error_source: 'fleet',
    diagnosis: { code: 'worker.starting', severity: 'info', active: true } });
  assert.equal(issueTone(fleet_error), 'warning');
});

test('Spanish translates a code title and the locale returns to English', () => {
  try {
    setLocale('es');
    assert.equal(t(CODE_TEXT['qmi.no_answer'].title), 'El módem no respondió a tiempo');
  } finally {
    setLocale('en');
  }
  assert.equal(getLocale(), 'en');
  assert.equal(t(CODE_TEXT['qmi.no_answer'].title), 'The modem did not answer in time');
});
