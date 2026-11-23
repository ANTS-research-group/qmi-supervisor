import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { BUILD_VERSION, accountVersionLabel, publishedVersionMismatch } from '../lib/version.ts';

test('the account line always shows the build version with a leading v', () => {
  assert.equal(BUILD_VERSION, 'dev', 'npm test runs without the package build variable');
  assert.equal(accountVersionLabel(), 'vdev');
  assert.equal(accountVersionLabel('1.2.0+10.g1182bbc-1'), 'v1.2.0+10.g1182bbc-1');
});

test('the Next config forwards the package build version to the frontend', () => {
  const source = fs.readFileSync(new URL('../next.config.ts', import.meta.url), 'utf8');
  assert.match(source, /QMI_SUPERVISOR_VERSION/, 'the package build variable is read');
  assert.match(source, /NEXT_PUBLIC_BUILD_VERSION/, 'the build constant is exposed to the app');
});

test('a reload is offered only when the published version really differs', () => {
  assert.equal(publishedVersionMismatch('1.2.0-1', '1.2.0-1'), null);
  assert.equal(publishedVersionMismatch('1.2.0-1', '1.3.0-1'), '1.3.0-1');
  assert.equal(publishedVersionMismatch('1.2.0-1', 'unknown'), null);
  assert.equal(publishedVersionMismatch('dev', '1.3.0-1'), null);
  assert.equal(publishedVersionMismatch('', '1.3.0-1'), null);
  assert.equal(publishedVersionMismatch('1.2.0-1', undefined), null);
  assert.equal(publishedVersionMismatch(' 1.2.0-1 ', '1.3.0-1'), '1.3.0-1');
});
