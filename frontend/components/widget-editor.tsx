'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEventHandler, ReactNode } from 'react';
import { DndContext, DragOverlay, KeyboardSensor, MeasuringStrategy, MouseSensor, TouchSensor, closestCenter, pointerWithin,
  rectIntersection, useDroppable, useSensor, useSensors } from '@dnd-kit/core';
import type { Active, Announcements, CollisionDetection, DragOverEvent, DragStartEvent, Over, UniqueIdentifier } from '@dnd-kit/core';
import { SortableContext, sortableKeyboardCoordinates, useSortable } from '@dnd-kit/sortable';
import type { SortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { Icon } from './icon';
import { allowedSizes, boardIds, containerOf, placeWidget, resizeWidget, sameLayout, shiftWidget, trayIds, widgetSizeLabels, widgetStyle } from '@/lib/widget-layout';
import type { WidgetContainer, WidgetLayout, WidgetSize, WidgetSpec } from '@/lib/widget-layout';

const BOARD = 'widget-board', TRAY = 'widget-tray';
/** Cards keep their own width: the board reflows while dragging instead of stretching cards into each other's slots. */
const reflow: SortingStrategy = () => null;
/** Controls inside a card never start a drag; the handle and the card body do. */
const interactive = 'input, label, a, button:not(.widget-handle)';

type Tools = {
  size: WidgetSize; container: WidgetContainer; first: boolean; last: boolean; reason?: string;
  onSize: (size: WidgetSize) => void; onShift: (delta: -1 | 1) => void; onToggle: () => void;
};

/** The share of the row a width takes on a wide screen: a third, a half or all of it. */
function SizeGlyph({ size }: { size: WidgetSize }) {
  const share = size === 'small' ? 5 : size === 'medium' ? 7.5 : 15;
  return <svg className="widget-size-glyph" width="22" height="14" viewBox="0 0 22 14" aria-hidden="true">
    <rect x="1.5" y="1.5" width="19" height="11" rx="2.5" fill="none" stroke="currentColor" strokeOpacity=".5" strokeWidth="1.3" />
    <rect x="3.5" y="3.5" width={share} height="7" rx="1.5" fill="currentColor" />
  </svg>;
}

function TileCard({ spec, tools, handle, overlay = false }: { spec: WidgetSpec; tools: Tools; handle: ReactNode; overlay?: boolean }) {
  usePreferences();
  const label = t(spec.label), hidden = tools.container === 'tray';
  const sizes = allowedSizes(spec);
  return <div className={'card widget-tile-card' + (overlay ? ' is-overlay' : '')}>
    <div className="widget-tile-head">
      {handle}
      <span className="widget-tile-icon" aria-hidden="true"><Icon name={spec.icon} size={17} /></span>
      <strong className="widget-tile-title">{label}</strong>
      {tools.reason && <span className="state-pill widget-tile-state">{t("Not shown now")}</span>}
    </div>
    {!hidden && <p className="widget-tile-text">{tools.reason ? t(tools.reason) : spec.description ? t(spec.description) : ' '}</p>}
    <div className="widget-tile-tools">
      {!hidden && sizes.length > 1 && <fieldset className="segmented widget-size" disabled={overlay}>
        <legend className="sr-only">{t("Width of {card}", { card: label })}</legend>
        {sizes.map(size => <label key={size} className="segmented-option" title={t(widgetSizeLabels[size])}>
          <input type="radio" name={'widget-size-' + spec.id + (overlay ? '-overlay' : '')} value={size} checked={tools.size === size}
            tabIndex={overlay ? -1 : undefined} onChange={() => tools.onSize(size)} />
          <span><SizeGlyph size={size} /><span className="sr-only">{t(widgetSizeLabels[size])}</span></span>
        </label>)}
      </fieldset>}
      <span className="widget-tile-moves">
        <button type="button" className="icon-button" data-action="earlier" disabled={overlay || tools.first} tabIndex={overlay ? -1 : undefined}
          aria-label={t("Move {card} earlier", { card: label })} title={t("Move earlier")} onClick={() => tools.onShift(-1)}>
          <Icon name="arrow-up" size={16} /></button>
        <button type="button" className="icon-button" data-action="later" disabled={overlay || tools.last} tabIndex={overlay ? -1 : undefined}
          aria-label={t("Move {card} later", { card: label })} title={t("Move later")} onClick={() => tools.onShift(1)}>
          <Icon name="arrow-down" size={16} /></button>
        {hidden
          ? <button type="button" className="button button-secondary widget-show" data-action="show" disabled={overlay} tabIndex={overlay ? -1 : undefined}
            aria-label={t("Show {card}", { card: label })} onClick={tools.onToggle}><Icon name="eye" size={16} />{t("Show")}</button>
          : <button type="button" className="icon-button" data-action="hide" disabled={overlay} tabIndex={overlay ? -1 : undefined}
            aria-label={t("Hide {card}", { card: label })} title={t("Hide")} onClick={tools.onToggle}><Icon name="eye-off" size={16} /></button>}
      </span>
    </div>
  </div>;
}

function SortableTile({ spec, tools }: { spec: WidgetSpec; tools: Tools }) {
  usePreferences();
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: spec.id, data: { container: tools.container }, attributes: { roleDescription: t("movable card") },
  });
  const { onKeyDown, ...pointer } = listeners ?? {};
  // The card body starts pointer drags; the keyboard uses the handle, so Space on a width option never lifts the card.
  const start = Object.fromEntries(Object.entries(pointer).map(([name, handler]) => [name,
    (event: { target: EventTarget | null }) => { if (!(event.target as Element | null)?.closest?.(interactive)) (handler as (event: unknown) => void)(event); }]));
  const style: CSSProperties = { ...widgetStyle(spec), transform: CSS.Translate.toString(transform), transition };
  return <div ref={setNodeRef} style={style} {...start} className={'widget widget-tile' + (isDragging ? ' is-placeholder' : '')}
    data-widget={spec.id} data-size={tools.container === 'board' ? tools.size : undefined} data-container={tools.container}>
    <TileCard spec={spec} tools={tools} handle={<button ref={setActivatorNodeRef} type="button" className="widget-handle" {...attributes}
      onKeyDown={onKeyDown as KeyboardEventHandler<HTMLButtonElement> | undefined} aria-label={t("Move {card}", { card: t(spec.label) })}><Icon name="grip" size={18} /></button>} />
  </div>;
}

function DropArea({ id, className, empty, children }: { id: string; className: string; empty: string | null; children: ReactNode }) {
  usePreferences();
  const { setNodeRef, isOver } = useDroppable({ id });
  return <div ref={setNodeRef} className={className + (isOver ? ' is-over' : '')} data-drop={id}>
    {children}{empty && <p className="widget-empty">{empty}</p>}
  </div>;
}

/** Arrangement editor: drag (mouse, touch or keyboard), width, visibility, or the step buttons as a single-pointer alternative. */
export default function WidgetEditor({ title, specs, layout, defaults, unavailable, onFinish }: {
  title: string; specs: WidgetSpec[]; layout: WidgetLayout; defaults: WidgetLayout;
  unavailable: Record<string, string>; onFinish: (next: WidgetLayout | null) => void;
}) {
  usePreferences();
  const [draft, setDraft] = useState(layout);
  const [active, setActive] = useState<string | null>(null);
  // `current` is ahead of the render while a drag fires several events; `start` restores a cancelled drag.
  const current = useRef(draft), start = useRef(draft), opened = useRef(layout), moved = useRef(0), finished = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  current.current = draft;
  const byId = useMemo(() => new Map(specs.map(spec => [spec.id, spec])), [specs]);
  const board = boardIds(draft), tray = trayIds(draft);
  const update = (next: WidgetLayout) => { if (!sameLayout(next, current.current)) { current.current = next; setDraft(next); } };
  const name = (id: UniqueIdentifier) => t(byId.get(String(id))?.label ?? String(id));
  const position = (id: UniqueIdentifier) => {
    const items = containerOf(current.current, String(id)) === 'board' ? boardIds(current.current) : trayIds(current.current);
    return { card: name(id), position: items.indexOf(String(id)) + 1, total: items.length };
  };
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 4 } }),
    // A short press tells a drag from a scroll on touch screens.
    useSensor(TouchSensor, { activationConstraint: { delay: 220, tolerance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const [reducedMotion, setReducedMotion] = useState(false);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    setReducedMotion(window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    // Leaving the view keeps what was arranged, as Done would.
    return () => { if (!finished.current && !sameLayout(current.current, opened.current)) onFinish(current.current); };
  }, []);

  const collisions: CollisionDetection = args => {
    const pointerHits = pointerWithin(args), hits = pointerHits.length ? pointerHits : rectIntersection(args);
    const among = (ids: UniqueIdentifier[]) => closestCenter({ ...args, droppableContainers: args.droppableContainers.filter(item => ids.includes(item.id)) });
    const cards = hits.filter(hit => hit.id !== BOARD && hit.id !== TRAY).map(hit => hit.id);
    if (cards.length) return among(cards);
    const area = hits.find(hit => hit.id === BOARD || hit.id === TRAY);
    if (!area) return [];
    // Between cards the nearest card decides, so crossing a gap never sends the card to the end.
    const items = area.id === BOARD ? boardIds(current.current) : trayIds(current.current);
    return items.length ? among(items) : [area];
  };
  const pastMiddle = (dragged: Active, target: Over) => {
    const rect = dragged.rect.current.translated;
    if (!rect) return false;
    const y = rect.top + rect.height / 2, middle = target.rect.top + target.rect.height / 2;
    return Math.abs(y - middle) > target.rect.height / 4 ? y > middle : rect.left + rect.width / 2 > target.rect.left + target.rect.width / 2;
  };
  const dragStart = ({ active: dragged }: DragStartEvent) => { setActive(String(dragged.id)); moved.current = 0; start.current = current.current; };
  const dragOver = ({ active: dragged, over }: DragOverEvent) => {
    if (!over || over.id === dragged.id) return;
    // A reflow moves cards under a still pointer; give the board a moment to settle.
    const now = performance.now();
    if (now - moved.current < 60) return;
    const id = String(dragged.id), target = String(over.id), layout = current.current;
    const container: WidgetContainer = target === BOARD ? 'board' : target === TRAY ? 'tray' : containerOf(layout, target);
    const items = container === 'board' ? boardIds(layout) : trayIds(layout);
    let index: number;
    if (target === BOARD || target === TRAY) index = items.filter(item => item !== id).length;
    else if (containerOf(layout, id) === container) index = items.indexOf(target);
    else index = items.indexOf(target) + (pastMiddle(dragged, over) ? 1 : 0);
    const next = placeWidget(layout, id, container, index);
    if (!sameLayout(next, layout)) { moved.current = now; update(next); }
  };
  const dragEnd = () => setActive(null);
  const dragCancel = () => { setActive(null); update(start.current); };

  const announcements: Announcements = {
    onDragStart: ({ active: dragged }) => t("Picked up {card}.", { card: name(dragged.id) }),
    // Hovering its own place is not news; a move is announced once the board has reflowed.
    onDragOver: ({ active: dragged, over }) => !over || over.id === dragged.id ? undefined : containerOf(current.current, String(dragged.id)) === 'tray'
      ? t("{card} is over the hidden cards.", { card: name(dragged.id) }) : t("{card} is now at position {position} of {total}.", position(dragged.id)),
    onDragEnd: ({ active: dragged }) => containerOf(current.current, String(dragged.id)) === 'tray'
      ? t("{card} is hidden.", { card: name(dragged.id) }) : t("{card} dropped at position {position} of {total}.", position(dragged.id)),
    onDragCancel: ({ active: dragged }) => t("Move cancelled. {card} is back in its place.", { card: name(dragged.id) }),
  };
  const tools = (spec: WidgetSpec, container: WidgetContainer, index: number, count: number): Tools => ({
    size: draft.sizes[spec.id], container, first: index === 0, last: index === count - 1, reason: unavailable[spec.id],
    onSize: size => update(resizeWidget(current.current, spec.id, size)),
    onShift: delta => update(shiftWidget(current.current, spec.id, delta)),
    onToggle: () => update(placeWidget(current.current, spec.id, container === 'board' ? 'tray' : 'board', container === 'board'
      ? trayIds(current.current).length : boardIds(current.current).length)),
  });
  const close = (next: WidgetLayout | null) => { finished.current = true; onFinish(next); };
  const dragged = active ? byId.get(active) : undefined;
  const changed = !sameLayout(draft, layout), isDefault = sameLayout(draft, defaults);

  return <section className="widget-editor" aria-labelledby="widget-editor-title">
    <div className="widget-editor-head">
      <h2 id="widget-editor-title" ref={heading} tabIndex={-1}>{title}</h2>
      <p className="muted">{t("Drag a card to move it (press and hold on touch screens), pick its width or hide it. The arrangement is saved in this browser.")}</p>
    </div>
    <DndContext sensors={sensors} collisionDetection={collisions} measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
      onDragStart={dragStart} onDragOver={dragOver} onDragEnd={dragEnd} onDragCancel={dragCancel}
      accessibility={{ announcements, screenReaderInstructions: { draggable: t("To pick up a card, press Space or Enter. Move it with the arrow keys and press Space or Enter again to drop it, or Escape to cancel.") } }}>
      <SortableContext id={BOARD} items={board} strategy={reflow}>
        <DropArea id={BOARD} className="widget-board is-editing" empty={board.length ? null : t("Drag cards here to show them.")}>
          {board.map((id, index) => <SortableTile key={id} spec={byId.get(id)!} tools={tools(byId.get(id)!, 'board', index, board.length)} />)}
        </DropArea>
      </SortableContext>
      <section className="widget-tray" aria-labelledby="widget-tray-title">
        <h3 id="widget-tray-title">{t("Hidden cards")} <span className="badge metric-badge-neutral">{tray.length}</span></h3>
        <SortableContext id={TRAY} items={tray} strategy={reflow}>
          <DropArea id={TRAY} className="widget-tray-items" empty={tray.length ? null : t("Drag a card here to hide it.")}>
            {tray.map((id, index) => <SortableTile key={id} spec={byId.get(id)!} tools={tools(byId.get(id)!, 'tray', index, tray.length)} />)}
          </DropArea>
        </SortableContext>
      </section>
      <DragOverlay dropAnimation={reducedMotion ? null : undefined} className="widget-overlay">
        {dragged ? <TileCard spec={dragged} overlay tools={tools(dragged, containerOf(draft, dragged.id), -1, 0)}
          handle={<span className="widget-handle" aria-hidden="true"><Icon name="grip" size={18} /></span>} /> : null}
      </DragOverlay>
    </DndContext>
    <div className="save-bar widget-edit-bar" role="region" aria-label={t("Card arrangement")}>
      <div className="save-bar-main">
        <div className="save-status">
          <strong>{t(board.length === 1 ? "{count} card shown" : "{count} cards shown", { count: board.length })}
            {tray.length > 0 && ' · ' + t(tray.length === 1 ? "{count} hidden card" : "{count} hidden cards", { count: tray.length })}</strong>
          <button type="button" id="widget-restore" className="save-diff-toggle" disabled={isDefault} onClick={() => update(defaults)}>{t("Restore the default arrangement")}</button>
        </div>
        <div className="button-row">
          <button type="button" id="widget-cancel" className="button button-ghost" onClick={() => close(null)}>{t("Cancel")}</button>
          <button type="button" id="widget-done" className="button" onClick={() => close(changed ? draft : null)}>
            <Icon name="check" size={16} />{t("Done")}</button>
        </div>
      </div>
    </div>
  </section>;
}
