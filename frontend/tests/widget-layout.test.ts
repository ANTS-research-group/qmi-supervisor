import assert from 'node:assert/strict';
import test from 'node:test';
import { english } from '../lib/messages.ts';
import { TRAY, addRow, boardIds, compactLayout, defaultLayout, defaultRows, fitColumns, moveRow, parseLayout, placeWidget, removeRow, resolveLayout,
  rowMin, sameLayout, serializeLayout, setRowColumns, shiftWidget, showWidget, summaryRowsWithTraffic, trayIds, widgetCatalogue } from '../lib/widget-layout.ts';
import type { WidgetLayout, WidgetSpec } from '../lib/widget-layout.ts';

const specs: WidgetSpec[] = [
  { id: 'a', label: 'A', icon: 'info', min: 150 },
  { id: 'b', label: 'B', icon: 'info', min: 320 },
  { id: 'c', label: 'C', icon: 'info' },
  { id: 'd', label: 'D', icon: 'info' },
];
const rows = (layout: WidgetLayout) => layout.rows.map(row => row.columns + ':' + row.items.join(','));

test('every card of every view has a translated name and description, and one place in its default rows', () => {
  const missing: string[] = [];
  for (const spec of Object.values(widgetCatalogue).flat()) {
    for (const text of [spec.label, spec.description]) if (text && !/^[A-Z]{3,4}$/.test(text) && !Object.hasOwn(english, text)) missing.push(text);
  }
  assert.deepEqual(missing, []);
  for (const [scope, list] of Object.entries(widgetCatalogue)) {
    const ids = list.map(spec => spec.id);
    assert.equal(new Set(ids).size, list.length, scope + ' card ids are unique');
    for (const variant of scope === 'summary' ? [defaultRows.summary, summaryRowsWithTraffic] : [defaultRows[scope as keyof typeof defaultRows]]) {
      assert.deepEqual(variant.flatMap(row => row.items).sort(), [...ids].sort(), scope + ' places every card once');
      assert.ok(variant.every(row => row.items.length <= row.columns), scope + ' rows hold at most their columns');
    }
  }
});

test('the default arrangement follows its rows, and a forgotten card gets a row of its own', () => {
  assert.deepEqual(rows(defaultLayout(specs, [{ columns: 2, items: ['a', 'b'] }, { columns: 4, items: ['c', 'd'] }])), ['2:a,b', '4:c,d']);
  assert.deepEqual(rows(defaultLayout(specs, [{ columns: 2, items: ['a', 'b', 'c', 'gone', 'a'] }])), ['2:a,b', '2:c', '1:d'],
    'a row never holds more than its columns, unknown and repeated cards are dropped');
  assert.deepEqual(defaultLayout(specs, []).rows.map(row => row.id), ['row:0', 'row:1', 'row:2', 'row:3']);
  assert.deepEqual(rows(defaultLayout(widgetCatalogue.summary, defaultRows.summary)).slice(0, 1), ['3:status,address,sample'], 'the summary opens with its three stat cards');
});

test('a saved arrangement survives new and removed cards', () => {
  const defaults = defaultLayout(specs, [{ columns: 2, items: ['a', 'b'] }, { columns: 2, items: ['c', 'd'] }]);
  const saved = { rows: [{ id: 'x', columns: 2 as const, items: ['c', 'gone'] }, { id: 'y', columns: 1 as const, items: ['a'] }], hidden: ['gone'] };
  const layout = resolveLayout(specs, saved, defaults);
  assert.deepEqual(rows(layout), ['2:c,d', '1:a', '2:b'], 'unknown cards disappear; new ones join their default predecessor or a row of their default width below it');
  assert.deepEqual(layout.rows.map(row => row.id), ['row:0', 'row:1', 'row:2']);
  const hiddenBefore = resolveLayout(specs, { rows: [{ id: 'x', columns: 4, items: ['c'] }], hidden: ['a'] }, defaults);
  assert.deepEqual(rows(hiddenBefore), ['2:b', '4:c,d'], 'a hidden predecessor does not anchor a new card');
  assert.deepEqual(hiddenBefore.hidden, ['a']);
  assert.equal(resolveLayout(specs, null, defaults), defaults);
});

test('cards move between rows and the tray, and trade places in a full row', () => {
  let layout = defaultLayout(specs, [{ columns: 2, items: ['a', 'b'] }, { columns: 4, items: ['c', 'd'] }]);
  layout = placeWidget(layout, 'a', 'row:1', 0);
  assert.deepEqual(rows(layout), ['2:b', '4:a,c,d']);
  layout = placeWidget(layout, 'b', 'row:1', 2);
  assert.deepEqual(rows(layout), ['2:', '4:a,c,b,d'], 'an emptied row stays while arranging');
  assert.equal(placeWidget(layout, 'unknown', 'row:1', 0), layout, 'a card the arrangement does not hold is not moved');
  layout = placeWidget(defaultLayout(specs, [{ columns: 2, items: ['a', 'b'] }, { columns: 2, items: ['c', 'd'] }]), 'a', 'row:1', 1);
  assert.deepEqual(rows(layout), ['2:d,b', '2:c,a'], 'in a full row the moved card trades places with the card it lands on');
  layout = placeWidget(layout, 'c', TRAY, 0);
  assert.deepEqual([rows(layout), trayIds(layout)], [['2:d,b', '2:a'], ['c']]);
  layout = placeWidget(layout, 'c', 'row:0', 0);
  assert.deepEqual([rows(layout), trayIds(layout)], [['2:c,b', '2:a'], ['d']], 'a card from the tray sends the one it replaces to the tray');
  layout = placeWidget(layout, 'b', 'row:1', 99);
  assert.deepEqual(rows(layout), ['2:c', '2:a,b'], 'an index past the end appends');
  assert.equal(placeWidget(layout, 'b', 'row:9', 0), layout, 'an unknown row changes nothing');
});

test('step buttons trade places in reading order, and hidden cards come back at the end', () => {
  let layout = defaultLayout(specs, [{ columns: 2, items: ['a', 'b'] }, { columns: 1, items: ['c'] }, { columns: 1, items: ['d'] }]);
  layout = shiftWidget(layout, 'c', -1);
  assert.deepEqual(rows(layout), ['2:a,c', '1:b', '1:d'], 'rows keep their cards count');
  assert.equal(shiftWidget(layout, 'a', -1), layout, 'the first card cannot move earlier');
  layout = compactLayout(placeWidget(placeWidget(layout, 'd', TRAY, 0), 'b', TRAY, 1));
  assert.deepEqual(trayIds(shiftWidget(layout, 'b', -1)), ['b', 'd']);
  layout = showWidget(layout, 'd');
  assert.deepEqual(rows(layout), ['2:a,c', '2:d'], 'a full last row gets a new row of its width');
  layout = showWidget(layout, 'b');
  assert.deepEqual(rows(layout), ['2:a,c', '2:d,b']);
  assert.equal(showWidget(layout, 'b'), layout);
});

test('rows are added, narrowed, moved and removed', () => {
  let layout = defaultLayout(specs, [{ columns: 4, items: ['a', 'b', 'c', 'd'] }]);
  layout = setRowColumns(layout, 'row:0', 1);
  assert.deepEqual(rows(layout), ['1:a', '1:b', '1:c', '1:d'], 'a narrower row continues its cards in rows of the same width below');
  assert.equal(new Set(layout.rows.map(row => row.id)).size, 4);
  layout = setRowColumns(defaultLayout(specs, [{ columns: 4, items: ['a', 'b', 'c'] }, { columns: 1, items: ['d'] }]), 'row:0', 2);
  assert.deepEqual(rows(layout), ['2:a,b', '2:c', '1:d']);
  layout = moveRow(layout, 'row:2', 1);
  assert.deepEqual(rows(layout), ['2:a,b', '1:d', '2:c']);
  assert.equal(moveRow(layout, layout.rows[0].id, -1), layout);
  layout = addRow(layout, 3);
  assert.deepEqual(rows(layout).at(-1), '3:');
  assert.ok(!sameLayout(layout, compactLayout(layout)), 'an empty row only lives in the editor');
  assert.deepEqual(rows(compactLayout(layout)), ['2:a,b', '1:d', '2:c']);
  layout = removeRow(layout, layout.rows[0].id);
  assert.deepEqual([rows(layout), trayIds(layout)], [['1:d', '2:c', '3:'], ['a', 'b']], 'the cards of a removed row are hidden');
  assert.deepEqual(boardIds(layout), ['d', 'c']);
});

test('a row shows all its columns while its cards stay readable, then 4 → 2 → 1 or 3 → 2 → 1', () => {
  assert.equal(fitColumns(4, 1400, 320, 20), 4);
  assert.equal(fitColumns(4, 1200, 320, 20), 2, 'four charts never wrap as three and one');
  assert.equal(fitColumns(4, 600, 320, 20), 1);
  assert.equal(fitColumns(3, 1000, 320, 20), 3);
  assert.equal(fitColumns(3, 700, 320, 20), 2);
  assert.equal(fitColumns(2, 600, 320, 20), 1);
  assert.equal(fitColumns(3, 0, 320, 20), 3, 'an unmeasured row shows its columns');
  assert.equal(fitColumns(0, 900, 0, 20), 1);
  assert.equal(rowMin(specs, ['a', 'b', 'c']), 320, 'the widest card sets the row minimum');
});

test('arrangements compare by value and are stored versioned', () => {
  const layout = placeWidget(defaultLayout(specs, [{ columns: 3, items: ['a', 'b', 'c'] }, { columns: 1, items: ['d'] }]), 'c', TRAY, 0);
  assert.deepEqual(parseLayout(serializeLayout(layout)), layout);
  assert.ok(sameLayout({ ...layout, rows: layout.rows.map((row, index) => ({ ...row, id: 'other' + index })) }, layout), 'row ids do not count');
  assert.equal(parseLayout('{"order":["a"],"hidden":[]}'), null, 'the unversioned card menu format is ignored');
  assert.equal(parseLayout('not json'), null);
  assert.deepEqual(parseLayout('{"v":3,"rows":[{"columns":5,"items":["a"]},{"columns":2,"items":["b",3]},null],"hidden":"x"}'),
    { rows: [{ id: 'row:0', columns: 2, items: ['b'] }], hidden: [] });
  const widths = parseLayout('{"v":2,"order":["a","b","c","d"],"hidden":["d"],"sizes":{"a":"small","b":"small","c":"large","d":"medium"}}')!;
  assert.deepEqual([rows(widths), widths.hidden], [['3:a,b', '1:c'], ['d']], 'arrangements of the previous widths become rows');
});
