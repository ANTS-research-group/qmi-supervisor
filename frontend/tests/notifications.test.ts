import { test } from 'node:test';
import assert from 'node:assert/strict';
import { notificationLifetime } from '../lib/notifications.ts';

test('every notice is temporary, with more time to read errors and warnings', () => {
  assert.equal(notificationLifetime('success'), 4500);
  assert.equal(notificationLifetime('info'), 4500);
  assert.equal(notificationLifetime('warning'), 7000);
  assert.equal(notificationLifetime('error'), 7000);
});
