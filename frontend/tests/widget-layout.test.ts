import assert from 'node:assert/strict';
import test from 'node:test';
import { english } from '../lib/messages.ts';
import { allowedSizes, boardIds, defaultLayout, parseLayout, placeWidget, resizeWidget, resolveLayout, sameLayout, serializeLayout,
  shiftWidget, summaryWidgets, trayIds, widgetCatalogue, widgetSizeLabels } from '../lib/widget-layout.ts';
import type { WidgetSpec } from '../lib/widget-layout.ts';

const specs: WidgetSpec[] = [
  { id: 'a', label: 'A', icon: 'info', size: 'small' },
  { id: 'b', label: 'B', icon: 'info', size: 'medium' },
  { id: 'c', label: 'C', icon: 'info', size: 'large', sizes: ['medium', 'large'] },
];

test('every card of every view has a translated name and description, and widths it can take', () => {
  const missing: string[] = [];
  for (const spec of Object.values(widgetCatalogue).flat()) {
    for (const text of [spec.label, spec.description]) if (text && !/^[A-Z]{3,4}$/.test(text) && !Object.hasOwn(english, text)) missing.push(text);
    assert.ok(allowedSizes(spec).includes(spec.size), spec.id + ' starts at a width it offers');
  }
  for (const label of Object.values(widgetSizeLabels)) if (!Object.hasOwn(english, label)) missing.push(label);
  assert.deepEqual(missing, []);
  for (const [scope, list] of Object.entries(widgetCatalogue)) {
    assert.equal(new Set(list.map(spec => spec.id)).size, list.length, scope + ' card ids are unique');
  }
});

test('the default arrangement follows the catalogue unless the view reorders or resizes it', () => {
  assert.deepEqual(defaultLayout(specs), { order: ['a', 'b', 'c'], hidden: [], sizes: { a: 'small', b: 'medium', c: 'large' } });
  const variant = defaultLayout(specs, { order: ['c', 'a'], sizes: { c: 'medium', a: 'large', b: 'tiny' as never } });
  assert.deepEqual(variant.order, ['c', 'a', 'b']);
  assert.deepEqual(variant.sizes, { a: 'large', b: 'medium', c: 'medium' });
  assert.equal(defaultLayout(specs, { sizes: { c: 'small' } }).sizes.c, 'large', 'a width the card does not offer is ignored');
  const summary = defaultLayout(summaryWidgets);
  assert.deepEqual(summary.order.slice(0, 3), ['status', 'address', 'sample'], 'the summary opens with its three stat cards');
});

test('a saved arrangement survives new and removed cards', () => {
  const saved = { order: ['c', 'gone', 'a', 'a'], hidden: ['gone'], sizes: { c: 'medium' as const, a: 'large' as const } };
  const layout = resolveLayout(specs, saved);
  assert.deepEqual(layout.order, ['c', 'a', 'b'], 'unknown cards disappear and a new card joins after its default predecessor');
  assert.deepEqual(layout.sizes, { c: 'medium', a: 'large', b: 'medium' });
  const hiddenBefore = resolveLayout(specs, { order: ['c', 'a'], hidden: ['a'], sizes: {} });
  assert.deepEqual(hiddenBefore.order, ['b', 'c', 'a'], 'a hidden predecessor does not anchor the new card');
  assert.deepEqual(hiddenBefore.hidden, ['a']);
  assert.deepEqual(resolveLayout(specs, { order: [], hidden: [], sizes: { c: 'small' } }).sizes.c, 'large');
  assert.deepEqual(resolveLayout(specs, null), defaultLayout(specs));
});

test('cards move between the page and the hidden tray, and one step at a time', () => {
  let layout = defaultLayout(specs);
  layout = placeWidget(layout, 'a', 'tray', 0);
  assert.deepEqual(boardIds(layout), ['b', 'c']);
  assert.deepEqual(trayIds(layout), ['a']);
  layout = placeWidget(layout, 'a', 'board', 1);
  assert.deepEqual(boardIds(layout), ['b', 'a', 'c']);
  assert.deepEqual(layout.hidden, []);
  layout = shiftWidget(layout, 'c', -1);
  assert.deepEqual(boardIds(layout), ['b', 'c', 'a']);
  assert.equal(shiftWidget(layout, 'b', -1), layout, 'the first card cannot move earlier');
  layout = placeWidget(layout, 'b', 'board', 99);
  assert.deepEqual(boardIds(layout), ['c', 'a', 'b'], 'an index past the end appends');
  assert.equal(placeWidget(layout, 'missing', 'board', 0), layout);
});

test('widths change one card and arrangements compare by value', () => {
  const base = defaultLayout(specs);
  const resized = resizeWidget(base, 'a', 'large');
  assert.equal(resized.sizes.a, 'large');
  assert.equal(base.sizes.a, 'small', 'the previous arrangement is not mutated');
  assert.equal(resizeWidget(base, 'a', 'small'), base);
  assert.ok(sameLayout(base, defaultLayout(specs)));
  assert.ok(!sameLayout(base, resized));
});

test('stored arrangements are versioned and validated', () => {
  const layout = placeWidget(resizeWidget(defaultLayout(specs), 'b', 'large'), 'c', 'tray', 0);
  assert.deepEqual(parseLayout(serializeLayout(layout)), layout);
  assert.equal(parseLayout('{"order":["a"],"hidden":[]}'), null, 'the unversioned card menu format is ignored');
  assert.equal(parseLayout('not json'), null);
  assert.deepEqual(parseLayout('{"v":2,"order":["a",3],"hidden":"x","sizes":{"a":"huge","b":"small"}}'), { order: ['a'], hidden: [], sizes: { b: 'small' } });
});
