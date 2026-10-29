'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEventHandler, ReactNode, RefObject } from 'react';
import { DndContext, DragOverlay, KeyboardSensor, MeasuringStrategy, MouseSensor, TouchSensor, closestCenter, pointerWithin,
  rectIntersection, useDroppable, useSensor, useSensors } from '@dnd-kit/core';
import type { Active, Announcements, CollisionDetection, DragOverEvent, DragStartEvent, Over, UniqueIdentifier } from '@dnd-kit/core';
import { SortableContext, sortableKeyboardCoordinates, useSortable } from '@dnd-kit/sortable';
import type { SortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { Icon } from './icon';
import { rowStyle, useRowWidth } from './widget-board';
import { TRAY, addRow, boardIds, compactLayout, containerOf, fitColumns, isRowId, itemsOf, moveRow, placeWidget, removeRow, rowColumns,
  sameLayout, setRowColumns, shiftWidget, showWidget, trayIds } from '@/lib/widget-layout';
import type { RowColumns, WidgetLayout, WidgetRow, WidgetSpec } from '@/lib/widget-layout';

/** Cards keep their own place: the rows reflow while dragging instead of stretching cards into each other's slots. */
const reflow: SortingStrategy = () => null;
/** Controls inside a card never start a drag; the handle and the card body do. */
const interactive = 'input, label, a, button:not(.widget-handle)';
/** Narrowest tile before a row of the editor wraps, as rows of the page do. */
const TILE_MIN = 210;
const SLOT = /^slot:(.+):\d+$/;

type Tools = {
  container: string; first: boolean; last: boolean; reason?: string;
  onShift: (delta: -1 | 1) => void; onToggle: () => void;
};

/** A row split into its columns. */
function ColumnsGlyph({ columns }: { columns: RowColumns }) {
  const width = (15 - 1.5 * (columns - 1)) / columns;
  return <svg className="widget-columns-glyph" width="22" height="14" viewBox="0 0 22 14" aria-hidden="true">
    <rect x="1.5" y="1.5" width="19" height="11" rx="2.5" fill="none" stroke="currentColor" strokeOpacity=".5" strokeWidth="1.3" />
    {Array.from({ length: columns }, (_, index) => <rect key={index} x={3.5 + index * (width + 1.5)} y="3.5" width={width} height="7" rx="1" fill="currentColor" />)}
  </svg>;
}
const cardsLabel = (count: number) => t(count === 1 ? "1 card" : "{count} cards", { count });

function TileCard({ spec, tools, handle, overlay = false }: { spec: WidgetSpec; tools: Tools; handle: ReactNode; overlay?: boolean }) {
  usePreferences();
  const label = t(spec.label), hidden = tools.container === TRAY;
  return <div className={'card widget-tile-card' + (overlay ? ' is-overlay' : '')}>
    <div className="widget-tile-head">
      {handle}
      <span className="widget-tile-icon" aria-hidden="true"><Icon name={spec.icon} size={17} /></span>
      <strong className="widget-tile-title">{label}</strong>
      {tools.reason && <span className="state-pill widget-tile-state">{t("Not shown now")}</span>}
    </div>
    {!hidden && <p className="widget-tile-text">{tools.reason ? t(tools.reason) : spec.description ? t(spec.description) : ' '}</p>}
    <div className="widget-tile-tools">
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
  // The card body starts pointer drags; the keyboard uses the handle, so Space on a button never lifts the card.
  const start = Object.fromEntries(Object.entries(pointer).map(([name, handler]) => [name,
    (event: { target: EventTarget | null }) => { if (!(event.target as Element | null)?.closest?.(interactive)) (handler as (event: unknown) => void)(event); }]));
  const style: CSSProperties = { transform: CSS.Translate.toString(transform), transition };
  return <div ref={setNodeRef} style={style} {...start} className={'widget widget-tile' + (isDragging ? ' is-placeholder' : '')}
    data-widget={spec.id} data-container={tools.container === TRAY ? 'tray' : 'board'}>
    <TileCard spec={spec} tools={tools} handle={<button ref={setActivatorNodeRef} type="button" className="widget-handle" {...attributes}
      onKeyDown={onKeyDown as KeyboardEventHandler<HTMLButtonElement> | undefined} aria-label={t("Move {card}", { card: t(spec.label) })}><Icon name="grip" size={18} /></button>} />
  </div>;
}

function DropArea({ id, className, empty, measure, attributes, children }: {
  id: string; className: string; empty: string | null; measure?: RefObject<HTMLDivElement | null>; attributes?: object; children: ReactNode;
}) {
  usePreferences();
  const { setNodeRef, isOver } = useDroppable({ id });
  return <div ref={node => { setNodeRef(node); if (measure) measure.current = node; }} className={className + (isOver ? ' is-over' : '')} data-drop={id} {...attributes}>
    {children}{empty && <p className="widget-empty">{empty}</p>}
  </div>;
}

/** A free place of a row: dropping a card there adds it to the row. */
function Slot({ id }: { id: string }) {
  usePreferences();
  const { setNodeRef, isOver } = useDroppable({ id });
  return <div ref={setNodeRef} className={'widget-slot' + (isOver ? ' is-over' : '')} data-slot={id}><span>{t("Drop a card here")}</span></div>;
}

function RowEditor({ row, index, total, byId, tools, onColumns, onMove, onRemove }: {
  row: WidgetRow; index: number; total: number; byId: Map<string, WidgetSpec>; tools: (spec: WidgetSpec, container: string) => Tools;
  onColumns: (columns: RowColumns) => void; onMove: (delta: -1 | 1) => void; onRemove: () => void;
}) {
  usePreferences();
  const [measure, { width, gap }] = useRowWidth<HTMLDivElement>();
  const number = index + 1, title = t("Row {row}", { row: number });
  return <section className="widget-row-editor" data-row={row.id} aria-label={title}>
    <div className="widget-row-head">
      <strong className="widget-row-title">{title}</strong>
      <fieldset className="segmented widget-columns">
        <legend className="sr-only">{t("Cards in row {row}", { row: number })}</legend>
        {rowColumns.map(columns => <label key={columns} className="segmented-option" title={cardsLabel(columns)}>
          <input type="radio" name={'widget-columns-' + row.id} value={columns} checked={row.columns === columns} onChange={() => onColumns(columns)} />
          <span><ColumnsGlyph columns={columns} /><span className="sr-only">{cardsLabel(columns)}</span></span>
        </label>)}
      </fieldset>
      <span className="widget-row-tools">
        <button type="button" className="icon-button" data-action="row-up" disabled={index === 0}
          aria-label={t("Move row {row} up", { row: number })} title={t("Move up")} onClick={() => onMove(-1)}><Icon name="arrow-up" size={16} /></button>
        <button type="button" className="icon-button" data-action="row-down" disabled={index === total - 1}
          aria-label={t("Move row {row} down", { row: number })} title={t("Move down")} onClick={() => onMove(1)}><Icon name="arrow-down" size={16} /></button>
        <button type="button" className="icon-button" data-action="row-remove"
          aria-label={t("Remove row {row}", { row: number })} title={t("Remove the row; its cards are hidden")} onClick={onRemove}><Icon name="trash" size={16} /></button>
      </span>
    </div>
    <SortableContext id={row.id} items={row.items} strategy={reflow}>
      <DropArea id={row.id} className="widget-row" empty={null} measure={measure} attributes={{ 'data-columns': row.columns, ...rowStyle(fitColumns(row.columns, width, TILE_MIN, gap), row.columns) }}>
        {row.items.map(id => <SortableTile key={id} spec={byId.get(id)!} tools={tools(byId.get(id)!, row.id)} />)}
        {Array.from({ length: row.columns - row.items.length }, (_, slot) => <Slot key={slot} id={'slot:' + row.id + ':' + slot} />)}
      </DropArea>
    </SortableContext>
  </section>;
}

/** Arrangement editor: rows of one to four cards, drag (mouse, touch or keyboard), visibility, or the step buttons as a single-pointer alternative. */
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
    const rows = current.current.rows, row = rows.findIndex(item => item.items.includes(String(id)));
    return { card: name(id), row: row + 1, position: rows[row].items.indexOf(String(id)) + 1, total: rows[row].items.length };
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
    return () => {
      const next = compactLayout(current.current);
      if (!finished.current && !sameLayout(next, opened.current)) onFinish(next);
    };
  }, []);

  const isArea = (id: UniqueIdentifier) => id === TRAY || isRowId(String(id));
  const collisions: CollisionDetection = args => {
    const pointerHits = pointerWithin(args), hits = pointerHits.length ? pointerHits : rectIntersection(args);
    const among = (ids: UniqueIdentifier[]) => closestCenter({ ...args, droppableContainers: args.droppableContainers.filter(item => ids.includes(item.id)) });
    const cards = hits.filter(hit => !isArea(hit.id) && !SLOT.test(String(hit.id))).map(hit => hit.id);
    if (cards.length) return among(cards);
    const slot = hits.find(hit => SLOT.test(String(hit.id)));
    if (slot) return [slot];
    const area = hits.find(hit => isArea(hit.id));
    if (!area) return [];
    // Between cards the nearest card decides, so crossing a gap never sends the card to the end.
    const items = itemsOf(current.current, String(area.id));
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
    // A reflow moves cards under a still pointer; give the rows a moment to settle.
    const now = performance.now();
    if (now - moved.current < 60) return;
    const id = String(dragged.id), target = String(over.id), layout = current.current, slot = SLOT.exec(target);
    const container = slot ? slot[1] : isArea(target) ? target : containerOf(layout, target);
    if (!container) return;
    const items = itemsOf(layout, container), row = layout.rows.find(item => item.id === container);
    const full = !!row && items.filter(item => item !== id).length >= row.columns;
    let index: number;
    // A free place, an empty row or the tray itself: the end. A card of a full row trades places with the dragged one.
    if (slot || container === target) index = items.filter(item => item !== id).length;
    else if (containerOf(layout, id) === container || full) index = items.indexOf(target);
    else index = items.indexOf(target) + (pastMiddle(dragged, over) ? 1 : 0);
    const next = placeWidget(layout, id, container, index);
    if (!sameLayout(next, layout)) { moved.current = now; update(next); }
  };
  const dragEnd = () => setActive(null);
  const dragCancel = () => { setActive(null); update(start.current); };

  const announcements: Announcements = {
    onDragStart: ({ active: dragged }) => t("Picked up {card}.", { card: name(dragged.id) }),
    // Hovering its own place is not news; a move is announced once the rows have reflowed.
    onDragOver: ({ active: dragged, over }) => !over || over.id === dragged.id ? undefined : containerOf(current.current, String(dragged.id)) === TRAY
      ? t("{card} is over the hidden cards.", { card: name(dragged.id) }) : t("{card} is now in row {row}, position {position} of {total}.", position(dragged.id)),
    onDragEnd: ({ active: dragged }) => containerOf(current.current, String(dragged.id)) === TRAY
      ? t("{card} is hidden.", { card: name(dragged.id) }) : t("{card} dropped in row {row}, position {position} of {total}.", position(dragged.id)),
    onDragCancel: ({ active: dragged }) => t("Move cancelled. {card} is back in its place.", { card: name(dragged.id) }),
  };
  const tools = (spec: WidgetSpec, container: string): Tools => {
    const items = container === TRAY ? tray : board, index = items.indexOf(spec.id);
    return {
      container, first: index === 0, last: index === items.length - 1, reason: unavailable[spec.id],
      onShift: delta => update(shiftWidget(current.current, spec.id, delta)),
      onToggle: () => update(container === TRAY ? showWidget(current.current, spec.id) : placeWidget(current.current, spec.id, TRAY, trayIds(current.current).length)),
    };
  };
  // A row button that moves or disappears hands the focus to its row's new place, or to the next row.
  const refocus = (selectors: string[]) => window.requestAnimationFrame(() => {
    for (const selector of selectors) {
      const target = document.querySelector<HTMLElement>('.widget-editor ' + selector);
      if (target && !(target as HTMLButtonElement).disabled) { target.focus(); return; }
    }
    heading.current?.focus();
  });
  const rowMove = (row: WidgetRow, delta: -1 | 1) => {
    update(moveRow(current.current, row.id, delta));
    refocus([`[data-row="${row.id}"] [data-action=row-${delta < 0 ? 'up' : 'down'}]`, `[data-row="${row.id}"] [data-action=row-${delta < 0 ? 'down' : 'up'}]`]);
  };
  const rowRemove = (row: WidgetRow, index: number) => {
    const rows = current.current.rows, neighbour = rows[index + 1] ?? rows[index - 1];
    update(removeRow(current.current, row.id));
    refocus(neighbour ? [`[data-row="${neighbour.id}"] [data-action=row-remove]`] : ['.widget-add-row button']);
  };
  const close = (next: WidgetLayout | null) => { finished.current = true; onFinish(next); };
  const dragged = active ? byId.get(active) : undefined;
  const compact = compactLayout(draft), changed = !sameLayout(compact, layout), isDefault = sameLayout(compact, defaults);

  return <section className="widget-editor" aria-labelledby="widget-editor-title">
    <div className="widget-editor-head">
      <h2 id="widget-editor-title" ref={heading} tabIndex={-1}>{title}</h2>
      <p className="muted">{t("Choose how many cards each row holds, then drag the cards into place (press and hold on touch screens) or hide them. The cards of a row share its width. The arrangement is saved in this browser.")}</p>
    </div>
    <DndContext sensors={sensors} collisionDetection={collisions} measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
      onDragStart={dragStart} onDragOver={dragOver} onDragEnd={dragEnd} onDragCancel={dragCancel}
      accessibility={{ announcements, screenReaderInstructions: { draggable: t("To pick up a card, press Space or Enter. Move it with the arrow keys and press Space or Enter again to drop it, or Escape to cancel.") } }}>
      <div className="widget-rows">
        {draft.rows.map((row, index) => <RowEditor key={row.id} row={row} index={index} total={draft.rows.length} byId={byId} tools={tools}
          onColumns={columns => update(setRowColumns(current.current, row.id, columns))} onMove={delta => rowMove(row, delta)} onRemove={() => rowRemove(row, index)} />)}
        {!draft.rows.length && <p className="widget-empty">{t("Add a row to show cards.")}</p>}
      </div>
      <div className="widget-add-row" role="group" aria-labelledby="widget-add-row-label">
        <span id="widget-add-row-label">{t("Add a row")}</span>
        {rowColumns.map(columns => <button key={columns} type="button" className="button button-secondary" data-columns={columns}
          aria-label={t(columns === 1 ? "Add a row of 1 card" : "Add a row of {count} cards", { count: columns })} title={cardsLabel(columns)}
          onClick={() => update(addRow(current.current, columns))}><ColumnsGlyph columns={columns} /></button>)}
      </div>
      <section className="widget-tray" aria-labelledby="widget-tray-title">
        <h3 id="widget-tray-title">{t("Hidden cards")} <span className="badge metric-badge-neutral">{tray.length}</span></h3>
        <SortableContext id={TRAY} items={tray} strategy={reflow}>
          <DropArea id={TRAY} className="widget-tray-items" empty={tray.length ? null : t("Drag a card here to hide it.")}>
            {tray.map(id => <SortableTile key={id} spec={byId.get(id)!} tools={tools(byId.get(id)!, TRAY)} />)}
          </DropArea>
        </SortableContext>
      </section>
      <DragOverlay dropAnimation={reducedMotion ? null : undefined} className="widget-overlay">
        {dragged ? <TileCard spec={dragged} overlay tools={tools(dragged, containerOf(draft, dragged.id) ?? TRAY)}
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
          <button type="button" id="widget-done" className="button" onClick={() => close(changed ? compact : null)}>
            <Icon name="check" size={16} />{t("Done")}</button>
        </div>
      </div>
    </div>
  </section>;
}
