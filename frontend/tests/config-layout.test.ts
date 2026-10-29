import assert from 'node:assert/strict';
import test from 'node:test';
import { configFields } from '../lib/config.ts';
import { acquisitionBudget, displayValue, formatDuration, hiddenRowCount, identificationMode, listenMode, plmnMode, plmnParts,
  accountSections, profileSections, queryCost, routeRole, routeRoleChanges, rowVisible, saveEffects, sectionFields, sectionOf, sectionVisible,
  systemSections, visibleSections } from '../lib/config-layout.ts';
import type { VisibilityContext } from '../lib/config-layout.ts';
import type { Config } from '../lib/types';

function configuration(): Config {
  return {
    modem: { device: 'auto', serial: '', apn: 'internet', plmn: '', mode: 'connect' },
    network: { method: 'qmi', route_metric: 700, default_route: true, dns: 'auto', route_policy: 'fixed', connectivity_check: true,
      connectivity_targets: '1.1.1.1:443,8.8.8.8:53', connectivity_interval: 3, connectivity_timeout: 1, connectivity_failures: 2, connectivity_successes: 2 },
    recovery: { retry_initial: 2, retry_max: 60, registration_timeout: 180, operation_timeout: 20, poll_interval: 10 },
    telemetry: { enabled: true, interval: 1, directory: 'files', csv: true, json: true, udp_host: '', udp_port: 5006, collector_host: '', collector_port: 5007,
      throughput: false, gps: false, gps_source: 'gpsd', queue_size: 64, flush_interval: 1, gps_max_age: 10, request_timeout: 1, collection_budget: .8, profile: 'full',
      query_signal: 1, query_cell: 1, query_txrx: 1, query_serving: 5, query_bands: 10, query_strength: 0, query_home: 3600,
      query_wds_stats: 1, query_wds_status: 1, query_wds_rates: 10, indications: 'off' },
    web: { enabled: true, host: '127.0.0.1', port: 8080, history_size: 120, username: 'admin', session_timeout: 28800 },
  };
}
const context = (draft: Config, values: Partial<VisibilityContext> = {}): VisibilityContext => ({ draft, advanced: false, errors: {}, changes: {}, ...values });
const row = (section: string, id: string) => [...profileSections, ...systemSections, ...accountSections].find(item => item.id === section)!.rows.find(item => item.id === id)!;

test('every setting appears exactly once, so nothing is unreachable or edited in two places', () => {
  const listed = [...profileSections, ...systemSections, ...accountSections].flatMap(sectionFields);
  assert.equal(new Set(listed).size, listed.length, 'duplicated fields');
  const expected = Object.entries(configFields).flatMap(([section, fields]) => Object.keys(fields).map(key => section + '.' + key));
  assert.deepEqual([...listed].sort(), expected.sort());
  assert.equal(sectionOf('network.connectivity_timeout')?.id, 'verification');
  assert.equal(sectionOf('telemetry.enabled')?.id, 'capture');
  assert.equal(sectionOf('web.password')?.id, 'account');
});

test('basic mode hides rarely changed settings but never an error, a pending change or a setting in use', () => {
  const draft = configuration();
  assert.equal(rowVisible(row('routing', 'dns'), context(draft)), false);
  assert.equal(rowVisible(row('routing', 'dns'), context(draft, { advanced: true })), true);
  assert.equal(rowVisible(row('routing', 'dns'), context(draft, { changes: { network: { dns: 'none' } } })), true);
  assert.equal(rowVisible(row('verification', 'rhythm'), context(draft, { errors: { 'network.connectivity_timeout': 'x' } })), true);
  draft.network.dns = 'resolvectl';
  assert.equal(rowVisible(row('routing', 'dns'), context(draft)), true, 'a non-default DNS mode stays visible');
  draft.telemetry.udp_host = '192.0.2.8';
  assert.equal(rowVisible(row('destinations', 'udp'), context(draft)), true, 'a configured destination stays visible');
  assert.equal(rowVisible(row('destinations', 'collector'), context(draft)), false);
  assert.equal(rowVisible(row('routing', 'metric'), context(draft)), true, 'a fixed metric is the basic choice');
  draft.network.route_policy = 'backup';
  assert.equal(rowVisible(row('routing', 'metric'), context(draft)), false);
  assert.equal(rowVisible(row('connection', 'identify'), context(draft)), false);
  draft.modem.serial = 'pinned';
  assert.equal(rowVisible(row('connection', 'identify'), context(draft)), false, 'automatic profiles always pin a serial');
});

test('sections with only advanced rows disappear from the menu until needed', () => {
  const draft = configuration(), recovery = profileSections.find(section => section.id === 'recovery')!;
  assert.equal(sectionVisible(recovery, context(draft)), false);
  assert.equal(sectionVisible(recovery, context(draft, { advanced: true })), true);
  assert.equal(sectionVisible(recovery, context(draft, { errors: { 'recovery.retry_max': 'x' } })), true);
  assert.deepEqual(visibleSections(profileSections, context(draft)).map(section => section.id),
    ['connection', 'routing', 'verification', 'capture', 'gps', 'destinations']);
  const capture = profileSections.find(section => section.id === 'capture')!;
  assert.equal(hiddenRowCount(capture, context(draft)), 3, 'indications, budget and query timeout');
});

test('rows for options this backend does not expose are not rendered', () => {
  const draft = configuration();
  for (const key of Object.keys(draft.telemetry)) if (key.startsWith('query_') || key === 'indications') delete draft.telemetry[key];
  assert.equal(rowVisible(row('capture', 'indications'), context(draft, { advanced: true })), false);
  assert.equal(rowVisible(row('capture', 'profile'), context(draft)), true, 'the profile row remains');
  assert.equal(rowVisible(row('account', 'password'), context(draft)), true, 'the transient password row needs no stored field');
});

test('route roles translate to the stored policy, default route and reference metrics', () => {
  assert.equal(routeRole({ default_route: false, route_policy: 'primary' }), 'none');
  assert.equal(routeRole({ default_route: true, route_policy: 'backup' }), 'backup');
  assert.equal(routeRole({ default_route: true, route_policy: 'fixed' }), 'fixed');
  assert.deepEqual(routeRoleChanges('primary'), { default_route: true, route_policy: 'primary', route_metric: 100 });
  assert.deepEqual(routeRoleChanges('backup'), { default_route: true, route_policy: 'backup', route_metric: 700 });
  assert.deepEqual(routeRoleChanges('none'), { default_route: false });
  assert.deepEqual(routeRoleChanges('fixed'), { default_route: true, route_policy: 'fixed' });
});

test('composite choices read the stored selectors', () => {
  assert.equal(identificationMode({ device: 'auto', serial: '' }), 'auto');
  assert.equal(identificationMode({ device: 'auto', serial: 'ABC' }), 'serial');
  assert.equal(identificationMode({ device: '/dev/cdc-wdm1', serial: '' }), 'port');
  assert.deepEqual([plmnMode(''), plmnMode('auto'), plmnMode('99910')], ['keep', 'auto', 'fixed']);
  assert.deepEqual(plmnParts('99910'), { mcc: '999', mnc: '10' });
  assert.deepEqual(plmnParts('214070'), { mcc: '214', mnc: '070' });
  assert.equal(plmnParts('auto'), null);
  assert.deepEqual([listenMode('127.0.0.1'), listenMode('::1'), listenMode('0.0.0.0'), listenMode('::'), listenMode('auto'), listenMode('192.168.1.2')],
    ['local', 'local', 'all', 'all', 'auto', 'custom']);
});

test('query costs and the acquisition budget come from the serialized request estimate', () => {
  const telemetry = { ...configuration().telemetry, profile: 'custom' };
  assert.deepEqual(queryCost(telemetry, 'signal'), { kind: 'ms', ms: 32 });
  assert.deepEqual(queryCost(telemetry, 'bands'), { kind: 'ms', ms: 3.2 });
  assert.deepEqual(queryCost(telemetry, 'strength'), { kind: 'off' });
  assert.deepEqual(queryCost({ ...telemetry, indications: 'on' }, 'wds_stats'), { kind: 'push' });
  const budget = acquisitionBudget(telemetry);
  assert.equal(budget.budget, 800); assert.equal(budget.over, false); assert.ok(budget.ratio > 0 && budget.ratio < 1);
  assert.equal(acquisitionBudget({ ...telemetry, collection_budget: .1 }).over, true);
});

test('values are shown with their option labels, units and readable durations', () => {
  assert.deepEqual(displayValue('modem.mode', 'monitor'), { text: 'Monitoring only', translate: true });
  assert.deepEqual(displayValue('network.connectivity_interval', 3), { text: '3 s', translate: false });
  assert.deepEqual(displayValue('telemetry.csv', false), { text: 'Disabled', translate: true });
  assert.deepEqual(displayValue('modem.plmn', ''), { text: 'Blank', translate: true });
  assert.equal(formatDuration(28800), '8 h'); assert.equal(formatDuration(90), '1 min 30 s'); assert.equal(formatDuration(30), null);
});

test('save effects name what is interrupted, and only a saved connect mode restarts the data session', () => {
  const saved = configuration();
  assert.deepEqual(saveEffects({ modem: { apn: 'x' } }, saved, 'profile'), ['Applying restarts the data session.']);
  saved.modem.mode = 'monitor';
  assert.deepEqual(saveEffects({ modem: { apn: 'x' } }, saved, 'profile'), []);
  assert.deepEqual(saveEffects({ modem: { serial: 'x' }, telemetry: { interval: 2 } }, saved, 'profile'),
    ['Applying reopens the modem; data and telemetry pause briefly.']);
  assert.deepEqual(saveEffects({ telemetry: { interval: 2 } }, saved, 'profile'), ['Applying restarts telemetry.']);
  assert.deepEqual(saveEffects({ web: { port: 8081 } }, saved, 'web'), ['Applying restarts web access.']);
  assert.deepEqual(saveEffects({ system: { log_level: 'DEBUG' } }, saved, 'web'), ['Applying restarts the supervisor workers.']);
  assert.deepEqual(saveEffects({ web: { password: 'x' } }, saved, 'account'), ['You will need to sign in again.']);
  assert.deepEqual(saveEffects({ web: { session_timeout: 60 } }, saved, 'account'), ['Applying updates the session lifetime.']);
});
