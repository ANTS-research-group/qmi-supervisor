import assert from 'node:assert/strict';
import test from 'node:test';
import { logsQuery, modemFromSearch, pluginFromSearch } from '../lib/logs.ts';

test('the system log query carries only the selected modem', () => {
  assert.equal(logsQuery(200), '/api/logs?lines=200');
  assert.equal(logsQuery(200, ''), '/api/logs?lines=200');
  assert.equal(logsQuery(200, 'auto-quectel-9f173352'),
    '/api/logs?lines=200&modem=auto-quectel-9f173352');
  assert.equal(logsQuery(200, 'a b'), '/api/logs?lines=200&modem=a%20b');
  assert.equal(logsQuery(0), '/api/logs?lines=1');
  assert.equal(logsQuery(900), '/api/logs?lines=500');
});

test('the page URL selects one modem when it names a valid parameter', () => {
  assert.equal(modemFromSearch('?section=log&modem=default'), 'default');
  assert.equal(modemFromSearch('?section=log'), '');
  assert.equal(modemFromSearch(''), '');
});

test('a plugin filter replaces the modem filter and only names valid plugins', () => {
  assert.equal(logsQuery(200, 'default', 'webhook'), '/api/logs?lines=200&plugin=webhook');
  assert.equal(logsQuery(200, 'default', ''), '/api/logs?lines=200&modem=default');
  assert.equal(pluginFromSearch('?section=log&plugin=webhook'), 'webhook');
  assert.equal(pluginFromSearch('?section=log&plugin=Bad_Name'), '');
  assert.equal(pluginFromSearch('?section=log'), '');
});
