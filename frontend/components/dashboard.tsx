'use client';

import { ObjectiveControls } from './objective-controls';
import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useSupervisor } from '@/components/supervisor-provider';
import { Icon } from '@/components/icon';
import { Chart } from '@/components/chart';
import type { DataMap } from '@/lib/types';
import { connectivityPresentation } from '@/lib/connectivity';
import { modemHref, statePresentation } from '@/lib/fleet';
import { requestConfigSection } from './configuration';
import { WidgetBoard } from '@/components/widget-board';
import { summaryRowsWithTraffic } from '@/lib/widget-layout';
import type { Widget } from '@/components/widget-board';
import { SIGNALS, age, apnPresentation, date, finite, gpsPresentation, imsiValue, items, metricLabel,
  number, object, radio, ratLabel, rfValue, sampleIsFresh, signalQuality, signalValue, text, trafficPresentation } from '@/lib/metrics';

const detailMemory = new Map<string, boolean>();
const scrollMemory = new Map<string, { top: number; left: number }>();
const selectionWaiters = new Set<() => void>();
function notifySelection() { for (const apply of [...selectionWaiters]) apply(); }
function waitingForSelection(apply: () => void, wait: boolean) {
  if (wait) {
    if (!selectionWaiters.size) document.addEventListener('selectionchange', notifySelection);
    selectionWaiters.add(apply);
  } else {
    selectionWaiters.delete(apply);
    if (!selectionWaiters.size) document.removeEventListener('selectionchange', notifySelection);
  }
}

/** Preserve selected text while other live values continue to update. */
export function StableText({ value, className }: { value: unknown; className?: string }) {
  usePreferences();
  const content = t(text(value)), latest = useRef(content), element = useRef<HTMLSpanElement>(null);
  const [shown, setShown] = useState(content);
  latest.current = content;
  useEffect(() => {
    const apply = () => {
      const selection = window.getSelection();
      let selected = false;
      if (selection && !selection.isCollapsed && element.current) {
        for (let index = 0; index < selection.rangeCount; index++) {
          try { if (selection.getRangeAt(index).intersectsNode(element.current)) { selected = true; break; } } catch { /* Detached range. */ }
        }
      }
      waitingForSelection(apply, selected);
      if (!selected) setShown(latest.current);
    };
    apply();
    return () => waitingForSelection(apply, false);
  }, [content]);
  return <span ref={element} className={className}>{shown}</span>;
}

export function Panel({ title, subtitle, children, action, className = '' }: {
  title: string; subtitle?: string; children: ReactNode; action?: ReactNode; className?: string;
}) {
  usePreferences();
  return <section className={'card panel ' + className}><div className="section-heading"><div>
    <h2>{typeof title === 'string' ? t(title) : title}</h2>{subtitle && <p className="muted">{t(subtitle)}</p>}</div>{action}</div>{children}</section>;
}
export function KeyValues({ pairs }: { pairs: [string, unknown][] }) {
  usePreferences();
  return <dl className="metric-key-values">{pairs.map(([label, value], index) => <div key={index}>
    <dt>{t(label)}</dt><dd><StableText value={value} /></dd></div>)}</dl>;
}
export function PreservedDetails({ id, title, children, initialOpen = false }: {
  id: string; title: ReactNode; children: ReactNode; initialOpen?: boolean;
}) {
  usePreferences();
  const [open, setOpen] = useState(() => detailMemory.get(id) ?? initialOpen);
  return <details className="metric-details" open={open} onToggle={event => {
    const next = event.currentTarget.open; detailMemory.set(id, next); setOpen(next);
  }}><summary>{typeof title === 'string' ? t(title) : title}<Icon name="chevron-down" size={16} /></summary>{children}</details>;
}
export function TableWrap({ id, children }: { id: string; children: ReactNode }) {
  usePreferences();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const saved = scrollMemory.get(id);
    if (saved && ref.current) { ref.current.scrollTop = saved.top; ref.current.scrollLeft = saved.left; }
  }, [id]);
  return <div ref={ref} className="table-wrap metrics-table-wrap" tabIndex={0} onScroll={event => {
    scrollMemory.set(id, { top: event.currentTarget.scrollTop, left: event.currentTarget.scrollLeft });
  }}>{children}</div>;
}
export function DataTable({ id, columns, records, empty = t("Not available in the latest sample.") }: {
  id: string; columns: { label: string; key?: string; value?: (record: DataMap) => unknown }[];
  records: DataMap[]; empty?: string;
}) {
  usePreferences();
  if (!records.length) return <div className="empty-state metric-empty"><Icon name="database" size={23} /><p>{t(empty)}</p></div>;
  return <TableWrap id={id}><table className="metrics-table"><thead><tr>{columns.map(column =>
    <th key={t(column.label)} scope="col">{t(column.label)}</th>)}</tr></thead><tbody>{records.slice(0, 1000).map((record, index) =>
      <tr key={record.key ?? record.name ?? index}>{columns.map(column => <td key={t(column.label)}>
        <StableText value={column.value ? column.value(record) : record[column.key || '']} /></td>)}</tr>)}</tbody></table></TableWrap>;
}
export function ValuesTable({ id, values, units = {} }: { id: string; values: DataMap; units?: DataMap }) {
  usePreferences();
  return <TableWrap id={id}><table className="metrics-table metrics-values-table"><thead><tr>
    <th scope="col">{t("Field")}</th><th scope="col">{t("Value")}</th></tr></thead><tbody>
    {Object.entries(values).map(([key, value]) => <tr key={key}><td><span>{t(metricLabel(key))}</span>
      <code className="metric-field-key">{key}</code></td><td><StableText value={value === null || value === undefined || value === ''
        ? null : text(value) + (units[key] ? ' ' + units[key] : '')} /></td></tr>)}
    {!Object.keys(values).length && <tr><td colSpan={2} className="muted">{t("No data is available.")}</td></tr>}
  </tbody></table></TableWrap>;
}
export function SampleStrip() {
  usePreferences();
  const { snapshot, freshness, connected } = useSupervisor();
  const sample = snapshot?.sample, run = sample?.timing?.run_id || sample?.run_id;
  return <div className="metric-sample-strip"><span><Icon name="activity" size={15} />
    <strong>{sample?.log_id !== undefined ? t("Sample #") + sample.log_id : t("No current sample")}</strong></span>
    <span>{t("Age:")} {age(freshness.sample_age_ms)}</span>
    {(!connected || freshness.state_fresh !== true) && <span className="badge metric-badge-warn">{t("Unconfirmed data")}</span>}
    {run && <span className="metric-run">{t("Run")} <code>{String(run).slice(0, 12)}</code></span>}</div>;
}
export function RadioCells({ compact = false }: { compact?: boolean }) {
  usePreferences();
  const { snapshot, freshness } = useSupervisor();
  const data = radio(snapshot), cells = items(data.cells).slice(0, compact ? 2 : 20);
  return <Panel title={t("Serving cells")} className={freshness.state_fresh === false ? 'metric-stale' : ''}
    action={<span className="badge metric-badge-neutral">{Array.isArray(data.rats) && data.rats.length ? data.rats.map(ratLabel).join(' + ') : t("RAT not available")}</span>}>
    <div className="metric-cell-list">{cells.map((cell, index) => <article key={cell.rat + ':' + index} className="metric-cell">
      <div className="metric-cell-heading"><span className="metric-cell-icon"><Icon name="radio" size={19} /></span>
        <h3>{t("Cell")} {ratLabel(cell.rat)}</h3>{(!snapshot?.sample || !sampleIsFresh(freshness.sample_age_ms)) && <span className="badge metric-badge-warn">{t("Previous observation")}</span>}</div>
      <dl className="metric-cell-grid">{([['PLMN', cell.plmn], ['PCI', cell.pci], ['TAC', cell.tac], [t("Cell ID"), cell.cell_id],
        [cell.channel_type || t("Channel"), cell.channel], [t("Frequency"), finite(cell.frequency_mhz) ? number(cell.frequency_mhz, 3) + ' MHz' : null]] as [string, unknown][])
        .map(([label, value], index) => <div key={index}><dt>{t(label)}</dt><dd><StableText value={value} /></dd>
          {label === t("Frequency") && cell.frequency_label && <small>{t(cell.frequency_label)}</small>}</div>)}</dl>
      {!compact && <p className="metric-observation muted">{[cell.source, date(cell.observed_at)].filter(Boolean).join(' · ')}</p>}
    </article>)}</div>
    {!cells.length && <div className="empty-state metric-empty"><Icon name="radio" size={24} /><p>{t("No cell information is available.")}</p></div>}
    <div className="metric-radio-strips">{(['band', 'bandwidth'] as const).map(kind => {
      const records = items(kind === 'band' ? data.bands : data.bandwidths);
      const visible = compact ? records.filter(record => kind === 'bandwidth' ? finite(record.bandwidth_mhz) : record.band || record.label) : records;
      if (compact && !visible.length) return null;
      return <div key={kind}><span className="metric-label">{kind === 'band' ? t("Observed RF bands") : t("Observed bandwidths")}</span>
        <div className="metric-chips">{visible.length ? visible.map((record, index) => <span className="badge metric-badge-neutral" key={index}>
          {ratLabel(record.rat)} · {rfValue(record, kind)}</span>) : <span className="muted">{t("Not available")}</span>}</div></div>;
    })}</div>
  </Panel>;
}

export function TrafficPanel({ compact = false }: { compact?: boolean }) {
  usePreferences();
  const { snapshot, config, selectedModem } = useSupervisor();
  const traffic = trafficPresentation(snapshot?.sample, config?.telemetry?.enabled !== false && config?.telemetry?.throughput === true);
  const labels: Record<string, string> = { disabled: t("Disabled"), initializing: t("Calculating first interval"), ok: t("Active"), counter_reset: t("Counters reset"), error: t("Not available") };
  return <Panel title={t("WWAN traffic")} subtitle={t("Actual traffic on the modem interface.")}
    action={<span className={'badge ' + (traffic.status === 'ok' ? 'metric-badge-good' : 'metric-badge-neutral')}>{labels[traffic.status] || t("Not available")}</span>}>
    <div className="metric-traffic-values"><div><span><Icon name="arrow-down" size={16} /> {t("RX · Download")}</span>
      <strong><StableText value={number(traffic.rx, 3)} /></strong><small>{traffic.unit}</small></div>
      <div><span><Icon name="arrow-up" size={16} /> {t("TX · Upload")}</span><strong><StableText value={number(traffic.tx, 3)} /></strong><small>{traffic.unit}</small></div></div>
    <KeyValues pairs={compact ? [[t("Interface"), traffic.interface]] : [[t("Interface"), traffic.interface],
      [t("Received"), finite(traffic.rxBytes) ? number(traffic.rxBytes, 0) + ' bytes' : null],
      [t("Sent"), finite(traffic.txBytes) ? number(traffic.txBytes, 0) + ' bytes' : null],
      [t("Observed period"), finite(traffic.interval) ? number(traffic.interval, 3) + ' s' : null], [t("Latest reading"), date(traffic.observed)]]} />
    {!traffic.enabled && <Link className="metric-text-link" href={modemHref('/configuration/modem/', selectedModem, { section: 'capture' })} prefetch={false}
      onClick={() => requestConfigSection('profile', 'capture')}>{t("Enable WWAN traffic")} <Icon name="arrow-up-right" size={15} /></Link>}
    {traffic.error && <p className="metric-inline-error">{traffic.error}</p>}
    {!compact && <PreservedDetails id="traffic-reference" title={t("How traffic is measured")}><div className="metric-explanation"><p>{t("Kibit/s measured between sysfs readings. Initial readings and resets leave gaps; QMI channel rates do not replace actual traffic.")}</p></div></PreservedDetails>}
  </Panel>;
}

export function GPSPanel({ compact = false }: { compact?: boolean }) {
  usePreferences();
  const { snapshot, config, connected, freshness, selectedModem } = useSupervisor();
  const gps = gpsPresentation(snapshot?.sample, config?.telemetry?.enabled !== false && config?.telemetry?.gps === true);
  const status = gps.status === 'fixed' && (!connected || freshness.state_fresh !== true) ? 'stale' : gps.status;
  const labels: Record<string, string> = { disabled: t("Disabled"), starting: t("Starting"), connected: t("Connected, waiting for a fix"),
    unsupported: t("Unsupported"), unavailable: t("Not available"), error: t("Error"), no_fix: t("No fix"), fixed: t("Fix available"), stale: t("Stale fix"), disconnected: t("Disconnected") };
  const pairs: [string, unknown][] = [];
  if (gps.enabled) pairs.push([t("Source"), gps.source === 'qmi_loc' ? t("Modem GPS (QMI)") : gps.source === 'gpsd' ? t("External GPSd") : null]);
  if (status === 'fixed' || status === 'stale') {
    pairs.push([t("Latitude"), finite(gps.latitude) ? String(gps.latitude) : null], [t("Longitude"), finite(gps.longitude) ? String(gps.longitude) : null]);
    if (!compact) pairs.push([t("Altitude ({reference})", { reference: gps.altitudeReference }), finite(gps.altitude) ? String(gps.altitude) + ' m' : null],
      [t("Ellipsoidal altitude"), finite(gps.ellipsoid) ? String(gps.ellipsoid) + ' m' : null],
      [t("Speed"), finite(gps.speed) ? String(gps.speed) + ' m/s' : null], [t("Position timestamp"), gps.timestamp]);
    pairs.push([t("Age"), age(gps.age)]);
    if (!compact && finite(gps.uncertainty)) pairs.push([t("Horizontal uncertainty"), String(gps.uncertainty) + ' m']);
    if (!compact && finite(gps.confidence)) pairs.push([t("Horizontal confidence"), String(gps.confidence) + ' %']);
  }
  return <Panel title="GPS" action={<span className={'badge ' + (status === 'fixed' ? 'metric-badge-good' : status === 'stale' ? 'metric-badge-warn' : 'metric-badge-neutral')}>
    {labels[status] || t("Not available")}</span>}><KeyValues pairs={pairs} />
    {!gps.enabled && <div className="metric-empty-inline"><Icon name="gps" size={23} /><p>{t("Position acquisition is disabled.")}</p>
      <Link href={modemHref('/configuration/modem/', selectedModem, { section: 'gps' })} prefetch={false} className="metric-text-link"
        onClick={() => requestConfigSection('profile', 'gps')}>{t("Configure GPS")} <Icon name="arrow-up-right" size={15} /></Link></div>}
    {gps.enabled && status !== 'fixed' && status !== 'stale' && <p className="muted metric-empty-inline">{t("No valid position is available.")}</p>}
    {gps.error && <p className="metric-inline-error">{gps.error}</p>}</Panel>;
}

function StatCard({ label, value, note, icon, tone = '', className = '' }: {
  label: string; value: unknown; note?: string; icon: string; tone?: string; className?: string;
}) {
  usePreferences();
  return <section className={'card metric-stat ' + tone + ' ' + className}><div className="metric-stat-top"><span className="metric-label">{t(label)}</span>
    <span className="metric-stat-icon"><Icon name={icon} size={19} /></span></div><strong className="metric-stat-value"><StableText value={value} /></strong>
    <p className="muted">{note ? t(note) : '\u00a0'}</p></section>;
}

/** Summary of the selected modem; its identity lives in the page header. */
export function ModemOverview() {
  usePreferences();
  const { snapshot: snapshot, config, connected, freshness, selectedModem } = useSupervisor();
  const snapshotData = snapshot || {}, sample = snapshotData.sample;
  const network = snapshotData.network || {}, device = snapshotData.device || {};
  const fresh = connected && freshness.state_fresh === true;
  const connectivity = connectivityPresentation(network.connectivity);
  const routesConflict = network.connectivity?.routes_conflict;
  const gateway = network.gateway || (Array.isArray(network.route) && network.route.includes('via') ? network.route[network.route.indexOf('via') + 1] : null);
  const connection = object(sample?.collection?.connection || snapshotData.connection), apn = apnPresentation(connection);
  const requests = Object.values(object(sample?.collection?.requests)) as DataMap[];
  const incomplete = sample && requests.length ? requests.filter(record => record.status !== 'ok').length : null;
  const history = Array.isArray(snapshotData.history) ? snapshotData.history : [];
  const error = snapshotData.error || snapshotData.health?.error;
  const trafficEnabled = trafficPresentation(sample, config?.telemetry?.enabled !== false && config?.telemetry?.throughput === true).enabled;
  const gpsEnabled = gpsPresentation(sample, config?.telemetry?.enabled !== false && config?.telemetry?.gps === true).enabled;
  const monitoring = config?.telemetry?.enabled !== false;
  // A single serving cell leaves the traffic panel room beside it; two (EN-DC) need the full width.
  const cellsWithTraffic = trafficEnabled && !gpsEnabled && items(radio(snapshot).cells).length <= 1;
  const off = "Shown while monitoring is on.";
  const widgets: Widget[] = [
    { id: 'status', render: () => <StatCard label={t("Local connection")} icon="activity" value={snapshot ? statePresentation(snapshotData.state).label : t("No data")}
      note={!fresh ? t("Unconfirmed state") : snapshotData.state === 'ONLINE' ? connectivity.summary : t("Recent heartbeat")} tone={fresh && snapshotData.state === 'ONLINE' && connectivity.verified && !network.connectivity?.dns_error && network.connectivity?.route_mode !== 'blocked' ? 'metric-stat-connected' : ''} /> },
    { id: 'address', render: () => <StatCard label={t("IPv4 address")} icon="globe" value={network.address || network.ipv4}
      note={[gateway ? t("Gateway {address}", { address: gateway }) : null, finite(network.route_metric) ? t("Metric ") + network.route_metric : null].filter(Boolean).join(' · ') || t("No configuration published")} /> },
    { id: 'sample', render: () => <StatCard label={t("Latest sample")} icon="clock" value={sample?.log_id !== undefined ? '#' + sample.log_id : null} note={t('Age:') + ' ' + age(freshness.sample_age_ms)} /> },
    { id: 'connection', available: monitoring, unavailable: off, render: () => <Panel title={t("UE and connection")} action={<Icon name="router" size={19} />}><KeyValues pairs={[
      [t("Registration"), sample?.dynamic?.nas_value_serving_system_registration_state], [t("Data session"), sample?.dynamic?.wds_value_connection_status],
      [t("Modem IMEI"), (sample?.static?.dms_value_imei ?? snapshotData.identity?.dms_value_imei)], [t("SIM IMSI"), (imsiValue(sample) ?? snapshotData.identity?.dms_value_imsi)],
    ]} /></Panel> },
    { id: 'network', available: monitoring, unavailable: off, render: () => <Panel title={t("IP network")} action={<Icon name="globe" size={19} />}><KeyValues pairs={[
      [t("Interface"), network.interface || device.interface], [t("Gateway"), gateway], [t("Requested PLMN"), connection.requested_plmn],
      [t("Requested APN"), apn.requested], [t("Effective APN"), apn.reported], [t("APN verification"), apn.verification],
    ]} /></Panel> },
    { id: 'cells', available: monitoring, unavailable: off, render: () => <RadioCells compact /> },
    { id: 'signal', available: monitoring, unavailable: off, render: () => <Panel title={t("Recent signal")} className="metric-signal-panel"
      action={<Link href={modemHref('/metrics/charts/', selectedModem)} prefetch={false} className="metric-text-link">{t("View details")} <Icon name="arrow-up-right" size={16} /></Link>}>
      <div className="metric-signal-cards">{SIGNALS.map(signal => {
        const current = fresh && sampleIsFresh(freshness.sample_age_ms, Number(config?.telemetry?.interval) || 1) ? signalValue(sample?.dynamic, signal.key) : { value: null, rat: null };
        const quality = signalQuality(signal.key, current.value);
        return <article className="metric-signal-card" key={signal.key}>
          <div className="metric-cell-heading"><span className="metric-cell-icon"><Icon name="signal" size={19} /></span><h3>{signal.label}</h3></div>
          <div className="metric-signal-value" style={{ color: quality?.ink }}><StableText value={number(current.value)} /><small>{signal.unit}</small></div>
          <span className="metric-signal-quality">{quality ? `${quality.name} · ${current.rat}` : t("No current sample")}</span>
          <Chart title={signal.label} points={history} series={[{ key: signal.key, label: signal.label, unit: signal.unit }]}
            metric={signal.key} min={signal.min} max={signal.max} interval={finite(config?.telemetry?.interval) ? config.telemetry.interval : 1} compact />
        </article>;
      })}</div>
    </Panel> },
    { id: 'traffic', available: monitoring && trafficEnabled, unavailable: monitoring ? "WWAN traffic measurement is off." : off, render: () => <TrafficPanel compact /> },
    { id: 'gps', available: monitoring && gpsEnabled, unavailable: monitoring ? "Position acquisition is disabled." : off, render: () => <GPSPanel compact /> },
  ];
  return <div className="metrics-page dashboard-page"><ObjectiveControls />
    {config?.telemetry?.enabled === false ? <div className="metric-notice"><Icon name="info" size={18} />{t("Monitoring is disabled. The session can continue without acquiring new samples.")}</div>
      : finite(incomplete) && incomplete > 0 && <div className="metric-notice metric-notice-warning"><Icon name="alert" size={18} />{t("Partial sample:")} {t("{count} queries did not complete. Missing fields remain unavailable.", { count: incomplete })}</div>}
    {snapshotData.state === 'WAIT_REGISTRATION' && <div className="metric-notice"><Icon name="radio" size={18} />{t("Waiting for network registration. Available radio and SIM observations are shown while data activation is pending.")}</div>}
    {error && <div className="metric-notice metric-notice-error" role="status"><Icon name="alert" size={18} /><span>
      {t("This modem could not finish its last step.")}{finite(snapshotData.retry_in_seconds) ? ' ' + t("Retry in {seconds} s", { seconds: Math.ceil(snapshotData.retry_in_seconds) }) + '.' : ''}{' '}
      {/* The technical detail stays in the journal, one click away. */}
      <Link href="/configuration/web/?section=log" prefetch={false} className="metric-notice-link"
        onClick={() => requestConfigSection('web', 'log')}>{t("See the detail in the system log")} <Icon name="arrow-up-right" size={14} /></Link></span></div>}
    {monitoring && apn.warning && <div className={'metric-notice metric-notice-' + apn.tone}><Icon name="info" size={18} />{t(apn.warning)}</div>}
    {/* The rest of the mobile network stays pending until the operator settles the collision. */}
    {routesConflict && <div className="metric-notice metric-notice-warning" role="status"><Icon name="alert" size={18} /><span>
      {t("The rest of the mobile network is not reachable through the modem yet: the automatic route to {network} overlaps {overlaps} on {dev}. Check that conflict, then add the network by hand.",
        { network: routesConflict.network, overlaps: routesConflict.overlaps, dev: routesConflict.dev })}{' '}
      <Link href={modemHref('/configuration/modem/', selectedModem, { section: 'routing' })} prefetch={false} className="metric-notice-link"
        onClick={() => requestConfigSection('profile', 'routing')}>{t("Review the routes")} <Icon name="arrow-up-right" size={14} /></Link></span></div>}
    {network.connectivity?.routes_error && <div className="metric-notice metric-notice-warning" role="status"><Icon name="alert" size={18} /><span>
      {t("Some routes through the modem could not be added.")}{' '}
      <Link href="/configuration/web/?section=log" prefetch={false} className="metric-notice-link"
        onClick={() => requestConfigSection('web', 'log')}>{t("See the detail in the system log")} <Icon name="arrow-up-right" size={14} /></Link></span></div>}
    <WidgetBoard scope="summary" widgets={widgets} defaults={cellsWithTraffic ? summaryRowsWithTraffic : undefined} />
  </div>;
}
