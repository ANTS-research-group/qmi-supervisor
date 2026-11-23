import assert from 'node:assert/strict';
import test from 'node:test';
import { english, spanish, setLocale, t } from '../lib/messages.ts';
import { availableUpdate, failureKeys, indicatorTone, reasonText, reconnectAction, releaseAction, stepText,
  updateErrorText, ERROR_TEXT, HISTORY_ACTION_TEXT, HISTORY_RESULT_TEXT, REASON_TEXT, RELATION_TEXT, STEP_STATE_TEXT,
  STEP_TEXT } from '../lib/updates.ts';
import type { UpdateInstall, UpdatePending, UpdateRelease, UpdateResponse } from '../lib/updates.ts';

function release(version: string, relation: UpdateRelease['relation'], extra: Partial<UpdateRelease> = {}): UpdateRelease {
  return { version, tag: 'v' + version, published_at_utc: '2026-11-20T08:00:00Z', prerelease: false,
    notes: 'notes', notes_truncated: false, relation, installable: relation !== 'installed',
    reason: relation === 'installed' ? 'installed' : null, ...extra };
}

function response(over: Partial<UpdateResponse> = {}): UpdateResponse {
  return {
    installed: { version: '1.2.0-1', commit: 'abc1234', built_utc: '2026-11-19T16:47:00Z' },
    settings: { mode: 'notify', channel: 'stable', window_start: '03:00', window_end: '05:00',
      check_interval_hours: 24, require_signature: true, auto_rollback: true, allow_downgrade: true, source: 'https://example.invalid' },
    check: { state: 'ok', checked_at_utc: '2026-11-20T09:00:00Z', error_code: null, error: null, running: false, retry_after: 0 },
    releases: [release('1.2.0-1', 'installed'), release('1.1.0-1', 'older')],
    install: null, history: [], rollback: { available: false, version: null, running: false },
    state_error: null, units_error: null, ...over,
  };
}

const NOW = 1_700_000_000_000;
const BASELINE = '2026-11-20T08:00:00Z';
const started = (over: Partial<UpdatePending> = {}): UpdatePending =>
  ({ action: 'install', target: '1.3.0-1', from: '1.2.0-1', baseline: BASELINE, started_ms: NOW, ...over });
const runningInstall = (over: Partial<UpdateInstall> = {}): UpdateInstall => ({
  action: 'install', state: 'running', version: '1.3.0-1', from_version: '1.2.0-1', step: 'download',
  steps: [], started_at_utc: '2026-11-20T09:00:00Z', finished_at_utc: null, error_code: null, error: null,
  running: true, ...over,
});

test('the newest newer installable release is the one offered', () => {
  const data = response({ releases: [release('1.4.0-1', 'newer'), release('1.3.0-1', 'newer'), release('1.1.0-1', 'older')] });
  assert.equal(availableUpdate(data)?.version, '1.4.0-1');
  assert.equal(availableUpdate(response()), null, 'no newer release');
  assert.equal(availableUpdate(response({ releases: [release('1.3.0-1', 'newer', { installable: false, reason: 'unsigned' })] })), null,
    'a newer release that cannot be installed is not offered');
  assert.equal(availableUpdate(null), null);
});

test('a release action follows the relation, the advanced mode and the downgrade setting', () => {
  assert.deepEqual(releaseAction(release('1.2.0-1', 'installed'), { advanced: true, allowDowngrade: true }),
    { kind: 'installed', reason: null });
  assert.deepEqual(releaseAction(release('1.3.0-1', 'newer'), { advanced: false, allowDowngrade: false }),
    { kind: 'update', reason: null });
  assert.deepEqual(releaseAction(release('1.1.0-1', 'older'), { advanced: true, allowDowngrade: true }),
    { kind: 'downgrade', reason: null });
  assert.deepEqual(releaseAction(release('1.1.0-1', 'older'), { advanced: false, allowDowngrade: true }),
    { kind: 'none', reason: null }, 'a downgrade needs advanced mode');
  assert.deepEqual(releaseAction(release('1.1.0-1', 'older'), { advanced: true, allowDowngrade: false }),
    { kind: 'none', reason: null }, 'a downgrade needs the setting');
  assert.deepEqual(releaseAction(release('1.3.0-1', 'newer', { installable: false, reason: 'unsigned' }),
    { advanced: true, allowDowngrade: true }), { kind: 'none', reason: { id: 'Not signed' } });
  assert.deepEqual(releaseAction(release('1.3.0-1', 'newer', { installable: false, reason: 'prerelease_channel' }),
    { advanced: true, allowDowngrade: true }), { kind: 'none', reason: { id: 'Pre-releases are not enabled' } });
  assert.deepEqual(releaseAction(release('1.3.0-1', 'unknown'), { advanced: true, allowDowngrade: true }),
    { kind: 'none', reason: null }, 'an unknown relation is not offered');
  assert.deepEqual(releaseAction(release('1.3.0-1', 'unknown', { installable: false, reason: 'installed_unknown' }),
    { advanced: true, allowDowngrade: true }),
    { kind: 'none', reason: { id: 'The installed version is unknown' } });
});

test('labels fall back safely for codes this build does not know', () => {
  assert.deepEqual(reasonText('unsigned'), { id: 'Not signed' });
  assert.deepEqual(reasonText('something_new'), { id: 'Unknown reason ({reason})', values: { reason: 'something_new' } });
  assert.equal(updateErrorText('bad_signature'), 'The release signature is not valid.');
  assert.equal(updateErrorText('tls'), 'The secure connection to the release server failed (certificates or clock).');
  assert.equal(updateErrorText('not_a_code'), 'The updater reported an error ({code}).');
  assert.equal(updateErrorText(null), 'The updater reported an error ({code}).');
  assert.equal(stepText('download'), 'Download the package');
  assert.equal(stepText('snapshot'), 'Record the running state');
  assert.equal(stepText('a_new_step'), 'a_new_step');
});

test('every label in the update tables has an English key and a Spanish text', () => {
  const tables = [RELATION_TEXT, REASON_TEXT, STEP_TEXT, STEP_STATE_TEXT, HISTORY_ACTION_TEXT, HISTORY_RESULT_TEXT, ERROR_TEXT];
  for (const table of tables) for (const [code, id] of Object.entries(table)) {
    assert.ok(Object.hasOwn(english, id), code + ': ' + id);
    assert.ok(spanish[id as keyof typeof spanish], code + ': ' + id);
  }
  for (const id of ['Unknown reason ({reason})', 'The updater reported an error ({code}).']) {
    assert.ok(Object.hasOwn(english, id), id);
    assert.ok(spanish[id as keyof typeof spanish], id);
  }
  try {
    setLocale('es');
    assert.equal(t(RELATION_TEXT.newer), 'Más reciente');
    assert.equal(t(STEP_TEXT.health), 'Comprobar la nueva versión');
  } finally { setLocale('en'); }
});

test('failure keys and the indicator tone follow unseen failures and available updates', () => {
  assert.deepEqual(failureKeys(response()), []);
  const failedCheck = response({ check: { ...response().check, state: 'failed', error_code: 'network',
    checked_at_utc: '2026-11-20T10:00:00Z' } });
  assert.deepEqual(failureKeys(failedCheck), ['check:2026-11-20T10:00:00Z']);
  const failedHistory = response({ history: [{ action: 'automatic', version: '1.4.0-1', from_version: '1.3.0-1',
    result: 'failed', started_at_utc: '2026-11-20T09:00:00Z', finished_at_utc: '2026-11-20T09:05:00Z',
    error_code: 'bad_signature', error: 'detail' }] });
  assert.deepEqual(failureKeys(failedHistory), ['history:2026-11-20T09:05:00Z']);

  assert.equal(indicatorTone(response(), []), null, 'up to date');
  assert.equal(indicatorTone(response({ releases: [release('1.3.0-1', 'newer')] }), []), 'info', 'update available');
  assert.equal(indicatorTone(response({ install: runningInstall() }), []), 'info', 'installing');
  assert.equal(indicatorTone(failedCheck, []), 'bad', 'an unseen failure');
  assert.equal(indicatorTone(failedCheck, ['check:2026-11-20T10:00:00Z']), null, 'a seen failure alone');
  assert.equal(indicatorTone(failedHistory, []), 'bad', 'an unseen failed result');
  assert.equal(indicatorTone({ ...failedHistory, releases: [release('1.4.0-1', 'newer')] }, ['history:2026-11-20T09:05:00Z']),
    'info', 'seen failure with an available update');
  assert.equal(indicatorTone(null, []), null);
});

test('reconnect waits for the new version, reloads by itself and reports the end of the request', () => {
  const idle = (over: Partial<Parameters<typeof reconnectAction>[0]> = {}) => reconnectAction({
    pending: null, installed: '1.2.0-1', pageVersion: '1.2.0-1', install: null, unsaved: false, now: NOW, ...over });

  assert.deepEqual(idle(), { kind: 'none' });
  assert.deepEqual(idle({ installed: '1.3.0-1' }), { kind: 'reload-prompt', version: '1.3.0-1' });
  assert.deepEqual(idle({ pageVersion: null }), { kind: 'none' }, 'a dev build has no version to compare');

  assert.deepEqual(idle({ pending: started(), install: null }), { kind: 'wait' });
  assert.deepEqual(idle({ pending: started(), installed: '1.3.0-1' }), { kind: 'reload' });
  assert.deepEqual(idle({ pending: started(), installed: '1.3.0-1', unsaved: true }),
    { kind: 'reload-prompt', version: '1.3.0-1' }, 'never reload with unsaved drafts');
  assert.deepEqual(idle({ pending: started(), install: runningInstall() }), { kind: 'wait' },
    'installed still matches while the transaction runs');

  assert.deepEqual(idle({ pending: started(), install: runningInstall({ state: 'succeeded', running: false }) }),
    { kind: 'succeeded', action: 'install', version: '1.3.0-1' });
  assert.deepEqual(idle({ pending: started({ action: 'rollback', target: '1.2.0-1' }),
    install: runningInstall({ action: 'rollback', version: '1.2.0-1', state: 'succeeded', running: false }) }),
    { kind: 'succeeded', action: 'rollback', version: '1.2.0-1' });

  const rolledBack = idle({ pending: started(), install: runningInstall({ state: 'rolled_back', running: false,
    error_code: 'health_failed', from_version: '1.2.0-1' }) });
  assert.deepEqual(rolledBack, { kind: 'failed', action: 'install', version: '1.3.0-1',
    previous: '1.2.0-1', code: 'health_failed', rolledBack: true });
  assert.deepEqual(idle({ pending: started(), install: runningInstall({ state: 'interrupted', running: false }) }),
    { kind: 'failed', action: 'install', version: '1.3.0-1', previous: '1.2.0-1', code: 'interrupted', rolledBack: false });

  // A result recorded before this request was accepted is ignored until its own timeout.
  const old = runningInstall({ state: 'succeeded', running: false, started_at_utc: BASELINE });
  assert.deepEqual(idle({ pending: started(), install: old }), { kind: 'wait' });
  assert.deepEqual(idle({ pending: started(), install: old, now: NOW + 2 * 60 * 1000 + 1 }),
    { kind: 'failed', action: 'install', version: '1.3.0-1', previous: null, code: 'interrupted', rolledBack: false });
  assert.deepEqual(idle({ pending: started(), install: null, now: NOW + 30 * 60 * 1000 + 1 }), { kind: 'expired' });
});
