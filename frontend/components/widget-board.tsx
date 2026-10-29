'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { Suspense, lazy, useCallback, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { Icon } from './icon';
import { PageAction } from './page-actions';
import { defaultLayout, defaultRows, fitColumns, resolveLayout, rowMin, savedLayout, sameLayout, storeLayout, subscribeLayout, widgetCatalogue } from '@/lib/widget-layout';
import type { RowSpec, WidgetLayout, WidgetScope, WidgetSpec } from '@/lib/widget-layout';

// Drag and drop is only needed while arranging, so it loads on demand.
const WidgetEditor = lazy(() => import('./widget-editor'));

/** A card of the view: its catalogue entry (lib/widget-layout.ts) plus what the page knows right now. */
export type Widget = {
  id: string;
  render: () => ReactNode;
  /** False leaves the card out of the page, e.g. GPS while position acquisition is off. */
  available?: boolean;
  /** Why the card is not shown now; the arrangement editor explains it. */
  unavailable?: string;
};

const titles: Record<WidgetScope, string> = {
  summary: "Customize the summary", signal: "Customize the signal charts", performance: "Customize the performance charts",
};

/** Content width and gap of an element, measured before paint so a row never shows at the wrong number of columns. */
export function useRowWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, gap: 0 });
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const style = getComputedStyle(element);
      const width = element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight), gap = parseFloat(style.columnGap) || 0;
      setSize(old => old.width === width && old.gap === gap ? old : { width, gap });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, size] as const;
}

/** The columns a row shows: --row-fit for styles/widgets.css, and a first card across the row when three cards wrap to two columns. */
export const rowStyle = (fit: number, count: number) => ({ style: { '--row-fit': fit } as CSSProperties, 'data-lead': count % fit ? '' : undefined });

/** The rows of the page; cards that are not available now leave their row to the others. */
function Rows({ scope, specs, layout, byId }: { scope: WidgetScope; specs: WidgetSpec[]; layout: WidgetLayout; byId: Map<string, Widget> }) {
  const [ref, { width, gap }] = useRowWidth<HTMLDivElement>();
  return <div ref={ref} className="widget-board" data-board={scope}>
    {layout.rows.map(row => {
      const shown = row.items.filter(id => byId.has(id) && byId.get(id)!.available !== false);
      if (!shown.length) return null;
      return <div key={row.id} className="widget-row" data-columns={row.columns} {...rowStyle(fitColumns(shown.length, width, rowMin(specs, shown), gap), shown.length)}>
        {shown.map(id => <div key={id} className="widget" data-widget={id}>{byId.get(id)!.render()}</div>)}
      </div>;
    })}
  </div>;
}

/** The cards of a view in the rows and visibility chosen in this browser, with their arrangement editor. */
export function WidgetBoard({ scope, widgets, defaults }: { scope: WidgetScope; widgets: Widget[]; defaults?: RowSpec[] }) {
  usePreferences();
  const specs = widgetCatalogue[scope];
  const subscribe = useCallback((listener: () => void) => subscribeLayout(scope, listener), [scope]);
  const saved = useSyncExternalStore(subscribe, () => savedLayout(scope), () => null);
  const fallback = defaultLayout(specs, defaults ?? defaultRows[scope]);
  const layout = resolveLayout(specs, saved, fallback);
  const [editing, setEditing] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const byId = new Map(widgets.map(widget => [widget.id, widget]));
  const unavailable = Object.fromEntries(widgets.filter(widget => widget.available === false)
    .map(widget => [widget.id, widget.unavailable ?? "Not available now."]));
  const finish = useCallback((next: WidgetLayout | null) => {
    // The default arrangement is not stored, so a view keeps following its own defaults.
    if (next) storeLayout(scope, sameLayout(next, defaultLayout(widgetCatalogue[scope], defaults ?? defaultRows[scope])) ? null : next);
    setEditing(false);
    window.requestAnimationFrame(() => opener.current?.focus({ preventScroll: true }));
  }, [scope, defaults]);
  return <>
    {!editing && <PageAction>
      <button ref={opener} type="button" id={'customize-' + scope} className="button button-secondary page-action" title={t(titles[scope])}
        onClick={() => setEditing(true)}><Icon name="sliders" size={16} /><span className="page-action-label">{t("Customize")}</span></button>
    </PageAction>}
    {editing ? <Suspense fallback={<div className="card widget-editor-loading" role="status">{t("Loading…")}</div>}>
      <WidgetEditor title={t(titles[scope])} specs={specs} layout={layout} defaults={fallback} unavailable={unavailable} onFinish={finish} />
    </Suspense> : <Rows scope={scope} specs={specs} layout={layout} byId={byId} />}
  </>;
}
