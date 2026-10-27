/** Card arrangement of a view, kept in this browser: the order, visibility and width of each card. */
export type WidgetSize = 'small' | 'medium' | 'large';
export const widgetSizes: readonly WidgetSize[] = ['small', 'medium', 'large'];
export const widgetSizeLabels: Record<WidgetSize, string> = { small: "Narrow", medium: "Medium", large: "Full width" };

export type WidgetSpec = {
  id: string;
  /** English message ids, translated where they are shown. */
  label: string;
  description?: string;
  icon: string;
  size: WidgetSize;
  /** Widths offered for the card; every width by default. */
  sizes?: readonly WidgetSize[];
  /** Narrowest readable width in px: a narrow chart takes a row of its own sooner than a narrow stat card. */
  min?: number;
};
/** Visible cards come first in `order`; `hidden` lists the cards kept out of the page. */
export type WidgetLayout = { order: string[]; hidden: string[]; sizes: Record<string, WidgetSize> };
export type WidgetContainer = 'board' | 'tray';
export type WidgetScope = 'summary' | 'signal' | 'performance';

const wide: readonly WidgetSize[] = ['medium', 'large'];

export const summaryWidgets: WidgetSpec[] = [
  { id: 'status', label: "Local connection", description: "Data session state and its verified exit.", icon: 'activity', size: 'small', min: 150 },
  { id: 'address', label: "IPv4 address", description: "Address, gateway and route metric.", icon: 'globe', size: 'small', min: 170 },
  { id: 'sample', label: "Latest sample", description: "Number and age of the last acquisition.", icon: 'clock', size: 'small', min: 150 },
  { id: 'connection', label: "UE and connection", description: "Registration, data session, IMEI and IMSI.", icon: 'router', size: 'medium', min: 280 },
  { id: 'network', label: "IP network", description: "Interface, gateway, PLMN and APN.", icon: 'globe', size: 'medium', min: 280 },
  { id: 'cells', label: "Serving cells", description: "Identity, channel and observed bands of each cell.", icon: 'radio', size: 'large', sizes: wide, min: 320 },
  { id: 'signal', label: "Recent signal", description: "RSRP, RSRQ and SNR with their recent trend.", icon: 'signal', size: 'large', sizes: wide, min: 320 },
  { id: 'traffic', label: "WWAN traffic", description: "Download and upload on the modem interface.", icon: 'arrow-down', size: 'medium', min: 280 },
  { id: 'gps', label: 'GPS', description: "Position, source and age of the fix.", icon: 'gps', size: 'medium', min: 260 },
];

const signalChart = "Recent values against the quality ranges.";
export const signalWidgets: WidgetSpec[] = [
  { id: 'rsrp', label: 'RSRP', description: signalChart, icon: 'signal', size: 'small', min: 320 },
  { id: 'rsrq', label: 'RSRQ', description: signalChart, icon: 'signal', size: 'small', min: 320 },
  { id: 'snr', label: 'SNR', description: signalChart, icon: 'signal', size: 'small', min: 320 },
  { id: 'cells', label: "Serving cells", description: "Identity, channel and observed bands of each cell.", icon: 'radio', size: 'large', sizes: wide, min: 320 },
];

export const performanceWidgets: WidgetSpec[] = [
  { id: 'traffic', label: "WWAN traffic", description: "Download and upload on the modem interface.", icon: 'arrow-down', size: 'large', sizes: wide, min: 280 },
  { id: 'download', label: "WWAN · Download", icon: 'chart', size: 'medium', min: 320 },
  { id: 'upload', label: "WWAN · Upload", icon: 'chart', size: 'medium', min: 320 },
  { id: 'acquisition', label: "Acquisition duration", icon: 'clock', size: 'medium', min: 320 },
  { id: 'period', label: "Actual period and lag", icon: 'clock', size: 'medium', min: 320 },
  { id: 'queue', label: "Storage queue", icon: 'database', size: 'medium', min: 320 },
  { id: 'writes', label: "Write duration", icon: 'download', size: 'medium', min: 320 },
  { id: 'cpu', label: "Process CPU", icon: 'cpu', size: 'medium', min: 320 },
  { id: 'memory', label: "Resident memory", icon: 'cpu', size: 'medium', min: 320 },
  { id: 'notes', label: "Acquisition, storage and resource details", icon: 'info', size: 'large', sizes: wide, min: 300 },
];

export const widgetCatalogue: Record<WidgetScope, WidgetSpec[]> = { summary: summaryWidgets, signal: signalWidgets, performance: performanceWidgets };

export const allowedSizes = (spec: WidgetSpec) => spec.sizes ?? widgetSizes;
/** Inline style of a card: its narrowest readable width, used by styles/widgets.css. */
export const widgetStyle = (spec: WidgetSpec | undefined) => ({ '--widget-min': (spec?.min ?? 0) + 'px' }) as Record<string, string>;

/** The arrangement a view shows until it is customised; a view may reorder or resize its defaults. */
export function defaultLayout(specs: readonly WidgetSpec[], variant: { order?: string[]; sizes?: Partial<Record<string, WidgetSize>> } = {}): WidgetLayout {
  const ids = specs.map(spec => spec.id);
  const order = [...(variant.order ?? []).filter((id, index, list) => ids.includes(id) && list.indexOf(id) === index),
    ...ids.filter(id => !variant.order?.includes(id))];
  const sizes = Object.fromEntries(specs.map(spec => {
    const size = variant.sizes?.[spec.id];
    return [spec.id, size && allowedSizes(spec).includes(size) ? size : spec.size];
  }));
  return { order, hidden: [], sizes };
}

/** A saved arrangement against the current cards: unknown cards are dropped, new ones join at their default place. */
export function resolveLayout(specs: readonly WidgetSpec[], saved: WidgetLayout | null, defaults = defaultLayout(specs)): WidgetLayout {
  if (!saved) return defaults;
  const known = new Map(specs.map(spec => [spec.id, spec]));
  const order = saved.order.filter((id, index, list) => known.has(id) && list.indexOf(id) === index);
  const hidden = saved.hidden.filter(id => order.includes(id));
  for (const id of defaults.order) {
    if (order.includes(id)) continue;
    // A card added after the arrangement was saved goes right after its default predecessor.
    const before = defaults.order.slice(0, defaults.order.indexOf(id)).reverse().find(other => order.includes(other) && !hidden.includes(other));
    order.splice(before ? order.indexOf(before) + 1 : 0, 0, id);
  }
  const sizes = Object.fromEntries(order.map(id => {
    const size = saved.sizes[id];
    return [id, size && allowedSizes(known.get(id)!).includes(size) ? size : defaults.sizes[id]];
  }));
  return normalize({ order, hidden, sizes });
}

/** Visible cards first, then hidden ones, each group in its own order. */
function normalize(layout: WidgetLayout): WidgetLayout {
  const hidden = new Set(layout.hidden);
  return { order: [...layout.order.filter(id => !hidden.has(id)), ...layout.order.filter(id => hidden.has(id))],
    hidden: layout.order.filter(id => hidden.has(id)), sizes: layout.sizes };
}

export const boardIds = (layout: WidgetLayout) => layout.order.filter(id => !layout.hidden.includes(id));
export const trayIds = (layout: WidgetLayout) => layout.order.filter(id => layout.hidden.includes(id));
export const containerOf = (layout: WidgetLayout, id: string): WidgetContainer => layout.hidden.includes(id) ? 'tray' : 'board';

/** Puts a card at `index` of the board or the tray, counted without the card itself. */
export function placeWidget(layout: WidgetLayout, id: string, container: WidgetContainer, index: number): WidgetLayout {
  if (!layout.order.includes(id)) return layout;
  const board = boardIds(layout).filter(item => item !== id), tray = trayIds(layout).filter(item => item !== id);
  const target = container === 'board' ? board : tray;
  target.splice(Math.max(0, Math.min(index, target.length)), 0, id);
  return { order: [...board, ...tray], hidden: tray, sizes: layout.sizes };
}

/** One step earlier or later within the card's own group: the single-pointer alternative to dragging. */
export function shiftWidget(layout: WidgetLayout, id: string, delta: -1 | 1): WidgetLayout {
  const container = containerOf(layout, id), items = container === 'board' ? boardIds(layout) : trayIds(layout);
  const index = items.indexOf(id);
  if (index < 0 || index + delta < 0 || index + delta >= items.length) return layout;
  return placeWidget(layout, id, container, index + delta);
}

export function resizeWidget(layout: WidgetLayout, id: string, size: WidgetSize): WidgetLayout {
  return layout.sizes[id] === size ? layout : { ...layout, sizes: { ...layout.sizes, [id]: size } };
}

export function sameLayout(a: WidgetLayout, b: WidgetLayout): boolean {
  return a.order.join() === b.order.join() && a.hidden.join() === b.hidden.join()
    && a.order.every(id => a.sizes[id] === b.sizes[id]);
}

export function parseLayout(raw: string | null): WidgetLayout | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as { v?: unknown; order?: unknown; hidden?: unknown; sizes?: unknown };
    if (value?.v !== 2) return null;
    const list = (items: unknown) => Array.isArray(items) ? items.filter((item): item is string => typeof item === 'string') : [];
    const sizes: Record<string, WidgetSize> = {};
    if (value.sizes && typeof value.sizes === 'object') {
      for (const [id, size] of Object.entries(value.sizes)) if (widgetSizes.includes(size as WidgetSize)) sizes[id] = size as WidgetSize;
    }
    return { order: list(value.order), hidden: list(value.hidden), sizes };
  } catch { return null; }
}

export const serializeLayout = (layout: WidgetLayout) => JSON.stringify({ v: 2, order: layout.order, hidden: layout.hidden, sizes: layout.sizes });

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
