import assert from 'node:assert/strict';
import test from 'node:test';
import { countChanges, discardScopedDraft, fieldAvailability, inScope, mergeSavedConfig, sanitizeConfig,
  scopedChanges, scopedDraft, scopedErrors, validateConfig } from '../lib/config.ts';
import type { Config } from '../lib/types';

function configuration(): Config {
  return {
    modem: { device: 'auto', serial: '', apn: 'internet', plmn: '00101', mode: 'connect' },
    network: { method: 'qmi', route_metric: 700, default_route: true, dns: 'auto', route_policy: 'fixed', connectivity_check: true,
      connectivity_targets: '1.1.1.1:443,8.8.8.8:53', connectivity_interval: 3, connectivity_timeout: 1, connectivity_failures: 2, connectivity_successes: 2 },
    recovery: { retry_initial: 2, retry_max: 60, registration_timeout: 180, operation_timeout: 20, poll_interval: 10 },
    telemetry: { enabled: true, interval: 1, directory: 'files', csv: true, json: true, udp_host: '', udp_port: 5006,
      collector_host: '', collector_port: 5007, throughput: false, gps: false, gps_source: 'gpsd', queue_size: 64, flush_interval: 1,
      gps_max_age: 10, request_timeout: 1, collection_budget: .8, profile: 'full' },
    web: { enabled: true, host: '127.0.0.1', port: 8080, history_size: 120, username: 'admin', session_timeout: 28800 },
  };
}

test('stopped connection settings remain editable before first activation', () => {
  const draft = configuration(); draft.modem.mode = 'monitor';
  for (const key of ['apn', 'plmn', 'device', 'serial']) assert.equal(fieldAvailability(draft, 'modem', key).disabled, false);
  for (const key of Object.keys(draft.network)) assert.equal(fieldAvailability(draft, 'network', key).disabled, false);
  assert.equal(fieldAvailability(draft, 'recovery', 'registration_timeout').disabled, false);
});

test('no default route disables route and DNS ownership and checks while local IPv4 configuration stays editable', () => {
  const draft = configuration(); draft.network.default_route = false;
  for (const key of ['route_metric', 'route_policy', 'dns', 'connectivity_check', 'connectivity_targets',
    'connectivity_interval', 'connectivity_timeout', 'connectivity_failures', 'connectivity_successes']) {
    assert.equal(fieldAvailability(draft, 'network', key).disabled, true, key);
  }
  for (const key of ['default_route', 'method']) assert.equal(fieldAvailability(draft, 'network', key).disabled, false, key);
  draft.network.default_route = true; draft.network.connectivity_check = false;
  for (const key of ['connectivity_targets', 'connectivity_interval', 'connectivity_timeout', 'connectivity_failures', 'connectivity_successes']) {
    assert.equal(fieldAvailability(draft, 'network', key).disabled, true, key);
  }
  for (const key of ['connectivity_check', 'route_policy', 'route_metric', 'dns']) assert.equal(fieldAvailability(draft, 'network', key).disabled, false, key);
});

test('disabling telemetry leaves its master control editable and preserves every dependent value', () => {
  const draft = configuration(); draft.telemetry.enabled = false;
  const original = structuredClone(draft);
  for (const key of Object.keys(draft.telemetry)) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, ['gps_source', 'gps_max_age', 'udp_port', 'collector_port'].includes(key), key);
  assert.deepEqual(draft, original);
});

test('optional GPS and destinations enable only their real dependent controls', () => {
  const draft = configuration();
  for (const key of ['gps_source', 'gps_max_age']) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, true);
  for (const key of ['udp_port', 'collector_port']) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, true);
  for (const key of ['gps', 'udp_host', 'collector_host', 'throughput']) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, false);
  draft.telemetry.gps = true; draft.telemetry.udp_host = '192.0.2.1'; draft.telemetry.collector_host = '192.0.2.2';
  for (const key of ['gps_source', 'gps_max_age', 'udp_port', 'collector_port']) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, false);
  draft.telemetry.collector_host = '   ';
  assert.equal(fieldAvailability(draft, 'telemetry', 'collector_port').disabled, true);
});

test('disk controls require CSV or JSON, independently of UDP and live dashboard acquisition', () => {
  const draft = configuration(); draft.telemetry.csv = false; draft.telemetry.json = false; draft.telemetry.udp_host = '192.0.2.1';
  for (const key of ['directory', 'flush_interval', 'queue_size']) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, true, key);
  for (const key of ['interval', 'request_timeout', 'collection_budget', 'profile', 'csv', 'json', 'udp_port']) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, false, key);
  draft.telemetry.json = true;
  for (const key of ['directory', 'flush_interval', 'queue_size']) assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, false, key);
});

test('web can be enabled later with editable account credentials even while listeners are off', () => {
  const draft = configuration(); draft.web.enabled = false;
  for (const key of ['host', 'port', 'history_size']) assert.equal(fieldAvailability(draft, 'web', key).disabled, true, key);
  for (const key of ['enabled', 'username', 'password', 'session_timeout']) assert.equal(fieldAvailability(draft, 'web', key).disabled, false, key);
  assert.equal(fieldAvailability(null, 'web', 'port').disabled, true);
});

test('inactive errors remain validated because the backend validates the complete stored configuration', () => {
  const draft = configuration(); draft.telemetry.enabled = false;
  draft.telemetry.interval = NaN; draft.telemetry.directory = ''; draft.telemetry.udp_port = 0;
  const errors = validateConfig(draft);
  for (const key of ['interval', 'directory', 'udp_port']) {
    assert.equal(fieldAvailability(draft, 'telemetry', key).disabled, key === 'udp_port');
    assert.ok(errors['telemetry.' + key]);
  }
});

test('a profile save covers modem, network, recovery and telemetry while web access and the account save apart', () => {
  for (const section of ['modem', 'network', 'recovery', 'telemetry']) assert.equal(inScope('profile', section, 'any'), true);
  for (const key of ['enabled', 'host', 'port', 'history_size']) assert.equal(inScope('web', 'web', key), true);
  for (const key of ['username', 'password', 'session_timeout']) assert.equal(inScope('account', 'web', key), true);
  assert.equal(inScope('web', 'web', 'username'), false);
  assert.equal(inScope('account', 'web', 'host'), false);
  assert.equal(inScope('profile', 'web', 'port'), false);
});

test('scoped payloads contain only the selected scope and only the account save can carry the transient password', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.modem.apn = 'private.example'; draft.network.route_policy = 'primary'; draft.recovery.retry_max = 90;
  draft.telemetry.gps = true; draft.web.username = 'operator'; draft.web.port = 8081;
  assert.deepEqual(scopedChanges(base, draft, 'a pending password', 'profile'), {
    modem: { apn: 'private.example' }, network: { route_policy: 'primary' }, recovery: { retry_max: 90 }, telemetry: { gps: true },
  });
  assert.deepEqual(scopedChanges(base, draft, 'a pending password', 'web'), { web: { port: 8081 } });
  assert.deepEqual(scopedChanges(base, draft, 'a pending password', 'account'), { web: { username: 'operator', password: 'a pending password' } });
  assert.deepEqual(scopedChanges(null, draft, 'a pending password', 'web'), {});
  assert.deepEqual(scopedChanges(base, null, 'a pending password'), {});
});

test('a shared web edit never counts as a pending change of a modem profile or of the account', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.web.port = 8081;
  assert.equal(countChanges(scopedChanges(base, draft, '', 'profile')), 0);
  assert.equal(countChanges(scopedChanges(base, draft, '', 'account')), 0);
  assert.equal(countChanges(scopedChanges(base, draft, '', 'web')), 1);
  assert.equal(countChanges(scopedChanges(base, draft, 'new password', 'account')), 1);
  assert.equal(countChanges(scopedChanges(base, draft, 'new password', 'web')), 1);
});

test('web errors cannot block a valid profile request, but any profile error does', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.modem.apn = 'private.example'; draft.web.port = 0;
  const errors = validateConfig(draft, 'short');
  assert.ok(errors['web.port']); assert.ok(errors['web.password']);
  assert.deepEqual(scopedErrors(errors, 'profile'), {});
  assert.deepEqual(validateConfig(scopedDraft(base, draft, 'profile')), {});
  assert.equal(scopedDraft(base, draft, 'profile')?.web.port, 8080);
  assert.ok(scopedErrors(errors, 'account')['web.password']);
  assert.ok(scopedErrors(errors, 'web')['web.port']);
  assert.deepEqual(scopedErrors(errors, 'web')['web.password'], undefined, 'a pending password belongs to the account page');
  assert.deepEqual(scopedErrors(errors), errors);
  draft.telemetry.interval = NaN;
  assert.ok(scopedErrors(validateConfig(draft), 'profile')['telemetry.interval'], 'telemetry belongs to the modem save');
});

test('an inactive invalid value in the selected scope still blocks that actual candidate', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.modem.mode = 'monitor'; draft.network.route_metric = NaN;
  const candidate = scopedDraft(base, draft, 'profile');
  assert.equal(fieldAvailability(draft, 'network', 'route_metric').disabled, false);
  assert.ok(validateConfig(candidate)['network.route_metric']);
  assert.ok(Object.hasOwn(scopedChanges(base, draft, '', 'profile').network, 'route_metric'));
});

test('discarding a profile preserves the shared web draft and global discard remains available', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.modem.apn = 'private.example'; draft.network.route_policy = 'primary'; draft.recovery.retry_max = 90;
  draft.telemetry.gps = true; draft.web.username = 'operator';
  const original = structuredClone(draft), reverted = discardScopedDraft(base, draft, 'profile')!;
  for (const section of ['modem', 'network', 'recovery', 'telemetry']) assert.deepEqual(reverted[section], base[section]);
  assert.deepEqual(reverted.web, draft.web);
  assert.deepEqual(draft, original);
  assert.deepEqual(discardScopedDraft(base, draft, 'web')?.modem, draft.modem);
  assert.equal(discardScopedDraft(base, draft, 'web')?.web.username, 'operator', 'discarding web access keeps the account draft');
  assert.equal(discardScopedDraft(base, draft, 'account')?.web.username, 'admin');
  assert.deepEqual(discardScopedDraft(base, draft, 'account')?.modem, draft.modem, 'discarding the account keeps the modem draft');
  assert.deepEqual(discardScopedDraft(base, draft), base);
  assert.equal(discardScopedDraft(null, draft, 'web'), null);
});

test('a successful scoped save rebases untouched fields from the server and retains other pending edits', () => {
  const base = configuration(), draft = structuredClone(base);
  draft.modem.apn = 'private.example'; draft.web.username = 'operator';
  const submitted = scopedChanges(base, draft, 'pending password', 'profile');
  const saved = structuredClone(base); saved.modem.apn = 'normalized.private.example'; saved.web.host = 'auto';
  const result = mergeSavedConfig(base, draft, saved, submitted);
  assert.equal(result.modem.apn, saved.modem.apn, 'An unchanged submitted value follows server normalization');
  assert.equal(result.web.username, 'operator');
  assert.equal(result.web.host, 'auto', 'A field with no pending edit follows the new server baseline');
  assert.equal(base.modem.apn, 'internet');
  assert.equal(draft.modem.apn, 'private.example');
});

test('edits during the request survive its response, including a return to the old baseline', () => {
  const base = configuration(), submittedDraft = structuredClone(base);
  submittedDraft.modem.apn = 'submitted.example';
  const submitted = scopedChanges(base, submittedDraft, '', 'profile'), saved = structuredClone(submittedDraft);
  const current = structuredClone(submittedDraft); current.modem.apn = 'newer.example'; current.telemetry.gps = true;
  assert.equal(mergeSavedConfig(base, current, saved, submitted).modem.apn, 'newer.example');
  current.modem.apn = base.modem.apn;
  const result = mergeSavedConfig(base, current, saved, submitted);
  assert.equal(result.modem.apn, 'internet');
  assert.equal(result.telemetry.gps, true);
});

test('merge never writes transient passwords or private credential fields into the reusable draft', () => {
  const base = configuration(), draft = structuredClone(base), saved = structuredClone(base);
  Object.assign(saved.web, { password_hash: 'private hash', password: 'private password' });
  Object.assign(draft.web, { password: 'transient password', password_confirmation: 'transient password' });
  const result = mergeSavedConfig(base, draft, saved, { web: { password: 'transient password' } });
  assert.deepEqual(result, base);
  assert.deepEqual(scopedDraft(base, draft, 'web'), base);
  assert.deepEqual(sanitizeConfig(result), base);
  assert.equal(JSON.stringify(result).includes('password'), false);
});
