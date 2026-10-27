'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { Suspense, lazy, useCallback, useRef, useState, useSyncExternalStore } from 'react';
import type { ReactNode } from 'react';
import { Icon } from './icon';
import { PageAction } from './page-actions';
import { boardIds, defaultLayout, resolveLayout, savedLayout, sameLayout, storeLayout, subscribeLayout, widgetCatalogue, widgetStyle } from '@/lib/widget-layout';
import type { WidgetLayout, WidgetScope, WidgetSize } from '@/lib/widget-layout';

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
export type WidgetDefaults = { order?: string[]; sizes?: Partial<Record<string, WidgetSize>> };

const titles: Record<WidgetScope, string> = {
  summary: "Customize the summary", signal: "Customize the signal charts", performance: "Customize the performance charts",
};

/** The cards of a view in the order, visibility and width chosen in this browser, with their arrangement editor. */
export function WidgetBoard({ scope, widgets, defaults }: { scope: WidgetScope; widgets: Widget[]; defaults?: WidgetDefaults }) {
  usePreferences();
  const specs = widgetCatalogue[scope];
  const subscribe = useCallback((listener: () => void) => subscribeLayout(scope, listener), [scope]);
  const saved = useSyncExternalStore(subscribe, () => savedLayout(scope), () => null);
  const fallback = defaultLayout(specs, defaults);
  const layout = resolveLayout(specs, saved, fallback);
  const [editing, setEditing] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const byId = new Map(widgets.map(widget => [widget.id, widget]));
  const unavailable = Object.fromEntries(widgets.filter(widget => widget.available === false)
    .map(widget => [widget.id, widget.unavailable ?? "Not available now."]));
  const finish = useCallback((next: WidgetLayout | null) => {
    // The default arrangement is not stored, so a view keeps following its own defaults.
    if (next) storeLayout(scope, sameLayout(next, defaultLayout(widgetCatalogue[scope], defaults)) ? null : next);
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
    </Suspense> : <div className="widget-board" data-board={scope}>
      {boardIds(layout).map(id => {
        const widget = byId.get(id);
        if (!widget || widget.available === false) return null;
        return <div key={id} className="widget" data-widget={id} data-size={layout.sizes[id]} style={widgetStyle(specs.find(spec => spec.id === id))}>
          {widget.render()}</div>;
      })}
    </div>}
  </>;
}
