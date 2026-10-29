/** Card arrangement of a view, kept in this browser: rows of one to four cards, and the cards kept out of the page. */
export type RowColumns = 1 | 2 | 3 | 4;
export const rowColumns: readonly RowColumns[] = [1, 2, 3, 4];

export type WidgetSpec = {
  id: string;
  /** English message ids, translated where they are shown. */
  label: string;
  description?: string;
  icon: string;
  /** Narrowest readable width in px: a row of charts wraps sooner than a row of stat cards. */
  min?: number;
};
/** A row holds up to `columns` cards; fewer cards share its whole width. */
export type WidgetRow = { id: string; columns: RowColumns; items: string[] };
/** `hidden` lists the cards kept out of the page, in the order of the tray. */
export type WidgetLayout = { rows: WidgetRow[]; hidden: string[] };
/** A row of a default arrangement, before it gets its id. */
export type RowSpec = { columns: RowColumns; items: string[] };
export type WidgetScope = 'summary' | 'signal' | 'performance';
/** The tray of hidden cards, as a drop target beside the rows. */
export const TRAY = 'widget-tray';

export const summaryWidgets: WidgetSpec[] = [
  { id: 'status', label: "Local connection", description: "Data session state and its verified exit.", icon: 'activity', min: 150 },
  { id: 'address', label: "IPv4 address", description: "Address, gateway and route metric.", icon: 'globe', min: 170 },
  { id: 'sample', label: "Latest sample", description: "Number and age of the last acquisition.", icon: 'clock', min: 150 },
  { id: 'connection', label: "UE and connection", description: "Registration, data session, IMEI and IMSI.", icon: 'router', min: 280 },
  { id: 'network', label: "IP network", description: "Interface, gateway, PLMN and APN.", icon: 'globe', min: 280 },
  { id: 'cells', label: "Serving cells", description: "Identity, channel and observed bands of each cell.", icon: 'radio', min: 320 },
  { id: 'signal', label: "Recent signal", description: "RSRP, RSRQ and SNR with their recent trend.", icon: 'signal', min: 320 },
  { id: 'traffic', label: "WWAN traffic", description: "Download and upload on the modem interface.", icon: 'arrow-down', min: 280 },
  { id: 'gps', label: 'GPS', description: "Position, source and age of the fix.", icon: 'gps', min: 260 },
];

const signalChart = "Recent values against the quality ranges.";
export const signalWidgets: WidgetSpec[] = [
  { id: 'rsrp', label: 'RSRP', description: signalChart, icon: 'signal', min: 320 },
  { id: 'rsrq', label: 'RSRQ', description: signalChart, icon: 'signal', min: 320 },
  { id: 'snr', label: 'SNR', description: signalChart, icon: 'signal', min: 320 },
  { id: 'cells', label: "Serving cells", description: "Identity, channel and observed bands of each cell.", icon: 'radio', min: 320 },
];

export const performanceWidgets: WidgetSpec[] = [
  { id: 'traffic', label: "WWAN traffic", description: "Download and upload on the modem interface.", icon: 'arrow-down', min: 280 },
  { id: 'download', label: "WWAN · Download", icon: 'chart', min: 320 },
  { id: 'upload', label: "WWAN · Upload", icon: 'chart', min: 320 },
  { id: 'acquisition', label: "Acquisition duration", icon: 'clock', min: 320 },
  { id: 'period', label: "Actual period and lag", icon: 'clock', min: 320 },
  { id: 'queue', label: "Storage queue", icon: 'database', min: 320 },
  { id: 'writes', label: "Write duration", icon: 'download', min: 320 },
  { id: 'cpu', label: "Process CPU", icon: 'cpu', min: 320 },
  { id: 'memory', label: "Resident memory", icon: 'cpu', min: 320 },
  { id: 'notes', label: "Acquisition, storage and resource details", icon: 'info', min: 300 },
];

export const widgetCatalogue: Record<WidgetScope, WidgetSpec[]> = { summary: summaryWidgets, signal: signalWidgets, performance: performanceWidgets };

/** The rows a view shows until it is customised. */
export const defaultRows: Record<WidgetScope, RowSpec[]> = {
  summary: [{ columns: 3, items: ['status', 'address', 'sample'] }, { columns: 2, items: ['connection', 'network'] },
    { columns: 1, items: ['cells'] }, { columns: 1, items: ['signal'] }, { columns: 2, items: ['traffic', 'gps'] }],
  signal: [{ columns: 3, items: ['rsrp', 'rsrq', 'snr'] }, { columns: 1, items: ['cells'] }],
  performance: [{ columns: 1, items: ['traffic'] }, { columns: 2, items: ['download', 'upload'] },
    { columns: 4, items: ['acquisition', 'period', 'queue', 'writes'] }, { columns: 2, items: ['cpu', 'memory'] }, { columns: 1, items: ['notes'] }],
};
/** A single serving cell leaves the traffic panel room beside it on the summary. */
export const summaryRowsWithTraffic: RowSpec[] = [{ columns: 3, items: ['status', 'address', 'sample'] }, { columns: 2, items: ['connection', 'network'] },
  { columns: 2, items: ['cells', 'traffic'] }, { columns: 1, items: ['signal'] }, { columns: 2, items: ['gps'] }];

const fits: Record<RowColumns, RowColumns[]> = { 1: [1], 2: [2, 1], 3: [3, 2, 1], 4: [4, 2, 1] };
/** Columns a row of `count` cards shows at a width: all of them while each card stays readable, else 4 → 2 → 1 and 3 → 2 → 1. */
export function fitColumns(count: number, width: number, min: number, gap: number): RowColumns {
  const columns = Math.min(4, Math.max(1, count)) as RowColumns;
  if (!width) return columns;
  return fits[columns].find(fit => (width - (fit - 1) * gap) / fit >= min) ?? 1;
}
/** The narrowest readable width of a row: that of its widest card. */
export const rowMin = (specs: readonly WidgetSpec[], ids: readonly string[]) =>
  Math.max(0, ...ids.map(id => specs.find(spec => spec.id === id)?.min ?? 0));

const rowId = (index: number) => 'row:' + index;
export const isRowId = (id: string) => id.startsWith('row:');
const withIds = (rows: readonly RowSpec[]): WidgetRow[] => rows.map((row, index) => ({ id: rowId(index), columns: row.columns, items: [...row.items] }));
const nextRowId = (rows: readonly WidgetRow[]) => Math.max(-1, ...rows.map(row => Number(row.id.slice(4)) || 0)) + 1;

/** Rows within their width: the cards that do not fit continue in rows of the same width. */
function split(rows: readonly RowSpec[]): RowSpec[] {
  return rows.flatMap(row => Array.from({ length: Math.max(1, Math.ceil(row.items.length / row.columns)) },
    (_, index) => ({ columns: row.columns, items: row.items.slice(index * row.columns, (index + 1) * row.columns) })));
}

/** Known cards once each, rows within their width and no empty rows. */
function tidy(known: Set<string>, rows: readonly RowSpec[], hidden: readonly string[]) {
  const seen = new Set<string>();
  const keep = (id: string) => known.has(id) && !seen.has(id) && !!seen.add(id);
  return { rows: split(rows.map(row => ({ columns: row.columns, items: row.items.filter(keep) }))).filter(row => row.items.length),
    hidden: hidden.filter(keep) };
}

/** The arrangement a view shows until it is customised; a card its rows forget gets a row of its own at the end. */
export function defaultLayout(specs: readonly WidgetSpec[], rows: readonly RowSpec[] = []): WidgetLayout {
  const tidied = tidy(new Set(specs.map(spec => spec.id)), rows, []), placed = new Set(tidied.rows.flatMap(row => row.items));
  return { rows: withIds([...tidied.rows, ...specs.filter(spec => !placed.has(spec.id)).map(spec => ({ columns: 1 as const, items: [spec.id] }))]), hidden: [] };
}

/** A saved arrangement against the current cards: unknown cards are dropped, new ones join at their default place. */
export function resolveLayout(specs: readonly WidgetSpec[], saved: WidgetLayout | null, defaults = defaultLayout(specs)): WidgetLayout {
  if (!saved) return defaults;
  const { rows, hidden } = tidy(new Set(specs.map(spec => spec.id)), saved.rows, saved.hidden);
  const order = defaults.rows.flatMap(row => row.items);
  for (const id of order) {
    if (hidden.includes(id) || rows.some(row => row.items.includes(id))) continue;
    // A card added after the arrangement was saved joins its default predecessor, or a row of its default width right below it.
    const before = order.slice(0, order.indexOf(id)).reverse().find(other => rows.some(row => row.items.includes(other)));
    const index = before ? rows.findIndex(row => row.items.includes(before)) : -1, row = rows[index];
    if (row && before && row.items.length < row.columns) row.items.splice(row.items.indexOf(before) + 1, 0, id);
    else rows.splice(index + 1, 0, { columns: defaults.rows.find(item => item.items.includes(id))!.columns, items: [id] });
  }
  return { rows: withIds(rows), hidden };
}

export const boardIds = (layout: WidgetLayout) => layout.rows.flatMap(row => row.items);
export const trayIds = (layout: WidgetLayout) => layout.hidden;
/** The row id of a card, the tray, or null for a card the arrangement does not hold. */
export function containerOf(layout: WidgetLayout, id: string): string | null {
  return layout.hidden.includes(id) ? TRAY : layout.rows.find(row => row.items.includes(id))?.id ?? null;
}
export const itemsOf = (layout: WidgetLayout, container: string) =>
  container === TRAY ? layout.hidden : layout.rows.find(row => row.id === container)?.items ?? [];

/** Puts a card at `index` of a row or the tray, counted without the card itself; in a full row it trades places with the card there. */
export function placeWidget(layout: WidgetLayout, id: string, container: string, index: number): WidgetLayout {
  const from = containerOf(layout, id), target = layout.rows.find(row => row.id === container);
  if (!from || (container !== TRAY && !target)) return layout;
  const fromIndex = itemsOf(layout, from).indexOf(id), without = (items: string[]) => items.filter(item => item !== id);
  const rows = layout.rows.map(row => ({ ...row, items: without(row.items) })), hidden = without(layout.hidden);
  const list = itemsOf({ rows, hidden }, container), at = Math.max(0, Math.min(index, list.length));
  if (target && list.length >= target.columns) {
    const swap = Math.min(at, list.length - 1);
    itemsOf({ rows, hidden }, from).splice(fromIndex, 0, list[swap]);
    list[swap] = id;
  } else list.splice(at, 0, id);
  return { rows, hidden };
}

/** Trades places with the previous or next card, in reading order or in the tray: the single-pointer alternative to dragging. */
export function shiftWidget(layout: WidgetLayout, id: string, delta: -1 | 1): WidgetLayout {
  const tray = layout.hidden.includes(id), items = tray ? layout.hidden : boardIds(layout);
  const index = items.indexOf(id), other = items[index + delta];
  if (index < 0 || other === undefined) return layout;
  const swap = (item: string) => item === id ? other : item === other ? id : item;
  return tray ? { rows: layout.rows, hidden: layout.hidden.map(swap) } : { rows: layout.rows.map(row => ({ ...row, items: row.items.map(swap) })), hidden: layout.hidden };
}

/** A hidden card comes back at the end of the last row, or in a new row of the same width when that one is full. */
export function showWidget(layout: WidgetLayout, id: string): WidgetLayout {
  if (!layout.hidden.includes(id)) return layout;
  const last = layout.rows.at(-1);
  if (last && last.items.length < last.columns) return placeWidget(layout, id, last.id, last.items.length);
  const added = addRow(layout, last?.columns ?? 2);
  return placeWidget(added, id, added.rows.at(-1)!.id, 0);
}

export function addRow(layout: WidgetLayout, columns: RowColumns): WidgetLayout {
  return { ...layout, rows: [...layout.rows, { id: rowId(nextRowId(layout.rows)), columns, items: [] }] };
}

/** A narrower row keeps its first cards; the others continue in new rows of the same width right below it. */
export function setRowColumns(layout: WidgetLayout, id: string, columns: RowColumns): WidgetLayout {
  const index = layout.rows.findIndex(row => row.id === id), row = layout.rows[index];
  if (!row || row.columns === columns) return layout;
  let next = nextRowId(layout.rows);
  const pieces = split([{ columns, items: row.items }]).map((piece, part) => ({ id: part ? rowId(next++) : row.id, ...piece }));
  return { ...layout, rows: [...layout.rows.slice(0, index), ...pieces, ...layout.rows.slice(index + 1)] };
}

export function moveRow(layout: WidgetLayout, id: string, delta: -1 | 1): WidgetLayout {
  const index = layout.rows.findIndex(row => row.id === id), other = index + delta;
  if (index < 0 || other < 0 || other >= layout.rows.length) return layout;
  const rows = [...layout.rows];
  [rows[index], rows[other]] = [rows[other], rows[index]];
  return { ...layout, rows };
}

/** The cards of a removed row wait in the tray. */
export function removeRow(layout: WidgetLayout, id: string): WidgetLayout {
  const row = layout.rows.find(item => item.id === id);
  return row ? { rows: layout.rows.filter(item => item !== row), hidden: [...layout.hidden, ...row.items] } : layout;
}

/** Empty rows only exist while arranging. */
export const compactLayout = (layout: WidgetLayout): WidgetLayout =>
  layout.rows.every(row => row.items.length) ? layout : { ...layout, rows: layout.rows.filter(row => row.items.length) };

/** Arrangements compare by their rows and tray, whatever the ids of their rows. */
export function sameLayout(a: WidgetLayout, b: WidgetLayout): boolean {
  const key = (layout: WidgetLayout) => JSON.stringify([layout.rows.map(row => [row.columns, row.items]), layout.hidden]);
  return key(a) === key(b);
}

/** The previous per-card widths as rows: narrow cards three to a row, medium ones two, full-width ones alone. */
function fromWidths(order: string[], hidden: string[], sizes: unknown): WidgetLayout {
  const widths: Record<string, RowColumns> = { small: 3, medium: 2, large: 1 };
  const rows: RowSpec[] = [];
  for (const id of order.filter(item => !hidden.includes(item))) {
    const size = sizes && typeof sizes === 'object' ? (sizes as Record<string, unknown>)[id] : undefined;
    const columns = typeof size === 'string' && Object.hasOwn(widths, size) ? widths[size] : 2, last = rows.at(-1);
    if (last && last.columns === columns && last.items.length < columns) last.items.push(id); else rows.push({ columns, items: [id] });
  }
  return { rows: withIds(rows), hidden };
}

export function parseLayout(raw: string | null): WidgetLayout | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as { v?: unknown; rows?: unknown; hidden?: unknown; order?: unknown; sizes?: unknown };
    const list = (items: unknown) => Array.isArray(items) ? items.filter((item): item is string => typeof item === 'string') : [];
    if (value?.v === 2) return fromWidths(list(value.order), list(value.hidden), value.sizes);
    if (value?.v !== 3 || !Array.isArray(value.rows)) return null;
    const rows = (value.rows as { columns?: unknown; items?: unknown }[])
      .filter(row => rowColumns.includes(row?.columns as RowColumns)).map(row => ({ columns: row.columns as RowColumns, items: list(row.items) }));
    return { rows: withIds(rows), hidden: list(value.hidden) };
  } catch { return null; }
}

export const serializeLayout = (layout: WidgetLayout) =>
  JSON.stringify({ v: 3, rows: layout.rows.map(row => ({ columns: row.columns, items: row.items })), hidden: layout.hidden });

/* Browser storage, readable through useSyncExternalStore. Disabled storage falls back to memory. */
const storageKey = (scope: string) => 'qmi-supervisor.widgets.' + scope;
const memory = new Map<string, string | null>();
const cache = new Map<string, { raw: string | null; layout: WidgetLayout | null }>();
const listeners = new Map<string, Set<() => void>>();

function readRaw(scope: string): string | null {
  if (typeof window === 'undefined') return null;
  try { return window.localStorage.getItem(storageKey(scope)); } catch { return memory.get(scope) ?? null; }
}

export function savedLayout(scope: string): WidgetLayout | null {
  const raw = readRaw(scope), hit = cache.get(scope);
  if (hit && hit.raw === raw) return hit.layout;
  const layout = parseLayout(raw);
  cache.set(scope, { raw, layout });
  return layout;
}

/** Saves an arrangement; null returns the view to its default arrangement. */
export function storeLayout(scope: string, layout: WidgetLayout | null): void {
  const raw = layout ? serializeLayout(layout) : null;
  memory.set(scope, raw);
  try {
    if (raw) window.localStorage.setItem(storageKey(scope), raw); else window.localStorage.removeItem(storageKey(scope));
  } catch { /* Memory keeps the arrangement for this page. */ }
  for (const listener of listeners.get(scope) ?? []) listener();
}

export function subscribeLayout(scope: string, listener: () => void): () => void {
  const set = listeners.get(scope) ?? new Set();
  listeners.set(scope, set);
  set.add(listener);
  // Another tab of this browser changed the arrangement.
  const storage = (event: StorageEvent) => { if (event.key === null || event.key === storageKey(scope)) listener(); };
  window.addEventListener('storage', storage);
  return () => { set.delete(listener); window.removeEventListener('storage', storage); };
}
