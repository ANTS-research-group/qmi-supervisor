import assert from 'node:assert/strict';
import test from 'node:test';
import { exitOrder, fleetEntries, fleetSummary, modemHref, modemName, modemSerialLine, modemSerialTitle, modemTab, pageKind, pruneObjectiveOverrides,
  showsRouteRole, statePresentation, validModemId } from '../lib/fleet.ts';
import type { ModemRecord } from '../lib/types';

const entry = (id: string, values: Partial<ModemRecord> = {}): ModemRecord => ({
  id, detected: true, configured: true, state: 'ONLINE', connection_enabled: true, monitoring_enabled: true, ...values,
});

test('a backend without fleet inventory still lists its default modem, and nothing before the first status', () => {
  assert.deepEqual(fleetEntries([], null, null), []);
  const [modem] = fleetEntries([], { state: 'ONLINE', device: { interface: 'wwan0', product: 'Test modem' }, network: { route_metric: 700 } },
    { modem: { apn: 'internet', mode: 'connect' }, telemetry: { enabled: false } });
  assert.equal(modem.id, 'default'); assert.equal(modem.detected, true); assert.equal(modem.interface, 'wwan0');
  assert.equal(modem.apn, 'internet'); assert.equal(modem.connection_enabled, true); assert.equal(modem.monitoring_enabled, false);
  assert.equal(fleetEntries([], { state: 'WAIT_DEVICE', device: null }, null)[0].detected, false);
  const inventory = [entry('default'), entry('second')];
  assert.deepEqual(fleetEntries(inventory, { state: 'IDLE' }, null), inventory);
});

test('a kept profile whose modem is unplugged is not offered as an available modem', () => {
  const inventory = [entry('default'), entry('auto-quectel', { detected: false, state: 'WAIT_DEVICE', interface: null })];
  assert.deepEqual(fleetEntries(inventory, { state: 'ONLINE' }, null).map(modem => modem.id), ['default'],
    'only the modem whose hardware is present stays in the fleet');
  // A single remaining modem means the single-modem view, whatever its profile id is.
  assert.deepEqual(fleetEntries([entry('default', { detected: false }), entry('auto-quectel')], null, null,
    'auto-quectel').map(modem => modem.id), ['auto-quectel']);
  // With every modem unplugged the selected profile keeps the waiting page, not a fleet of absent modems.
  const waiting = [entry('default', { detected: false }), entry('auto-quectel', { detected: false })];
  assert.deepEqual(fleetEntries(waiting, null, null, 'auto-quectel').map(modem => modem.id), ['auto-quectel']);
  assert.deepEqual(fleetEntries(waiting, null, null, 'missing').map(modem => modem.id), ['default']);
});

test('states share one translated vocabulary and undetected modems never look online', () => {
  assert.deepEqual(statePresentation('ONLINE'), { label: 'Online', tone: 'good' });
  assert.deepEqual(statePresentation('WAIT_REGISTRATION'), { label: 'Waiting for registration', tone: 'warning' });
  assert.deepEqual(statePresentation('ONLINE', false), { label: 'Not detected', tone: 'neutral' });
  assert.deepEqual(statePresentation('SOMETHING_NEW'), { label: 'SOMETHING_NEW', tone: 'neutral' });
  assert.deepEqual(fleetSummary([entry('a'), entry('b', { state: 'IDLE' }), entry('c', { detected: false })]), { online: 1, undetected: 1 });
});

test('exit order follows the effective metrics the kernel uses and skips modems without a default route', () => {
  const order = exitOrder([
    entry('second', { interface: 'wwan1', network: { route_metric: 700, connectivity: { route_mode: 'requested' } } }),
    entry('default', { interface: 'wwan0', network: { route_metric: 100, connectivity: { route_mode: 'requested', preferred: true } } }),
    entry('monitor', { network: { route_metric: 50, connectivity: { route_mode: 'disabled' } } }),
    entry('missing', { detected: false, network: { route_metric: 10 } }),
    entry('unknown', { network: {} }),
  ]);
  assert.deepEqual(order.map(item => [item.id, item.position, item.active]), [['default', 1, true], ['second', 2, false]]);
  assert.equal(showsRouteRole(entry('a', { network: { connectivity: { route_mode: 'fallback' } } })), true);
  assert.equal(showsRouteRole(entry('a', { network: { connectivity: { route_mode: 'disabled' } } })), false);
  assert.equal(showsRouteRole(entry('a', { connection_enabled: false, network: { connectivity: { route_mode: 'requested' } } })), false);
});

test('modem links carry their id so new tabs, reloads and history keep the right modem', () => {
  assert.equal(modemHref('/modem/', 'second'), '/modem/?modem=second');
  assert.equal(modemHref('/configuration/modem/', 'auto-1', { section: 'gps' }), '/configuration/modem/?modem=auto-1&section=gps');
  assert.ok(validModemId('default')); assert.ok(validModemId('auto-0a1b')); assert.ok(!validModemId('Default')); assert.ok(!validModemId('../x'));
  assert.equal(pageKind('/', true), 'fleet'); assert.equal(pageKind('/dashboard/', false), 'modem');
  assert.equal(pageKind('/configuration/web/', true), 'system'); assert.equal(pageKind('/metrics/data/', true), 'modem');
  assert.equal(pageKind('/profile/', true), 'account');
  assert.equal(modemTab('/metrics/data/'), 'diagnostics'); assert.equal(modemTab('/metrics/charts/'), 'metrics');
  assert.equal(modemTab('/configuration/modem/'), 'configuration'); assert.equal(modemTab('/'), 'summary');
});

test('a modem is named after its manufacturer and model, with serial and interface underneath', () => {
  const quectel = entry('auto-02a7c12a9befb606', { device: { manufacturer: 'Quectel', product: 'RM520N-GL', serial: '18d94aeb', interface: 'wwan1' }, interface: 'wwan1' });
  assert.equal(modemName(quectel), 'Quectel RM520N-GL');
  assert.equal(modemSerialLine(quectel), '18d94aeb · wwan1');
  assert.match(modemSerialTitle(quectel), /18d94aeb/);
  const fibocom = entry('default', { device: { manufacturer: 'Fibocom', product: 'Fibocom Modem_SN:857DF105', serial: '857df105', interface: 'wwan0' } });
  assert.equal(modemName(fibocom), 'Fibocom Modem', 'the firmware serial suffix is not part of the model');
  assert.equal(modemName(entry('second', { device: { product: 'Synthetic UE 2' } })), 'Synthetic UE 2');
  assert.equal(modemName(entry('third')), 'Modem', 'an absent device never shows the profile id');
  assert.equal(modemName(entry('auto-quectel-18d94aeb')), 'Quectel', 'a generated profile names its vendor');
  assert.equal(modemName(entry('default')), 'Modem');
  assert.equal(modemSerialLine(entry('third', { device: { serial: 'abc' } })), 'abc');
});

test('a fleet switch keeps its new position until the published revision changes', () => {
  const overrides = { 'second:telemetry': { enabled: false, revision: 'r1' }, 'other:connection': { enabled: true, revision: 'r0' } };
  assert.deepEqual(Object.keys(pruneObjectiveOverrides(overrides, 'r1')), ['second:telemetry']);
  assert.deepEqual(pruneObjectiveOverrides(overrides, 'r2'), {});
});
