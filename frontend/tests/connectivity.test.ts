import assert from 'node:assert/strict';
import test from 'node:test';
import { connectivityPresentation, connectivityTargetError, connectivityTargetErrors, parseConnectivityTargets, splitTargets } from '../lib/connectivity.ts';

test('TCP targets accept private numeric addresses and reject unusable or duplicate endpoints', () => {
  assert.deepEqual(parseConnectivityTargets('1.1.1.1:443, 10.0.0.1:53'), [['1.1.1.1', 443], ['10.0.0.1', 53]]);
  assert.deepEqual(parseConnectivityTargets('192.168.1.255:65535'), [['192.168.1.255', 65535]]);
  for (const value of ['', null, 1, 'example.com:443', '[::1]:443', '1.1.1.1', '01.1.1.1:443', '256.1.1.1:443',
    '0.0.0.0:443', '0.1.2.3:443', '127.1.2.3:443', '224.0.0.1:443', '239.1.1.1:443', '255.255.255.255:443',
    '1.1.1.1:0', '1.1.1.1:65536', '1.1.1.1:443,1.1.1.1:0443', '1.1.1.1:443,', '1.1.1.1:443\n',
    '1.1.1.1:1,1.1.1.1:2,1.1.1.1:3,1.1.1.1:4,1.1.1.1:5']) {
    assert.equal(parseConnectivityTargets(value), null, String(value));
  }
});

test('each listed target explains why it is rejected, so chips can mark only the bad one', () => {
  assert.deepEqual(splitTargets(' 1.1.1.1:443 ,, 8.8.8.8:53 '), ['1.1.1.1:443', '8.8.8.8:53']);
  assert.deepEqual(splitTargets(undefined), []);
  assert.equal(connectivityTargetError('10.0.0.1:443'), null);
  for (const [target, reason] of [['example.com:443', 'numeric'], ['256.1.1.1:443', 'IPv4'], ['127.0.0.1:80', 'Loopback'],
    ['0.1.2.3:80', '0.0.0.0/8'], ['224.0.0.1:80', 'Multicast'], ['255.255.255.255:80', 'Broadcast'], ['1.1.1.1:0', 'port']] as const) {
    assert.match(connectivityTargetError(target) ?? '', new RegExp(reason.replace(/[./]/g, '\\$&')), target);
  }
  assert.deepEqual(connectivityTargetErrors(['1.1.1.1:443', '127.0.0.1:1', '1.1.1.1:0443']), [null, 'Loopback is not allowed.', 'Duplicate target.']);
});

test('the compact presentation requires a fresh positive check and retains fallback or blocked routes', () => {
  assert.equal(connectivityPresentation(null).verified, false);
  assert.equal(connectivityPresentation({ state: 'checking', route_mode: 'fallback' }).summary, 'Checking connectivity · Backup');
  assert.equal(connectivityPresentation({ state: 'down', route_mode: 'fallback' }).summary, 'Verification failed · Backup');
  assert.equal(connectivityPresentation({ state: 'up', route_mode: 'requested', preferred: true }).summary, 'Connectivity verified · Primary');
  assert.equal(connectivityPresentation({ state: 'up', route_mode: 'requested', preferred: false }).summary, 'Connectivity verified · Not primary');
  assert.equal(connectivityPresentation({ state: 'up', stale: true, route_mode: 'fallback' }).verified, false);
  assert.equal(connectivityPresentation({ state: 'up', route_mode: 'blocked' }).summary, 'Connectivity verified · Route blocked');
  assert.equal(connectivityPresentation({ state: 'up', route_mode: 'fallback', dns_error: 'DNS installation failed' }).summary,
    'DNS error · Backup');
  assert.equal(connectivityPresentation({ state: 'disabled', enabled: false, route_mode: 'requested', preferred: true }).summary,
    'Verification disabled · Primary');
  assert.equal(connectivityPresentation({ state: 'disabled', enabled: true, route_mode: 'disabled' }).summary,
    'No active verification · No default route');
});
