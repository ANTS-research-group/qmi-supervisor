import assert from 'node:assert/strict';
import test from 'node:test';
import { configFields, countChanges, customFromProfile, estimateAcquisition, fieldAvailability, queryPeriods, diffConfig, isNumericIP, sanitizeConfig, validateConfig } from '../lib/config.ts';
import type { Config } from '../lib/types';

function configuration(): Config {
  return {
    modem: { device: 'auto', serial: '', apn: 'internet', plmn: '', mode: 'connect' },
    network: { method: 'qmi', route_metric: 700, default_route: true, dns: 'auto', route_policy: 'fixed', connectivity_check: true,
      connectivity_targets: '1.1.1.1:443,8.8.8.8:53', connectivity_interval: 3, connectivity_timeout: 1, connectivity_failures: 2, connectivity_successes: 2 },
    recovery: { retry_initial: 2, retry_max: 60, registration_timeout: 180, operation_timeout: 20, poll_interval: 10 },
    telemetry: { enabled: true, interval: 1, directory: 'files', csv: true, json: true, udp_host: '', udp_port: 5006, collector_host: '', collector_port: 5007, throughput: false, gps: false, gps_source: 'gpsd', queue_size: 64, flush_interval: 1, gps_max_age: 10, request_timeout: 1, collection_budget: .8, profile: 'full',
      query_signal: 1, query_cell: 1, query_txrx: 1, query_serving: 5, query_bands: 10, query_strength: 0, query_home: 3600,
      query_wds_stats: 1, query_wds_status: 1, query_wds_rates: 10, indications: 'off' },
    web: { enabled: true, host: '127.0.0.1', port: 8080, history_size: 120, max_workers: 8, address_refresh: 10, poll_interval: 2,
      username: 'admin', session_timeout: 28800 },
    system: { log_level: 'INFO' },
  };
}

test('all public runtime fields have typed form definitions', () => {
  for (const [section, fields] of Object.entries(configuration())) {
    assert.deepEqual(Object.keys(configFields[section]).sort(), Object.keys(fields).sort(), section);
  }
});

test('units live in the control, never in the label', () => {
  for (const [section, fields] of Object.entries(configFields)) {
    for (const [key, definition] of Object.entries(fields)) {
      assert.ok(!/\((s|ms)\)/.test(definition.label), section + '.' + key + ': ' + definition.label);
      if (definition.control === 'segmented') assert.ok(definition.options && definition.options.length <= 4, section + '.' + key);
    }
  }
  assert.equal(configFields.network.connectivity_interval.unit, 's');
  assert.equal(configFields.telemetry.queue_size.unit, 'samples');
});

test('valid defaults retain automatic modem selection, blank selectors and DNS modes', () => {
  const draft = configuration();
  assert.deepEqual(validateConfig(draft), {});
  draft.network.default_route = false;
  draft.network.dns = 'none';
  draft.modem.mode = 'monitor';
  draft.modem.apn = '';
  assert.deepEqual(validateConfig(draft), {});
  assert.equal(draft.modem.device, 'auto');
  assert.equal(draft.modem.serial, '');
});

test('a shared draft sends changes from several tabs together without sending defaults', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.modem.apn = 'private.example';
  draft.telemetry.throughput = true;
  draft.web.host = 'auto';
  const changes = diffConfig(base, draft);
  assert.deepEqual(changes, { modem: { apn: 'private.example' }, telemetry: { throughput: true }, web: { host: 'auto' } });
  assert.equal(countChanges(changes), 3);
  assert.equal(base.modem.apn, 'internet');
  assert.equal(base.telemetry.throughput, false);
});

test('reverting fields removes them from the pending payload and missing configuration is never a write', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.network.route_metric = 100;
  draft.network.default_route = false;
  draft.network.route_metric = base.network.route_metric;
  assert.deepEqual(diffConfig(base, draft), { network: { default_route: false } });
  draft.network.default_route = true;
  assert.deepEqual(diffConfig(base, draft), {});
  assert.deepEqual(diffConfig(null, draft, 'new password'), {});
  assert.deepEqual(diffConfig(base, null), {});
});

test('blank password preserves credentials and a replacement appears only in the transient payload', () => {
  const base = configuration(), draft = structuredClone(base);
  assert.deepEqual(diffConfig(base, draft, ''), {});
  assert.deepEqual(diffConfig(base, draft, '  new password  '), { web: { password: '  new password  ' } });
  assert.equal(Object.hasOwn(draft.web, 'password'), false);
  assert.equal(countChanges(diffConfig(base, draft, 'a new password')), 1);
});

test('diagnostic JSON and regular diffs omit passwords and hashes without mutating input', () => {
  const draft = configuration();
  Object.assign(draft.web, { password: 'private password', password_confirm: 'private password', password_confirmation: 'private password', password_hash: 'private hash' });
  const sanitized = sanitizeConfig(draft);
  assert.deepEqual(sanitized, configuration());
  assert.equal(draft.web.password, 'private password');
  assert.deepEqual(diffConfig(configuration(), draft), {});
  sanitized.modem.apn = 'different';
  assert.equal(draft.modem.apn, 'internet');
  assert.equal(/private/.test(JSON.stringify(sanitizeConfig(draft))), false);
});

test('fractional scheduling and zero route metric remain typed while timing relationships are checked', () => {
  const draft = configuration();
  draft.telemetry.interval = .5;
  draft.telemetry.collection_budget = .4;
  draft.network.route_metric = 0;
  assert.deepEqual(validateConfig(draft), {});
  draft.telemetry.collection_budget = .6;
  draft.recovery.retry_max = 1;
  let errors = validateConfig(draft);
  assert.ok(errors['telemetry.collection_budget']);
  assert.ok(errors['recovery.retry_max']);
  draft.recovery.poll_interval = 0;
  draft.telemetry.flush_interval = Infinity;
  draft.telemetry.request_timeout = 1.5;
  errors = validateConfig(draft);
  assert.ok(errors['recovery.poll_interval']);
  assert.ok(errors['telemetry.flush_interval']);
  assert.ok(errors['telemetry.request_timeout']);
});

test('numeric boundaries report errors by field for ports, queue, history and sessions', () => {
  const draft = configuration();
  for (const [section, key, minimum, maximum] of [
    ['telemetry', 'udp_port', 1, 65535], ['telemetry', 'collector_port', 1, 65535], ['web', 'port', 1, 65535],
    ['telemetry', 'queue_size', 1, 4096], ['telemetry', 'request_timeout', 1, 60], ['web', 'history_size', 1, 1000], ['web', 'session_timeout', 300, 604800], ['network', 'route_metric', 0, 4294967295],
  ] as const) {
    draft[section][key] = minimum;
    assert.equal(validateConfig(draft)[`${section}.${key}`], undefined);
    draft[section][key] = maximum;
    assert.equal(validateConfig(draft)[`${section}.${key}`], undefined);
    draft[section][key] = maximum + 1;
    assert.ok(validateConfig(draft)[`${section}.${key}`]);
    draft[section][key] = minimum - 1;
    assert.ok(validateConfig(draft)[`${section}.${key}`]);
    draft[section][key] = minimum;
  }
});

test('NaN, booleans masquerading as numbers and unsupported enums cannot be saved', () => {
  const draft = configuration();
  draft.telemetry.interval = NaN;
  draft.network.route_metric = true;
  draft.telemetry.gps = 'true';
  draft.telemetry.profile = 'inferred';
  draft.telemetry.gps_source = 'automatic';
  const errors = validateConfig(draft);
  for (const path of ['telemetry.interval', 'network.route_metric', 'telemetry.gps', 'telemetry.profile', 'telemetry.gps_source']) assert.ok(errors[path], path);
});

test('query periods apply only to the custom profile and keep bearer status while connecting', () => {
  const draft = configuration();
  assert.ok(fieldAvailability(draft, 'telemetry', 'query_bands').disabled);
  draft.telemetry.profile = 'custom';
  assert.equal(fieldAvailability(draft, 'telemetry', 'query_bands').disabled, false);
  draft.telemetry.query_wds_status = 0;
  assert.ok(validateConfig(draft)['telemetry.query_wds_status']);
  draft.modem.mode = 'monitor';
  assert.equal(validateConfig(draft)['telemetry.query_wds_status'], undefined);
  draft.telemetry.query_bands = -1;
  assert.ok(validateConfig(draft)['telemetry.query_bands']);
});

test('customizing a fixed profile keeps its exact query groups and estimates serialized requests', () => {
  const telemetry = configuration().telemetry;
  assert.deepEqual(estimateAcquisition(telemetry), { average: 312, worst: 312 });
  const fast = customFromProfile({ ...telemetry, profile: 'fast' });
  assert.deepEqual(Object.entries(fast).filter(([, period]) => period > 0).map(([key]) => key).sort(),
    ['query_signal', 'query_strength', 'query_wds_stats', 'query_wds_status']);
  const custom = { ...telemetry, profile: 'custom' };
  assert.equal(queryPeriods(custom).strength, 0);
  assert.equal(queryPeriods({ ...custom, interval: 2, query_bands: .5 }).bands, 2);
  // signal, cell, txrx, stats and status every sample; serving, bands, rates and home now and then.
  assert.deepEqual(estimateAcquisition(custom), { average: 165, worst: 280 });
  assert.equal(estimateAcquisition({ ...custom, indications: 'on' }).worst, 184);
});

test('operator and fixed modem selectors preserve strings and validate the connection intent', () => {
  const draft = configuration();
  draft.modem.device = '/dev/cdc-wdm3';
  draft.modem.serial = '0001234';
  for (const plmn of ['', 'auto', '00101', '001001']) { draft.modem.plmn = plmn; assert.deepEqual(validateConfig(draft), {}); }
  assert.equal(draft.modem.serial, '0001234');
  draft.modem.plmn = '１２３４５';
  draft.modem.apn = ' ';
  draft.modem.device = 'cdc-wdm3';
  const errors = validateConfig(draft);
  assert.ok(errors['modem.plmn']); assert.ok(errors['modem.apn']); assert.ok(errors['modem.device']);
});

test('web binding supports auto and numeric IPv4/IPv6 but rejects hostnames and scoped paths', () => {
  const draft = configuration();
  for (const host of ['auto', '127.0.0.1', '192.168.0.10', '0.0.0.0', '::1', '::', '2001:db8::1']) { draft.web.host = host; assert.deepEqual(validateConfig(draft), {}, host); }
  for (const host of ['localhost', '256.0.0.1', '01.2.3.4', '[::1]', '::1/64', 'fe80::1%wwan0', 'https://192.168.0.1']) { draft.web.host = host; assert.ok(validateConfig(draft)['web.host'], host); }
  assert.equal(isNumericIP('::ffff:192.0.2.1'), true);
});

test('single-line settings reject control separators and an empty output directory', () => {
  const draft = configuration();
  draft.modem.apn = 'internet\nother';
  draft.modem.serial = 'serial\0suffix';
  draft.telemetry.directory = '  ';
  const errors = validateConfig(draft);
  for (const path of ['modem.apn', 'modem.serial', 'telemetry.directory']) assert.ok(errors[path]);
});

test('account usernames and new passwords follow server bounds without trimming or normalizing', () => {
  const draft = configuration();
  draft.web.username = 'operator.name@mocha-1';
  assert.deepEqual(validateConfig(draft, '  password  '), {});
  assert.deepEqual(validateConfig(draft, '😀'.repeat(10)), {});
  assert.ok(validateConfig(draft, '😀'.repeat(5))['web.password']);
  assert.ok(validateConfig(draft, '😀'.repeat(65))['web.password']);
  assert.ok(validateConfig(draft, 'new\u0000password')['web.password']);
  assert.deepEqual(validateConfig(draft, ''), {}, 'a blank password keeps the current one');
  draft.web.username = 'operator name'; assert.ok(validateConfig(draft)['web.username']);
  draft.web.username = 'a'.repeat(65); assert.ok(validateConfig(draft)['web.username']);
});

test('connectivity fields retain typed defaults, private targets and cross-field timeout limits', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.network.connectivity_targets = '10.20.0.1:8443,192.168.1.2:53';
  draft.network.connectivity_check = false;
  assert.deepEqual(validateConfig(draft), {});
  assert.deepEqual(diffConfig(base, draft), { network: { connectivity_check: false, connectivity_targets: '10.20.0.1:8443,192.168.1.2:53' } });
  draft.network.connectivity_interval = 1;
  draft.network.connectivity_timeout = 2;
  assert.ok(validateConfig(draft)['network.connectivity_timeout']);
  for (const [key, value] of [['connectivity_interval', 0.9], ['connectivity_interval', 61], ['connectivity_timeout', .09],
    ['connectivity_timeout', 5.1], ['connectivity_failures', 0], ['connectivity_successes', 11], ['connectivity_successes', 1.5], ['connectivity_check', 'on']] as const) {
    const changed = configuration(); changed.network[key] = value;
    assert.ok(validateConfig(changed)['network.' + key], key + ': ' + value);
  }
  draft.network.connectivity_targets = 'example.com:443';
  assert.ok(validateConfig(draft)['network.connectivity_targets']);
});
