'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import type { MetricPoint } from '@/lib/types';
import { SIGNAL_LEVELS, SIGNAL_LIMITS, clockLabel, cssColor, finite, lineConnects, seriesColor,
  nearestPoint, number, orderedPoints, pointKey, pointTime, signalQuality, signalSegments, valueScale } from '@/lib/metrics';
import type { ChartSeries, SignalMetric } from '@/lib/metrics';

type ChartProps = {
  title: string;
  points: MetricPoint[];
  series: ChartSeries[];
  metric?: SignalMetric;
  unit?: string;
  interval?: number;
  min?: number;
  max?: number;
  compact?: boolean;
};

export function SignalLegend({ metric }: { metric: SignalMetric }) {
  usePreferences();
  const [low, middle, high] = SIGNAL_LIMITS[metric];
  const ranges = [`≥ ${high}`, t('≥ {minimum} and < {maximum}', { minimum: middle, maximum: high }),
    t('> {minimum} and < {maximum}', { minimum: low, maximum: middle }), `≤ ${low}`];
  return <div className="signal-legend" aria-label={t("Signal quality reference")}>
    {SIGNAL_LEVELS.map((level, index) => <span key={t(level.name)}><i style={{ background: level.color }} />
      <span>{t(level.name)} <small>{ranges[index]} {metric === 'rsrp' ? 'dBm' : 'dB'}</small></span></span>)}
  </div>;
}

export function Chart({ title, points, series, metric, unit, interval = 1, min, max, compact = false }: ChartProps) {
  usePreferences();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [frame, setFrame] = useState({ width: 500, height: compact ? 112 : 240 });
  const [cursor, setCursor] = useState<{ time: number; key?: string; pointer?: boolean } | null>(null);
  const tooltipId = useId();
  const ordered = useMemo(() => orderedPoints(points || []), [points]);
  const values = useMemo(() => ordered.flatMap(point => series.map(record => point[record.key]).filter(finite)), [ordered, series]);
  const scale = useMemo(() => valueScale(values, { metric, min, max }), [values, metric, min, max]);
  const first = ordered.length ? pointTime(ordered[0]) : 0;
  const last = ordered.length ? pointTime(ordered[ordered.length - 1]) : first;
  const plot = { left: compact ? 43 : 51, right: frame.width - 12, top: 23, bottom: frame.height - 25,
    span: Math.max(1000, last - first), first, last };
  const x = (time: number) => plot.left + (time - first) / plot.span * (plot.right - plot.left);
  const y = (value: number) => plot.bottom - (value - scale.min) / (scale.max - scale.min) * (plot.bottom - plot.top);
  const selected = useMemo(() => {
    if (!cursor) return null;
    const index = cursor.key ? ordered.findIndex(point => pointKey(point) === cursor.key) : -1;
    if (index >= 0) return { point: ordered[index], index, time: pointTime(ordered[index]), gap: false };
    return nearestPoint(ordered, cursor.time, series, interval);
  }, [cursor, ordered, series, interval]);
  const seriesUnit = (record: ChartSeries) => record.unit || unit || (metric === 'rsrp' ? 'dBm' : metric ? 'dB' : '');
  const unitLabel = [...new Set(series.map(seriesUnit).filter(Boolean))].join(' / ');

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const measure = () => {
      const width = Math.max(160, canvas.clientWidth || 500), height = compact ? 112 : 240;
      setFrame(previous => previous.width === width && previous.height === height ? previous : { width, height });
    };
    measure();
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(measure);
      observer.observe(canvas);
      return () => observer.disconnect();
    }
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [compact]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const draw = () => {
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      if (canvas.width !== Math.round(frame.width * ratio)) canvas.width = Math.round(frame.width * ratio);
      if (canvas.height !== Math.round(frame.height * ratio)) canvas.height = Math.round(frame.height * ratio);
      const context = canvas.getContext('2d');
      const styles = getComputedStyle(document.documentElement);
      const palette = (name: string) => styles.getPropertyValue(name).trim();
      const ink = (color: string) => color.startsWith('--') ? palette(color) : color;
      if (!context) return;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, frame.width, frame.height);
      context.font = '10px system-ui, sans-serif';
      context.lineWidth = 1;
      const tickCount = compact ? 3 : 5, span = scale.max - scale.min;
      const digits = span >= 100 ? 0 : span >= 10 ? 1 : 2;
      for (let index = 0; index < tickCount; index++) {
        const value = scale.min + span * index / (tickCount - 1), position = y(value);
        context.strokeStyle = palette('--chart-grid'); context.beginPath();
        context.moveTo(plot.left, position); context.lineTo(plot.right, position); context.stroke();
        context.fillStyle = palette('--chart-label'); context.textAlign = 'right';
        context.fillText(number(value, digits), plot.left - 7, position + 3);
      }
      context.fillStyle = palette('--chart-label'); context.textAlign = 'left'; context.fillText(unitLabel || t("Value"), 3, 11);
      if (!values.length) {
        context.textAlign = 'center'; context.fillStyle = palette('--chart-label');
        context.fillText(t("No data available"), (plot.left + plot.right) / 2, (plot.top + plot.bottom) / 2);
      } else {
        context.lineWidth = compact ? 1.7 : 2; context.lineCap = 'round'; context.lineJoin = 'round';
        series.forEach((record, index) => {
          let previous: { point: MetricPoint; x: number; y: number } | null = null;
          const color = ink(seriesColor(record, index));
          for (const point of ordered) {
            const value = point[record.key];
            if (!finite(value)) { previous = null; continue; }
            const current = { point, x: x(pointTime(point)), y: y(value) };
            if (previous && lineConnects(previous.point, point, record.key, interval)) {
              const pieces = metric ? signalSegments(metric, previous.point[record.key], value) : [{ from: 0, to: 1, color }];
              for (const piece of pieces) {
                context.strokeStyle = piece.color; context.beginPath();
                context.moveTo(previous.x + (current.x - previous.x) * piece.from, previous.y + (current.y - previous.y) * piece.from);
                context.lineTo(previous.x + (current.x - previous.x) * piece.to, previous.y + (current.y - previous.y) * piece.to);
                context.stroke();
              }
            } else {
              context.fillStyle = metric ? signalQuality(metric, value)!.color : color;
              context.beginPath(); context.arc(current.x, current.y, compact ? 1.7 : 2, 0, Math.PI * 2); context.fill();
            }
            previous = current;
          }
        });
      }
      if (ordered.length) {
        context.fillStyle = palette('--chart-label'); context.textAlign = 'left';
        context.fillText(clockLabel(ordered[0]), plot.left, frame.height - 5);
        context.textAlign = 'right'; context.fillText(clockLabel(ordered[ordered.length - 1]), plot.right, frame.height - 5);
      }
      if (selected) {
        const position = x(selected.time);
        context.strokeStyle = palette('--chart-cursor'); context.lineWidth = 1; context.setLineDash([3, 3]); context.beginPath();
        context.moveTo(position, plot.top); context.lineTo(position, plot.bottom); context.stroke(); context.setLineDash([]);
        for (const [index, record] of series.entries()) {
          const value = selected.point?.[record.key];
          if (!finite(value)) continue;
          context.fillStyle = metric ? signalQuality(metric, value)!.color : ink(seriesColor(record, index));
          context.strokeStyle = palette('--surface'); context.lineWidth = 2; context.beginPath();
          context.arc(position, y(value), 4, 0, Math.PI * 2); context.fill(); context.stroke();
        }
      }
    };
    const request = window.requestAnimationFrame(draw);
    return () => window.cancelAnimationFrame(request);
  });

  function selectPointer(event: PointerEvent<HTMLCanvasElement>) {
    if (!ordered.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const pixel = (event.clientX - rect.left) * frame.width / (rect.width || frame.width);
    const time = first + Math.max(0, Math.min(1, (pixel - plot.left) / (plot.right - plot.left))) * plot.span;
    setCursor({ time, pointer: true });
  }
  function selectIndex(index: number) {
    if (!ordered.length) return;
    const point = ordered[Math.max(0, Math.min(index, ordered.length - 1))];
    setCursor({ time: pointTime(point), key: pointKey(point) });
  }
  function keyDown(event: KeyboardEvent<HTMLCanvasElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Escape') { setCursor(null); return; }
    const previous = selected?.index ?? ordered.length - 1;
    selectIndex(event.key === 'Home' ? 0 : event.key === 'End' ? ordered.length - 1 : previous + (event.key === 'ArrowLeft' ? -1 : 1));
  }
  const tooltipLeft = selected ? Math.max(6, Math.min(x(selected.time) - 75, frame.width - 210)) : 0;
  return <div className={'metric-chart' + (compact ? ' metric-chart-compact' : '')}>
    <div className="metric-chart-shell">
      <canvas ref={canvasRef} className="metric-chart-canvas" tabIndex={0} role="img"
        aria-label={t("{title}. Use Left/Right, Home and End to inspect samples. Escape closes the tooltip.", { title: t(title) })}
        aria-describedby={tooltipId} aria-keyshortcuts="ArrowLeft ArrowRight Home End Escape"
        onPointerMove={selectPointer} onPointerDown={event => { event.currentTarget.focus({ preventScroll: true }); selectPointer(event); }}
        onPointerLeave={() => { if (cursor?.pointer && document.activeElement !== canvasRef.current) setCursor(null); }}
        onFocus={() => { if (!cursor) selectIndex(ordered.length - 1); }} onBlur={event => { if (!(event.relatedTarget instanceof Element && event.relatedTarget.closest('.preference-controls'))) setCursor(null); }} onKeyDown={keyDown} />
      <div id={tooltipId} className="chart-tooltip metric-chart-tooltip" role="status" aria-live="polite" hidden={!selected}
        style={{ left: tooltipLeft, top: plot.top + 7 }}>
        {selected && <><strong>{selected.gap ? t("No sample in this interval") : clockLabel(selected.point)}</strong>
          {series.map((record, index) => {
            const value = selected.point?.[record.key], rat = selected.point?.[record.ratKey || record.key + '_rat'];
            return <span key={record.key}><i style={{ background: cssColor(seriesColor(record, index)) }} />
              {t(record.label)}: <b>{number(value, 2)}{finite(value) && seriesUnit(record) ? ' ' + seriesUnit(record) : ''}</b>{rat ? ' · ' + rat : ''}</span>;
          })}</>}
      </div>
    </div>
    {!compact && <div className="chart-caption"><div className="chart-series-legend">
      {series.map((record, index) => <span key={record.key}><i style={{ background: cssColor(seriesColor(record, index)) }} />{t(record.label)}{seriesUnit(record) ? ` · ${seriesUnit(record)}` : ''}</span>)}
    </div><span className="muted">{scale.observedMin === null ? t("Waiting for data") : t("Observed: {min} to {max}", { min: number(scale.observedMin, 2), max: number(scale.observedMax, 2) })}</span></div>}
  </div>;
}

export function ChartPanel({ title, current, subtitle, note, currentColor, ...props }: ChartProps & {
  current?: string; subtitle?: string; note?: string; currentColor?: string;
}) {
  usePreferences();
  return <section className="card panel chart-panel"><div className="section-heading"><div>
    <h2>{t(title)}</h2>{subtitle && <p className="muted">{t(subtitle)}</p>}</div>
    {current && <strong className="chart-current" style={{ color: currentColor }}>{current}</strong>}</div>
    <Chart title={t(title)} {...props} />
    {props.metric && <SignalLegend metric={props.metric} />}
    {note && <p className="chart-note muted">{t(note)}</p>}
  </section>;
}
