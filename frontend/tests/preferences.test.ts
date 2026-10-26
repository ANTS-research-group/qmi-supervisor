import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
import { english, spanish, setLocale, t } from '../lib/messages.ts';
import { number, signalQuality } from '../lib/metrics.ts';
import { configFields, mergeSavedConfig, queryGroups } from '../lib/config.ts';
import { accountSections, profileSections, routeRoles, saveEffects, systemSections } from '../lib/config-layout.ts';
import { modemTabs, statePresentation } from '../lib/fleet.ts';

test('catalogues have identical complete keys and English is the default', () => {
  assert.deepEqual(Object.keys(english), Object.keys(spanish));
  assert.ok(Object.keys(english).length > 350);
  for (const [key, value] of Object.entries(english)) {
    assert.equal(value, key);
    assert.ok(spanish[key as keyof typeof spanish]);
  }
  assert.equal(t('Sign in'), 'Sign in');
});

test('every literal UI translation request exists in the catalogue', () => {
  const missing: string[] = [];
  for (const directory of ['components', 'lib', 'app']) {
    const root = path.resolve(directory);
    for (const relative of fs.readdirSync(root, { recursive: true })) {
      if (typeof relative !== 'string' || !/\.tsx?$/.test(relative) || relative === 'messages.ts') continue;
      const filename = path.join(root, relative);
      const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true);
      const visit = (node: ts.Node) => {
        if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 't'
            && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) {
          const key = node.arguments[0].text;
          if (key && !Object.hasOwn(english, key)) missing.push(directory + '/' + relative + ': ' + key);
        }
        ts.forEachChild(node, visit);
      };
      visit(source);
    }
  }
  assert.deepEqual(missing, []);
});

test('labels chosen at runtime (fields, sections, roles, states and effects) are translated too', () => {
  const missing: string[] = [];
  const check = (value: unknown) => { if (typeof value === 'string' && value && !Object.hasOwn(english, value)) missing.push(value); };
  for (const fields of Object.values(configFields)) for (const definition of Object.values(fields)) {
    check(definition.label); check(definition.hint);
    for (const option of definition.options ?? []) { check(option.label); check(option.description); }
  }
  for (const section of [...profileSections, ...systemSections, ...accountSections]) { check(section.label); for (const row of section.rows) { check(row.label); check(row.hint); } }
  for (const role of routeRoles) { check(role.label); check(role.description); }
  for (const group of queryGroups) { check(group.label); check(group.metrics); }
  for (const tab of modemTabs) check(tab.label);
  for (const state of ['ONLINE', 'MONITOR', 'WAIT_DEVICE', 'WAIT_REGISTRATION', 'CONNECT', 'CONFIGURE_IP', 'BACKOFF', 'IDLE', 'STOPPED']) check(statePresentation(state).label);
  for (const effect of [...saveEffects({ modem: { serial: 'x' } }, null, 'profile'), ...saveEffects({ modem: { apn: 'x' }, telemetry: { interval: 2 } }, { modem: { mode: 'connect' } }, 'profile'),
    ...saveEffects({}, null, 'web'), ...saveEffects({ web: { password: 'x' } }, null, 'account'),
    ...saveEffects({ web: { session_timeout: 60 } }, null, 'account')]) check(effect);
  assert.deepEqual(missing, []);
});

test('language changes format UI values without changing raw metric values', () => {
  try {
    setLocale('es');
    assert.equal(t('Sign in'), 'Iniciar sesión');
    assert.equal(t('Retry in {seconds} s', { seconds: 6 }), 'Reintento en 6 s');
    assert.equal(number(3449.28, 2), '3449,28');
    assert.equal(signalQuality('rsrp', -70)?.name, 'Excelente');
    assert.equal(t('nas_value_5g_signal_strength_rsrp'), 'nas_value_5g_signal_strength_rsrp');
  } finally { setLocale('en'); }
  assert.equal(number(3449.28, 2), '3,449.28');
});

test('quick objective changes rebase untouched flags while preserving unrelated drafts', () => {
  const base = { modem: { mode: 'monitor', apn: 'internet' }, telemetry: { enabled: false, interval: 1 } };
  const draft = structuredClone(base); draft.modem.apn = 'private';
  const saved = structuredClone(base); saved.modem.mode = 'connect';
  const merged = mergeSavedConfig(base, draft, saved, {});
  assert.equal(merged.modem.mode, 'connect');
  assert.equal(merged.modem.apn, 'private');
  assert.equal(merged.telemetry.enabled, false);
});
