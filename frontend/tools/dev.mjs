#!/usr/bin/env node
import http from 'node:http';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { access } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDirectory = fileURLToPath(new URL('../', import.meta.url));
const hopHeaders = new Set(['connection', 'proxy-connection', 'keep-alive', 'te', 'trailer', 'transfer-encoding', 'upgrade']);

export function validateApiUrl(value = 'http://127.0.0.1:8080') {
  let url;
  try { url = new URL(value); } catch { throw new Error('QMI_API_URL must be a loopback HTTP URL.'); }
  const host = url.hostname.replace(/^\[|\]$/g, '');
  if (url.protocol !== 'http:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('QMI_API_URL must use HTTP without credentials, paths, queries or fragments.');
  }
  if (!(net.isIP(host) === 4 && host.startsWith('127.')) && host !== '::1') {
    throw new Error('QMI_API_URL must use a loopback IP (127.0.0.1 or ::1). Use a local tunnel for another machine.');
  }
  return url;
}

function portNumber(value) {
  if (!/^[0-9]+$/.test(String(value)) || Number(value) < 1 || Number(value) > 65535) throw new Error('PORT must be an integer between 1 and 65535.');
  return Number(value);
}

function cleanHeaders(headers) {
  const excluded = new Set(hopHeaders);
  for (const name of String(headers.connection || '').split(',')) excluded.add(name.trim().toLowerCase());
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !excluded.has(name.toLowerCase())));
}

function sendError(response, status, message) {
  if (response.headersSent) { response.destroy(); return; }
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify({ error: message }));
}

function headerCount(request, name) {
  let count = 0;
  for (let index = 0; index < request.rawHeaders.length; index += 2) if (request.rawHeaders[index].toLowerCase() === name) count++;
  return count;
}

function allowedRequest(request, port) {
  const authority = String(request.headers.host || '').toLowerCase();
  const suffix = port === 80 ? '' : `:${port}`;
  if (headerCount(request, 'host') !== 1 || ![`127.0.0.1${suffix}`, `localhost${suffix}`].includes(authority)) {
    return [403, 'Host is not allowed. Open the local development server address.'];
  }
  if (headerCount(request, 'origin') > 1 || (request.headers.origin !== undefined && request.headers.origin !== `http://${authority}`)) {
    return [403, 'Origin is not allowed.'];
  }
  if (!request.url?.startsWith('/') || request.url.startsWith('//')) return [400, 'Invalid path.'];
  return null;
}

function targetHeaders(request, target) {
  const headers = cleanHeaders(request.headers);
  for (const key of Object.keys(headers)) if (key === 'forwarded' || key.startsWith('x-forwarded-')) delete headers[key];
  headers.host = target.host;
  if (request.headers.origin !== undefined) headers.origin = target.origin;
  return headers;
}

/** Only a development bridge: the production backend retains its own guards. */
export function createDevProxy({ apiUrl, nextUrl }) {
  const api = validateApiUrl(apiUrl);
  const next = validateApiUrl(nextUrl);
  const sockets = new Set();
  const server = http.createServer({ maxHeaderSize: 16384, headersTimeout: 15000, requestTimeout: 60000 }, (request, response) => {
    const rejected = allowedRequest(request, server.address().port);
    if (rejected) { sendError(response, ...rejected); return; }
    const pathname = request.url.split('?', 1)[0];
    const target = pathname === '/api' || pathname.startsWith('/api/') ? api : next;
    const upstream = http.request(target, { method: request.method, path: request.url, headers: targetHeaders(request, target) }, incoming => {
      response.writeHead(incoming.statusCode || 502, cleanHeaders(incoming.headers));
      incoming.on('error', () => response.destroy());
      incoming.pipe(response);
    });
    upstream.setTimeout(60000, () => upstream.destroy(new Error('timeout')));
    upstream.on('error', () => sendError(response, 502, 'Upstream unavailable. Check Next and the supervisor API.'));
    request.on('aborted', () => upstream.destroy());
    response.on('close', () => { if (!response.writableEnded) upstream.destroy(); });
    request.pipe(upstream);
  });
  server.maxHeadersCount = 64;
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  server.on('clientError', (_error, socket) => socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'));
  server.on('upgrade', (request, socket, head) => {
    const rejected = allowedRequest(request, server.address().port);
    if (rejected) { socket.end(`HTTP/1.1 ${rejected[0]} Forbidden\r\nConnection: close\r\n\r\n`); return; }
    const pathname = request.url.split('?', 1)[0];
    if (pathname === '/api' || pathname.startsWith('/api/') || String(request.headers.upgrade).toLowerCase() !== 'websocket') {
      socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'); return;
    }
    const upstream = http.request(next, { method: request.method, path: request.url,
      headers: { ...targetHeaders(request, next), connection: 'Upgrade', upgrade: 'websocket' } });
    upstream.setTimeout(15000, () => upstream.destroy());
    upstream.on('upgrade', (incoming, remote, remoteHead) => {
      remote.setTimeout(0);
      const responseHeaders = incoming.rawHeaders.reduce((lines, value, index, values) => index % 2 ? lines : lines + `${value}: ${values[index + 1]}\r\n`, '');
      socket.write(`HTTP/1.1 ${incoming.statusCode} ${incoming.statusMessage}\r\n${responseHeaders}\r\n`);
      if (head.length) remote.write(head);
      if (remoteHead.length) socket.write(remoteHead);
      remote.on('error', () => socket.destroy());
      socket.on('error', () => remote.destroy());
      remote.on('close', () => socket.destroy());
      socket.on('close', () => remote.destroy());
      socket.pipe(remote).pipe(socket);
    });
    upstream.on('response', incoming => { incoming.resume(); socket.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n'); });
    upstream.on('error', () => socket.destroy());
    socket.on('error', () => upstream.destroy());
    socket.on('close', () => upstream.destroy());
    upstream.end();
  });
  server.shutdown = () => {
    server.close();
    for (const socket of sockets) socket.destroy();
  };
  return server;
}

async function availablePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const port = server.address().port;
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  return port;
}

async function main() {
  const port = portNumber(process.env.PORT || '3000');
  const api = validateApiUrl(process.env.QMI_API_URL || 'http://127.0.0.1:8080');
  const nextBinary = path.join(frontendDirectory, 'node_modules', 'next', 'dist', 'bin', 'next');
  try { await access(nextBinary); } catch { throw new Error('The frontend dependencies are missing. Run npm ci inside frontend/.'); }
  let internalPort = await availablePort();
  while (internalPort === port) internalPort = await availablePort();
  const proxy = createDevProxy({ apiUrl: api.href, nextUrl: `http://127.0.0.1:${internalPort}` });
  await new Promise((resolve, reject) => { proxy.once('error', reject); proxy.listen(port, '127.0.0.1', resolve); });
  const child = spawn(process.execPath, [nextBinary, 'dev', '--hostname', '127.0.0.1', '--port', String(internalPort)], {
    cwd: frontendDirectory, stdio: 'inherit', detached: process.platform !== 'win32', env: { ...process.env, PORT: String(internalPort) },
  });
  let stopping = false;
  const killChild = signal => {
    if (!child.pid) return;
    try { if (process.platform === 'win32') child.kill(signal); else process.kill(-child.pid, signal); } catch { /* Already stopped. */ }
  };
  const shutdown = (code = 0) => {
    if (stopping) return;
    stopping = true; process.exitCode = code; proxy.shutdown(); killChild('SIGTERM');
    const timer = setTimeout(() => killChild('SIGKILL'), 3000);
    timer.unref(); child.once('close', () => clearTimeout(timer));
  };
  proxy.on('error', () => { console.error('The development proxy became unavailable.'); shutdown(1); });
  child.once('error', () => { console.error('Could not start Next.'); shutdown(1); });
  child.once('exit', (code, signal) => shutdown(code ?? (signal ? 1 : 0)));
  process.once('SIGINT', () => shutdown());
  process.once('SIGTERM', () => shutdown());
  process.once('exit', () => killChild('SIGTERM'));
  console.log(`\nQMI Supervisor · development: http://127.0.0.1:${port}`);
  console.log(`Existing API: ${api.origin} (the proxy never starts the supervisor)\n`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
