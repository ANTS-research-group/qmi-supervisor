import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import test from 'node:test';
import { createDevProxy, validateApiUrl } from '../tools/dev.mjs';

function trackedServer(handler) {
  const server = http.createServer(handler);
  const sockets = new Set();
  server.on('connection', socket => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });
  return {
    server,
    async stop() {
      for (const socket of sockets) socket.destroy();
      await new Promise(resolve => server.close(() => resolve()));
    },
  };
}

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server.address().port));
  });
}

async function fixture(t) {
  const observed = { api: [], next: [], upgrades: [] };
  const cookies = ['qmi_session=fixture-only; HttpOnly; SameSite=Strict; Path=/', 'fixture_extra=1; Path=/'];
  const api = trackedServer((request, response) => {
    observed.api.push(request.url);
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      response.setHeader('Set-Cookie', cookies);
      response.end(JSON.stringify({ method: request.method, url: request.url, body, headers: request.headers }));
    });
  });
  const next = trackedServer((request, response) => {
    observed.next.push(request.url);
    response.end(JSON.stringify({ source: 'next-fixture', url: request.url, headers: request.headers }));
  });
  next.server.on('upgrade', (request, socket) => {
    observed.upgrades.push(request.headers);
    socket.on('error', () => {});
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n');
  });
  let proxy;
  t.after(async () => {
    proxy?.shutdown();
    await Promise.all([api.stop(), next.stop()]);
  });
  const apiPort = await listen(api.server), nextPort = await listen(next.server);
  proxy = createDevProxy({ apiUrl: `http://127.0.0.1:${apiPort}`, nextUrl: `http://127.0.0.1:${nextPort}` });
  const port = await listen(proxy);
  return { port, origin: `http://127.0.0.1:${port}`, apiPort, nextPort, api, observed, cookies };
}

function request(port, route, { body, ...options } = {}) {
  return new Promise((resolve, reject) => {
    const outgoing = http.request({ hostname: '127.0.0.1', port, path: route, agent: false, ...options }, response => {
      let content = '';
      response.setEncoding('utf8');
      response.on('data', chunk => { content += chunk; });
      response.on('error', reject);
      response.on('end', () => resolve({ status: response.statusCode, headers: response.headers, body: content }));
    });
    outgoing.setTimeout(2000, () => outgoing.destroy(new Error('Fixture request timed out.')));
    outgoing.on('error', reject);
    outgoing.end(body);
  });
}

test('development API URL permits only credential-free HTTP loopback origins', () => {
  assert.equal(validateApiUrl().origin, 'http://127.0.0.1:8080');
  assert.equal(validateApiUrl('http://[::1]:8081').origin, 'http://[::1]:8081');
  assert.equal(validateApiUrl('http://127.0.0.2:8081').origin, 'http://127.0.0.2:8081');
  for (const url of [
    'not a URL', 'https://127.0.0.1:8080', 'http://user:password@127.0.0.1:8080',
    'http://192.168.0.1:8080', 'http://0.0.0.0:8080', 'http://localhost:8080',
    'http://127.0.0.1:8080/path', 'http://127.0.0.1:8080/?x=1', 'http://127.0.0.1:8080/#fragment',
  ]) assert.throws(() => validateApiUrl(url));
});

test('API proxy forwards cookies and CSRF while translating validated Host and Origin', { timeout: 5000 }, async t => {
  const { port, origin, apiPort, cookies, observed } = await fixture(t);
  const response = await request(port, '/api/login?fixture=1', {
    method: 'POST', body: '{"fixture":true}',
    headers: { Origin: origin, Cookie: 'qmi_session=fixture-only', 'X-CSRF-Token': 'fixture-csrf',
      'Content-Type': 'application/json', Authorization: 'Bearer fixture-compat', 'X-Forwarded-Host': 'untrusted.example' },
  });
  assert.equal(response.status, 200);
  const forwarded = JSON.parse(response.body);
  assert.equal(forwarded.method, 'POST');
  assert.equal(forwarded.url, '/api/login?fixture=1');
  assert.equal(forwarded.body, '{"fixture":true}');
  assert.equal(forwarded.headers.host, `127.0.0.1:${apiPort}`);
  assert.equal(forwarded.headers.origin, `http://127.0.0.1:${apiPort}`);
  assert.equal(forwarded.headers.cookie, 'qmi_session=fixture-only');
  assert.equal(forwarded.headers['x-csrf-token'], 'fixture-csrf');
  assert.equal(forwarded.headers.authorization, 'Bearer fixture-compat');
  assert.equal(forwarded.headers['x-forwarded-host'], undefined);
  assert.deepEqual(response.headers['set-cookie'], cookies);
  assert.equal(response.headers['access-control-allow-origin'], undefined);
  assert.deepEqual(observed.next, []);
});

test('non-API paths reach the Next fixture and normal same-origin GET needs no Origin header', { timeout: 5000 }, async t => {
  const { port, nextPort, observed } = await fixture(t);
  const response = await request(port, '/dashboard/?fixture=1');
  assert.equal(response.status, 200);
  const forwarded = JSON.parse(response.body);
  assert.equal(forwarded.source, 'next-fixture');
  assert.equal(forwarded.url, '/dashboard/?fixture=1');
  assert.equal(forwarded.headers.host, `127.0.0.1:${nextPort}`);
  assert.equal(forwarded.headers.origin, undefined);
  assert.deepEqual(observed.api, []);
});

test('foreign Host, foreign Origin, mismatched loopback Origin and duplicate Host are rejected before forwarding', { timeout: 5000 }, async t => {
  const { port, observed } = await fixture(t);
  for (const headers of [
    { Host: `evil.example:${port}` }, { Origin: 'http://evil.example' },
    { Origin: `http://localhost:${port}` }, { Origin: 'null' },
  ]) assert.equal((await request(port, '/api/config', { method: 'POST', headers })).status, 403);
  const raw = await new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1', () => {
      socket.end(`GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nHost: evil.example\r\nConnection: close\r\n\r\n`);
    });
    let content = '';
    socket.setTimeout(2000, () => socket.destroy(new Error('Fixture socket timed out.')));
    socket.on('data', chunk => { content += chunk; });
    socket.on('end', () => resolve(content));
    socket.on('error', reject);
  });
  assert.match(raw, /^HTTP\/1\.1 (400|403)/);
  assert.deepEqual(observed.api, []);
  assert.deepEqual(observed.next, []);
});

test('Next HMR upgrade works with translated Origin while API websocket upgrades are refused', { timeout: 5000 }, async t => {
  const { port, origin, nextPort, observed } = await fixture(t);
  await new Promise((resolve, reject) => {
    const outgoing = http.request({ hostname: '127.0.0.1', port, path: '/_next/webpack-hmr', agent: false,
      headers: { Origin: origin, Upgrade: 'websocket', Connection: 'Upgrade' } });
    outgoing.setTimeout(2000, () => outgoing.destroy(new Error('Fixture upgrade timed out.')));
    outgoing.on('upgrade', (_response, socket) => { socket.destroy(); resolve(); });
    outgoing.on('response', response => { response.resume(); reject(new Error('HMR upgrade was rejected.')); });
    outgoing.on('error', reject);
    outgoing.end();
  });
  assert.equal(observed.upgrades.length, 1);
  assert.equal(observed.upgrades[0].host, `127.0.0.1:${nextPort}`);
  assert.equal(observed.upgrades[0].origin, `http://127.0.0.1:${nextPort}`);
  assert.equal((await request(port, '/api/status', { headers: { Origin: origin, Upgrade: 'websocket', Connection: 'Upgrade' } })).status, 400);
  assert.equal((await request(port, '/_next/webpack-hmr', { headers: { Origin: 'http://evil.example', Upgrade: 'websocket', Connection: 'Upgrade' } })).status, 403);
  assert.deepEqual(observed.api, []);
  assert.equal(observed.upgrades.length, 1);
});

test('an unavailable API returns 502 without starting a service or leaking upstream errors', { timeout: 5000 }, async t => {
  const { port, api, observed } = await fixture(t);
  await api.stop();
  const response = await request(port, '/api/status');
  assert.equal(response.status, 502);
  assert.match(JSON.parse(response.body).error, /Upstream unavailable/);
  assert.equal(response.body.includes('ECONNREFUSED'), false);
  assert.deepEqual(observed.api, []);
  assert.deepEqual(observed.next, []);
});
