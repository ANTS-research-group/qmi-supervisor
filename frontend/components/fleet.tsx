'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import type { MouseEvent } from 'react';
import { useRef } from 'react';
import { useSupervisor } from './supervisor-provider';
import type { Objective } from './supervisor-provider';
import { Icon } from './icon';
import { Switch } from './form-controls';
import { requestConfigSection } from './configuration';
import { connectivityPresentation } from '@/lib/connectivity';
import { scopedErrors } from '@/lib/config';
import { exitOrder, fleetEntries, fleetSummary, modemHref, modemName, modemSerialLine, modemTab, modemTabs, pageKind, showsRouteRole,
  statePresentation } from '@/lib/fleet';
import { SIGNAL_LIMITS, finite, number, signalQuality } from '@/lib/metrics';
import type { ModemRecord } from '@/lib/types';

/** Inventory entries, or the default modem of a backend without fleet inventory. */
export function useFleet() {
  const { modems, snapshot, config, selectedModem } = useSupervisor();
  return fleetEntries(modems, snapshot, config, selectedModem);
}

const normalized = (pathname: string) => pathname.endsWith('/') ? pathname : pathname + '/';
const shortSerial = (serial: string) => serial.length > 12 ? serial.slice(0, 4) + '…' + serial.slice(-4) : serial;

export function StatePill({ state, detected = true }: { state: unknown; detected?: boolean }) {
  usePreferences();
  const presentation = statePresentation(state, detected);
  return <span className={'state-pill tone-' + presentation.tone}><span className="state-dot" aria-hidden="true" />{presentation.label}</span>;
}

export function SignalBars({ rsrp }: { rsrp: unknown }) {
  usePreferences();
  const quality = signalQuality('rsrp', rsrp), [low, middle, high] = SIGNAL_LIMITS.rsrp;
  const level = !finite(rsrp) ? 0 : rsrp >= high ? 4 : rsrp >= middle ? 3 : rsrp > low ? 2 : 1;
  return <svg className="signal-bars" width="22" height="16" viewBox="0 0 22 16" role="img"
    aria-label={quality ? t("Signal: {quality}", { quality: quality.name }) : t("No signal data")}>
    {[0, 1, 2, 3].map(index => <rect key={index} x={index * 6} y={12 - index * 4} width="4" height={4 + index * 4} rx="1"
      style={{ fill: index < level ? quality?.ink : 'var(--track)' }} />)}
  </svg>;
}

/** Busy writes would ignore a modem change, so links wait instead of silently doing nothing. */
function useOpenModem() {
  const { selectModem, prepareModem, selectedModem, saving, objectiveBusy } = useSupervisor();
  const router = useRouter();
  const sequence = useRef(0);
  const busy = saving || objectiveBusy !== null;
  return { busy, open: (event: MouseEvent<HTMLAnchorElement>, id: string) => {
    if (busy) { event.preventDefault(); return false; }
    // The same modem only changes page; a different one loads before the route does,
    // so the target page never renders its empty state first.
    if (id === selectedModem) return true;
    const href = event.currentTarget?.getAttribute('href');
    if (!href) { selectModem(id); return true; }
    event.preventDefault();
    const ticket = ++sequence.current;
    void prepareModem(id).catch(() => undefined).finally(() => {
      // A later click owns the navigation; this one must not move the page back.
      if (ticket !== sequence.current) return;
      selectModem(id);
      router.push(href);
    });
    return true;
  } };
}

function FleetRow({ entry }: { entry: ModemRecord }) {
  usePreferences();
  const { selectedModem, config, editable, objectiveOverrides, setObjective } = useSupervisor();
  const { busy, open } = useOpenModem();
  const enabled = (name: Objective) => objectiveOverrides[entry.id + ':' + name]?.enabled
    ?? (entry.id === selectedModem && config
      ? name === 'connection' ? config.modem?.mode === 'connect' : config.telemetry?.enabled === true
      : name === 'connection' ? entry.connection_enabled : entry.monitoring_enabled);
  const connectivity = connectivityPresentation(entry.network?.connectivity);
  const signal = entry.detected ? entry.signal : null;
  const address = typeof entry.network?.address === 'string' ? entry.network.address.split('/')[0] : null;
  const serial = entry.selector?.serial || entry.device?.serial || '';
  const radio = [signal?.operator, [signal?.rat, signal?.band].filter(Boolean).join(' ')].filter(Boolean).join(' · ');
  const link = [entry.apn ? 'APN ' + entry.apn : null, address].filter(Boolean).join(' · ');
  const error = entry.error ? String(entry.error).split('\n')[0] : null;
  const pending = Object.values(entry.features ?? {}).some(feature => feature?.pending === true);
  const href = modemHref('/modem/', entry.id);
  return <li className={'fleet-row' + (entry.detected ? '' : ' is-undetected')}>
    <div className="fleet-cell fleet-identity">
      <span className="fleet-device" aria-hidden="true"><Icon name="modem" size={20} /></span>
      <div className="fleet-name">
        <span className="fleet-title"><Link href={href} prefetch={false} className="fleet-link" title={entry.id} aria-disabled={busy || undefined}
          onClick={event => open(event, entry.id)}>{modemName(entry)}</Link>
          {showsRouteRole(entry) && connectivity.route && <span className="role-pill">{connectivity.route}</span>}</span>
        <span className="fleet-sub">{entry.detected ? modemSerialLine(entry) || t("Detection pending")
          : serial ? t("Serial {serial}", { serial: shortSerial(serial) }) : t("No hardware assigned")}</span>
      </div>
    </div>
    <div className="fleet-cell fleet-state"><span className="fleet-label">{t("State")}</span>
      <StatePill state={entry.state} detected={entry.detected} />
      <span className="fleet-sub">{error ? t(error) : pending ? t("Applying changes") : entry.detected && entry.connection_enabled ? connectivity.label : ''}</span>
    </div>
    <div className="fleet-cell fleet-network"><span className="fleet-label">{t("Network")}</span>
      {entry.detected ? <><span className="fleet-main">{radio || link || '—'}</span>{radio && link && <span className="fleet-sub mono">{link}</span>}</>
        : <span className="fleet-sub">{t("Profile kept; it resumes when the modem is connected.")}{entry.cleanup_pending ? ' ' + t("Cleanup pending.") : ''}</span>}
    </div>
    <div className="fleet-cell fleet-signal"><span className="fleet-label">{t("Signal")}</span>
      {finite(signal?.rsrp) ? <div className="fleet-signal-value"><SignalBars rsrp={signal.rsrp} />
        <span><span className="fleet-main"><span className="mono">{number(signal.rsrp, 0)}</span> dBm</span>
          {finite(signal?.snr) && <span className="fleet-sub">SNR {number(signal.snr, 0)} dB</span>}</span></div>
        : <span className="muted">—</span>}
    </div>
    <div className="fleet-cell fleet-objectives">
      {(['connection', 'telemetry'] as const).map(name => <label key={name} className="fleet-switch">
        <Switch id={'fleet-' + entry.id + '-' + name} checked={enabled(name)} disabled={!editable || busy || !entry.configured}
          aria-label={t(name === 'connection' ? "Connection of {modem}" : "Telemetry of {modem}", { modem: modemName(entry) })}
          onChange={event => void setObjective(name, event.currentTarget.checked, entry.id)} />
        <span aria-hidden="true">{t(name === 'connection' ? "Connection" : "Telemetry")}</span></label>)}
    </div>
    <Link href={href} prefetch={false} className="icon-button fleet-open" aria-label={t("Open {modem}", { modem: modemName(entry) })}
      aria-disabled={busy || undefined} onClick={event => open(event, entry.id)}><Icon name="chevron-right" size={18} /></Link>
  </li>;
}

function ExitOrder({ order, entries }: { order: ReturnType<typeof exitOrder>; entries: ModemRecord[] }) {
  usePreferences();
  const { open } = useOpenModem();
  const byId = new Map(entries.map(entry => [entry.id, entry]));
  return <section className="card exit-order" aria-labelledby="exit-order-title">
    <h2 id="exit-order-title">{t("Internet exit order")}</h2>
    <p className="muted">{t("The default route with the lowest metric carries the traffic. Change it in Routing and DNS of each modem.")}</p>
    <ol>{order.map(item => {
      const entry = byId.get(item.id);
      return <li key={item.id}>
        <span className="exit-position" aria-hidden="true">{item.position}</span>
        <Link href={modemHref('/configuration/modem/', item.id, { section: 'routing' })} prefetch={false} title={item.id}
          onClick={event => { if (open(event, item.id)) requestConfigSection('profile', 'routing'); }}>
          <strong>{entry ? modemName(entry) : item.id}</strong>
          <span className="muted"> {(entry ? modemSerialLine(entry) : '') || item.interface || '—'}</span></Link>
        <span className="mono muted exit-metric">{t("metric {metric}", { metric: item.metric })}</span>
        <span className={'state-pill tone-' + (item.active ? 'good' : 'neutral')}>{t(item.active ? "Active" : "Standby")}</span>
      </li>;
    })}</ol>
  </section>;
}

export function Fleet() {
  usePreferences();
  const entries = useFleet(), order = exitOrder(entries);
  return <div className="fleet-page">
    <section className="card fleet-table" aria-label={t("Modems")}>
      <div className="fleet-head" aria-hidden="true"><span>{t("Modem")}</span><span>{t("State")}</span><span>{t("Network")}</span>
        <span>{t("Signal")}</span><span>{t("Objectives")}</span><span /></div>
      <ul className="fleet-rows">{entries.map(entry => <FleetRow key={entry.id} entry={entry} />)}</ul>
    </section>
    {order.length >= 2 && <ExitOrder order={order} entries={entries} />}
    <p className="fleet-note"><Icon name="info" size={15} />
      <span>{t("New modems copy the default configuration; afterwards each one is independent. Web access is shared by all.")}</span></p>
  </div>;
}

export function FleetHeading() {
  usePreferences();
  const { refresh } = useSupervisor();
  const summary = fleetSummary(useFleet());
  return <div className="page-heading"><div><h1>{t("Fleet")}</h1>
    <p>{[t("{count} online", { count: summary.online }), summary.undetected ? t("{count} not detected", { count: summary.undetected }) : null].filter(Boolean).join(' · ')}</p></div>
    <div className="page-heading-actions"><button className="icon-button refresh-button" title={t("Refresh data")} aria-label={t("Refresh data")}
      onClick={() => void refresh()}><Icon name="refresh" size={17} /></button></div></div>;
}

/** Sidebar list: the modem is the context of every modem page. */
export function ModemList({ onNavigate }: { onNavigate: (event: MouseEvent<HTMLAnchorElement>) => void }) {
  usePreferences();
  const pathname = normalized(usePathname());
  const { selectedModem, pendingByModem } = useSupervisor();
  const { busy, open } = useOpenModem();
  const entries = useFleet();
  if (!entries.length) return null;
  const kind = pageKind(pathname, entries.length >= 2);
  const path = kind === 'modem' && pathname !== '/' && pathname !== '/dashboard/' ? pathname : '/modem/';
  return <nav id="modem-list" className="modem-list" aria-labelledby="modem-list-title">
    <div className="nav-label" id="modem-list-title"><span>{t("MODEMS")}</span><span className="nav-count">{entries.length}</span></div>
    {entries.map(entry => {
      const active = kind === 'modem' && entry.id === selectedModem, presentation = statePresentation(entry.state, entry.detected);
      return <Link key={entry.id} href={modemHref(path, entry.id)} prefetch={false} data-modem={entry.id}
        className={'nav-link modem-link' + (active ? ' active' : '')} aria-current={active ? 'page' : undefined} aria-disabled={busy || undefined}
        onClick={event => { if (open(event, entry.id)) onNavigate(event); }}>
        <span className={'modem-dot tone-' + presentation.tone + (entry.detected ? '' : ' is-hollow')} aria-hidden="true" />
        <span className="modem-link-name" title={entry.id}><strong>{modemName(entry)}</strong><span>{modemSerialLine(entry) || '—'}</span></span>
        <span className="sr-only">{presentation.label}</span>
        {pendingByModem[entry.id] > 0 ? <span className="nav-dirty" title={t("Unsaved changes")} aria-label={t("Unsaved changes")} /> : null}
      </Link>;
    })}
  </nav>;
}

/** Identity, state and views of the selected modem. */
export function ModemHeader() {
  usePreferences();
  const pathname = normalized(usePathname());
  const { selectedModem, snapshot, refresh, errors, pendingByModem } = useSupervisor();
  const entries = useFleet();
  const entry = entries.find(item => item.id === selectedModem) ?? entries[0];
  const device = entry?.device ?? snapshot?.device ?? null, detected = entry?.detected ?? Boolean(device);
  const serial = String(device?.serial || entry?.selector?.serial || '');
  const tab = pathname === '/' || pathname === '/dashboard/' ? 'resumen' : modemTab(pathname);
  const profileErrors = Object.keys(scopedErrors(errors, 'profile')).length;
  return <>
    <div className="page-heading modem-heading">
      <div className="modem-heading-main">
        <span className="modem-heading-icon" aria-hidden="true"><Icon name="modem" size={24} /></span>
        <div className="modem-heading-copy">
          <h1><span className="modem-heading-id">{entry ? modemName(entry) : selectedModem}</span><StatePill state={snapshot?.state ?? entry?.state} detected={detected} /></h1>
          <p className="modem-heading-device">
            {serial && <span title={serial}>{t("Serial {serial}", { serial: shortSerial(serial) })}</span>}
            {detected && device?.path && <span className="mono">{device.path}</span>}
            {detected && (device?.interface || entry?.interface) && <span className="mono">{device?.interface || entry?.interface}</span>}
            {!detected && <span>{t("Not detected")}</span>}
          </p>
        </div>
      </div>
      <div className="page-heading-actions"><button className="icon-button refresh-button" title={t("Refresh data")} aria-label={t("Refresh data")}
        onClick={() => void refresh()}><Icon name="refresh" size={17} /></button></div>
    </div>
    <nav id="subnav" className="subnav" aria-label={t("Modem views")}>
      {modemTabs.map(item => <Link key={item.id} href={modemHref(item.path, selectedModem)} prefetch={false}
        aria-current={tab === item.id ? 'page' : undefined} className={tab === item.id ? 'active' : ''}>{t(item.label)}
        {item.id === 'configuration' && (profileErrors > 0 ? <span className="tab-error-count">{profileErrors}</span>
          : pendingByModem[selectedModem] > 0 && <span className="nav-dirty" aria-label={t("Unsaved changes")} />)}
      </Link>)}
    </nav>
  </>;
}
