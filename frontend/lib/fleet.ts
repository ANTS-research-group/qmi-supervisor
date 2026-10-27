import { t } from './messages.ts';
import type { Config, DataMap, ModemRecord, Snapshot } from '@/lib/types';

/** Profile identifiers accepted by the backend (fleet_config.py). */
export const validModemId = (value: unknown): value is string => typeof value === 'string' && /^[a-z][a-z0-9_-]{0,47}$/.test(value);

/** Modems whose hardware is present; a kept profile without its modem is not offered. */
export function detectedModems(modems: ModemRecord[]): ModemRecord[] {
  return modems.filter(entry => entry.detected !== false);
}

/** A backend without fleet inventory still supervises its default modem. */
export function fleetEntries(modems: ModemRecord[], snapshot: Snapshot | null, config: Config | null,
  selected = 'default'): ModemRecord[] {
  if (modems.length) {
    const detected = detectedModems(modems);
    if (detected.length) return detected;
    // Every modem unplugged: keep the selected profile on the single-modem page
    // instead of an empty fleet of unavailable modems.
    return [modems.find(entry => entry.id === selected) ?? modems.find(entry => entry.id === 'default') ?? modems[0]];
  }
  if (!snapshot) return [];
  const device = snapshot.device && typeof snapshot.device === 'object' ? snapshot.device : null;
  return [{
    id: 'default', configured: true, state: String(snapshot.state ?? 'STARTING'),
    detected: Boolean(device) && snapshot.state !== 'WAIT_DEVICE',
    device, interface: device?.interface ?? null, error: snapshot.error ?? null,
    apn: typeof config?.modem?.apn === 'string' ? config.modem.apn : undefined,
    connection_enabled: config?.modem?.mode === 'connect', monitoring_enabled: config?.telemetry?.enabled !== false,
    features: snapshot.features, network: snapshot.network,
  }];
}

export type StateTone = 'good' | 'warning' | 'bad' | 'neutral';
const states: Record<string, [string, StateTone]> = {
  ONLINE: ["Online", 'good'], MONITOR: ["Monitoring only", 'good'], RUNNING: ["Running", 'good'],
  WAIT_DEVICE: ["Waiting for modem", 'neutral'], STARTING: ["Starting", 'warning'], OPEN: ["Opening modem", 'warning'],
  WAIT_REGISTRATION: ["Waiting for registration", 'warning'], CONNECT: ["Activating data session", 'warning'],
  CONFIGURE_IP: ["Configuring IPv4", 'warning'], BACKOFF: ["Waiting to retry", 'warning'], RECOVERING: ["Recovering", 'warning'],
  DEGRADED: ["Degraded", 'warning'], IDLE: ["Idle", 'neutral'], DISABLED: ["Disabled", 'neutral'],
  STOPPED: ["Stopped", 'neutral'], STOPPING: ["Stopping", 'neutral'],
};

/** One vocabulary for modem states across the fleet, headers and summaries. */
export function statePresentation(state: unknown, detected = true): { label: string; tone: StateTone } {
  if (!detected) return { label: t("Not detected"), tone: 'neutral' };
  const known = states[String(state)];
  return known ? { label: t(known[0]), tone: known[1] } : { label: state ? String(state) : t("No data"), tone: 'neutral' };
}

export function fleetSummary(entries: ModemRecord[]) {
  return {
    online: entries.filter(entry => entry.detected && entry.state === 'ONLINE').length,
    undetected: entries.filter(entry => !entry.detected).length,
  };
}

const connectivity = (entry: ModemRecord): DataMap => entry.network?.connectivity && typeof entry.network.connectivity === 'object' ? entry.network.connectivity : {};

/** The kernel uses the lowest-metric default route; demoted modems publish a higher effective metric. */
export function exitOrder(entries: ModemRecord[]) {
  return entries
    .filter(entry => entry.detected && typeof entry.network?.route_metric === 'number' && Number.isFinite(entry.network.route_metric)
      && !['disabled', 'blocked'].includes(String(connectivity(entry).route_mode)))
    .sort((a, b) => a.network!.route_metric - b.network!.route_metric || a.id.localeCompare(b.id))
    .map((entry, index) => ({ id: entry.id, interface: entry.interface ?? null, metric: entry.network!.route_metric as number, position: index + 1, active: index === 0 }));
}

/** Only an active connection has a meaningful role; monitor mode reports a disabled route. */
export function showsRouteRole(entry: ModemRecord): boolean {
  return entry.connection_enabled && ['requested', 'fallback'].includes(String(connectivity(entry).route_mode));
}

export function modemHref(path: string, id: string, extra: Record<string, string> = {}): string {
  return path + '?' + new URLSearchParams({ modem: id, ...extra }).toString();
}

/** Model text without the serial some firmwares append: 'Fibocom Modem_SN:123' -> 'Fibocom Modem'. */
const modelName = (product: unknown) => String(product ?? '').trim().replace(/\s*[_.]?sn[:\s].*$/i, '').trim();

/** Human name of a modem: manufacturer and model, never the profile identifier. */
export function modemName(entry: ModemRecord): string {
  const make = String(entry.device?.manufacturer ?? '').trim(), model = modelName(entry.device?.product);
  const label = !model ? make : !make || model.toLowerCase().startsWith(make.toLowerCase()) ? model : make + ' ' + model;
  if (label) return label;
  // Hardware without USB identity: a generated profile still names its vendor; never the id.
  const id = String(entry.id || '');
  const vendor = id.startsWith('auto-') ? id.slice(5).split('-')[0] : '';
  return vendor ? vendor.charAt(0).toUpperCase() + vendor.slice(1) : t("Modem");
}

/** The line under a modem name: its USB serial and network interface. */
export function modemSerialLine(entry: ModemRecord): string {
  const serial = String(entry.device?.serial || entry.selector?.serial || '').trim();
  const started = entry.interface || entry.device?.interface || '';
  return [serial, started].filter(Boolean).join(' · ');
}

/** Hover text for the serial line: the value itself is unlabelled on screen. */
export function modemSerialTitle(entry: ModemRecord): string {
  const serial = String(entry.device?.serial || entry.selector?.serial || '').trim();
  return serial ? t("Serial number: {serial}", { serial }) : t("Serial number not reported");
}

export type PageKind = 'fleet' | 'modem' | 'system' | 'account';
export function pageKind(pathname: string, fleet: boolean): PageKind {
  const path = pathname.endsWith('/') ? pathname : pathname + '/';
  if (path === '/configuration/web/') return 'system';
  if (path === '/profile/') return 'account';
  if (path === '/' || path === '/dashboard/') return fleet ? 'fleet' : 'modem';
  return 'modem';
}

export type ModemTab = 'summary' | 'metrics' | 'configuration' | 'diagnostics' | 'tools';
export const modemTabs: { id: ModemTab; label: string; path: string }[] = [
  { id: 'summary', label: "Summary", path: '/modem/' },
  { id: 'metrics', label: "Metrics", path: '/metrics/charts/' },
  { id: 'configuration', label: "Configuration", path: '/configuration/modem/' },
  { id: 'diagnostics', label: "Diagnostics", path: '/metrics/data/' },
  { id: 'tools', label: "Tools", path: '/tools/' },
];
export function modemTab(pathname: string): ModemTab {
  if (pathname.startsWith('/tools')) return 'tools';
  if (pathname.startsWith('/metrics/data')) return 'diagnostics';
  if (pathname.startsWith('/metrics/')) return 'metrics';
  if (pathname.startsWith('/configuration/')) return 'configuration';
  return 'summary';
}

/** Fleet switches keep a pending position until the published inventory follows the saved revision. */
export type ObjectiveOverride = { enabled: boolean; revision: string | null };
export function pruneObjectiveOverrides(overrides: Record<string, ObjectiveOverride>, revision: string | null | undefined): Record<string, ObjectiveOverride> {
  return Object.fromEntries(Object.entries(overrides).filter(([, value]) => value.revision === (revision ?? null)));
}
