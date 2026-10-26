import type { Config, ConfigChanges, ConfigValue } from '@/lib/types';
import { estimateAcquisition, fieldDefinition, pushedQueryGroups, queryGroups, queryPeriods } from './config.ts';
import type { SaveScope } from './config.ts';

export type RowComposite = 'route-role' | 'identify' | 'plmn' | 'queries' | 'listen' | 'listen-urls' | 'password';
export type RowDefinition = {
  id: string;
  /** Configuration paths edited by this row, as section.key. */
  fields: string[];
  /** Defaults to the label of the only field. */
  label?: string;
  hint?: string;
  composite?: RowComposite;
  /** Advanced rows stay hidden in basic mode unless they hold an error or a pending change. */
  advanced?: boolean | ((draft: Config) => boolean);
};
export type SectionDefinition = {
  id: string;
  label: string;
  icon: string;
  group?: 'mobile' | 'telemetry';
  /** A boolean field shown as the switch in the card header. */
  toggle?: string;
  rows: RowDefinition[];
};

const blank = (value: unknown) => !String(value ?? '').trim();

/** One modem profile: what a single save applies to that modem. */
export const profileSections: SectionDefinition[] = [
  { id: 'connection', group: 'mobile', label: "Connection", icon: 'signal', rows: [
    { id: 'mode', fields: ['modem.mode'] },
    { id: 'apn', fields: ['modem.apn'] },
    { id: 'plmn', fields: ['modem.plmn'], composite: 'plmn' },
    // Discovery normally sets this; a wrong selector detaches the modem.
    { id: 'identify', fields: ['modem.device', 'modem.serial'], composite: 'identify', label: "Identify this modem by",
      hint: "How the hardware is recognised after a restart or a USB port change.", advanced: true },
  ] },
  { id: 'routing', group: 'mobile', label: "Routing and DNS", icon: 'globe', rows: [
    { id: 'role', fields: ['network.default_route', 'network.route_policy'], composite: 'route-role', label: "Role for Internet access",
      hint: "Priority applies only after connectivity is verified." },
    { id: 'metric', fields: ['network.route_metric'],
      advanced: draft => !(draft.network?.default_route !== false && draft.network?.route_policy === 'fixed') },
    { id: 'method', fields: ['network.method'], advanced: draft => draft.network?.method !== 'dhcp' },
    { id: 'dns', fields: ['network.dns'], advanced: draft => (draft.network?.dns ?? 'auto') === 'auto' },
  ] },
  { id: 'verification', group: 'mobile', label: "Verification", icon: 'shield', toggle: 'network.connectivity_check', rows: [
    { id: 'targets', fields: ['network.connectivity_targets'] },
    { id: 'rhythm', fields: ['network.connectivity_interval', 'network.connectivity_timeout'], label: "Check rhythm",
      hint: "Each round tries the targets in order.", advanced: true },
    { id: 'thresholds', fields: ['network.connectivity_failures', 'network.connectivity_successes'], label: "Thresholds",
      hint: "Consecutive rounds before changing the route.", advanced: true },
  ] },
  { id: 'recovery', group: 'mobile', label: "Recovery", icon: 'refresh', rows: [
    { id: 'retries', fields: ['recovery.retry_initial', 'recovery.retry_max'], label: "Retries",
      hint: "The wait grows from the initial to the maximum delay.", advanced: true },
    { id: 'registration', fields: ['recovery.registration_timeout'], advanced: true },
    { id: 'operation', fields: ['recovery.operation_timeout'], advanced: true },
    { id: 'health', fields: ['recovery.poll_interval'], advanced: true },
  ] },
  { id: 'capture', group: 'telemetry', label: "Capture and queries", icon: 'activity', toggle: 'telemetry.enabled', rows: [
    { id: 'interval', fields: ['telemetry.interval'] },
    { id: 'profile', fields: ['telemetry.profile', ...queryGroups.map(group => 'telemetry.query_' + group.key)], composite: 'queries' },
    { id: 'throughput', fields: ['telemetry.throughput'] },
    { id: 'indications', fields: ['telemetry.indications'], advanced: draft => (draft.telemetry?.indications ?? 'off') === 'off' },
    { id: 'budget', fields: ['telemetry.collection_budget'], advanced: true },
    { id: 'request-timeout', fields: ['telemetry.request_timeout'], advanced: true },
  ] },
  { id: 'gps', group: 'telemetry', label: 'GPS', icon: 'gps', toggle: 'telemetry.gps', rows: [
    { id: 'source', fields: ['telemetry.gps_source'] },
    { id: 'max-age', fields: ['telemetry.gps_max_age'], advanced: true },
  ] },
  { id: 'destinations', group: 'telemetry', label: "Destinations", icon: 'download', rows: [
    { id: 'files', fields: ['telemetry.csv', 'telemetry.json'], label: "Save files" },
    { id: 'directory', fields: ['telemetry.directory'] },
    { id: 'udp', fields: ['telemetry.udp_host', 'telemetry.udp_port'], label: "UDP stream", hint: "Blank disables this destination.",
      advanced: draft => blank(draft.telemetry?.udp_host) },
    { id: 'collector', fields: ['telemetry.collector_host', 'telemetry.collector_port'], label: "Collector", hint: "Blank disables the collector.",
      advanced: draft => blank(draft.telemetry?.collector_host) },
    { id: 'flush', fields: ['telemetry.flush_interval'], advanced: true },
    { id: 'queue', fields: ['telemetry.queue_size'], advanced: true },
  ] },
];

/** The shared platform settings: the supervisor itself and the device's web service. */
export const systemSections: SectionDefinition[] = [
  { id: 'supervisor', label: "Supervisor", icon: 'cpu', rows: [
    { id: 'log-level', fields: ['system.log_level'] },
  ] },
  { id: 'access', label: "Web access", icon: 'globe', rows: [
    { id: 'listen', fields: ['web.host'], composite: 'listen', label: "Listen on", hint: "Who can open this interface." },
    { id: 'addresses', fields: [], composite: 'listen-urls', label: "Current addresses" },
    { id: 'port', fields: ['web.port'], advanced: true },
    { id: 'history', fields: ['web.history_size'], advanced: true },
    { id: 'workers', fields: ['web.max_workers'], advanced: true },
    { id: 'refresh', fields: ['web.address_refresh', 'web.poll_interval'], label: "Refresh",
      hint: "How often the LAN address is rechecked and how often the open page asks for new readings.", advanced: true },
    { id: 'enabled', fields: ['web.enabled'], advanced: draft => draft.web?.enabled !== false },
  ] },
];

/** The user account of the interface, edited on the profile page. */
export const accountSections: SectionDefinition[] = [
  { id: 'account', label: "Account", icon: 'user', rows: [
    { id: 'username', fields: ['web.username'] },
    { id: 'password', fields: [], composite: 'password', label: "New password", hint: "Leave blank to keep the current password." },
    { id: 'session', fields: ['web.session_timeout'], advanced: true },
  ] },
];

export const sectionsFor = (scope: SaveScope) =>
  scope === 'account' ? accountSections : scope === 'web' ? systemSections : profileSections;

export function valueAt(config: Config | null | undefined, path: string): ConfigValue | undefined {
  const [section, key] = path.split('.');
  return config?.[section]?.[key];
}

export const hasChange = (changes: ConfigChanges, path: string) => {
  const [section, key] = path.split('.');
  return Object.hasOwn(changes[section] ?? {}, key);
};

export type VisibilityContext = {
  draft: Config | null; advanced: boolean; errors: Record<string, string>; changes: ConfigChanges;
};

/** Rows for options this backend does not expose are never rendered. */
export function rowPresent(row: RowDefinition, draft: Config | null): boolean {
  if (!draft) return false;
  return row.fields.length === 0 ? Boolean(row.composite) : row.fields.some(path => valueAt(draft, path) !== undefined);
}

export function rowIsAdvanced(row: RowDefinition, draft: Config): boolean {
  return typeof row.advanced === 'function' ? row.advanced(draft) : Boolean(row.advanced);
}

/** A row with an error or a pending change is never hidden, so nothing can be trapped out of view. */
export function rowVisible(row: RowDefinition, context: VisibilityContext): boolean {
  const { draft } = context;
  if (!draft || !rowPresent(row, draft)) return false;
  if (context.advanced || !rowIsAdvanced(row, draft)) return true;
  return row.fields.some(path => Boolean(context.errors[path]) || hasChange(context.changes, path));
}

export const sectionFields = (section: SectionDefinition) =>
  [...(section.toggle ? [section.toggle] : []), ...section.rows.flatMap(row => row.fields)];

export function sectionVisible(section: SectionDefinition, context: VisibilityContext): boolean {
  if (section.toggle && valueAt(context.draft, section.toggle) !== undefined) return true;
  return section.rows.some(row => rowVisible(row, context));
}

export const visibleSections = (sections: SectionDefinition[], context: VisibilityContext) =>
  sections.filter(section => sectionVisible(section, context));

export const hiddenRowCount = (section: SectionDefinition, context: VisibilityContext) =>
  section.rows.filter(row => rowPresent(row, context.draft) && !rowVisible(row, context)).length;

export function sectionOf(path: string, sections: SectionDefinition[] = [...profileSections, ...systemSections, ...accountSections]): SectionDefinition | undefined {
  return sections.find(section => sectionFields(section).includes(path) || (path === 'web.password' && section.rows.some(row => row.composite === 'password')));
}

export type RouteRole = 'primary' | 'backup' | 'none' | 'fixed';
export const routeRoles: { value: RouteRole; label: string; description: string }[] = [
  { value: 'primary', label: "Primary route", description: "Preferred while its connectivity is verified." },
  { value: 'backup', label: "Backup route", description: "Takes over when the primary route fails." },
  { value: 'none', label: "No default route", description: "Only traffic addressed to this modem uses it." },
  { value: 'fixed', label: "Fixed metric", description: "You set the exact route metric." },
];

export function routeRole(network: Record<string, ConfigValue> | undefined): RouteRole {
  if (network?.default_route === false) return 'none';
  return network?.route_policy === 'primary' ? 'primary' : network?.route_policy === 'backup' ? 'backup' : 'fixed';
}

/** Primary and Backup start from the same reference metrics as before; a fixed metric is kept. */
export function routeRoleChanges(role: RouteRole): Record<string, ConfigValue> {
  if (role === 'none') return { default_route: false };
  if (role === 'primary') return { default_route: true, route_policy: 'primary', route_metric: 100 };
  if (role === 'backup') return { default_route: true, route_policy: 'backup', route_metric: 700 };
  return { default_route: true, route_policy: 'fixed' };
}

export type IdentificationMode = 'auto' | 'serial' | 'port';
export function identificationMode(modem: Record<string, ConfigValue> | undefined): IdentificationMode {
  if (modem?.device !== undefined && modem.device !== 'auto') return 'port';
  return String(modem?.serial ?? '') ? 'serial' : 'auto';
}

export type PlmnMode = 'keep' | 'auto' | 'fixed';
export const plmnMode = (value: ConfigValue | undefined): PlmnMode => !value ? 'keep' : value === 'auto' ? 'auto' : 'fixed';
export function plmnParts(value: ConfigValue | undefined): { mcc: string; mnc: string } | null {
  return typeof value === 'string' && /^[0-9]{5,6}$/.test(value) ? { mcc: value.slice(0, 3), mnc: value.slice(3) } : null;
}

export type ListenMode = 'local' | 'auto' | 'custom';
export function listenMode(host: ConfigValue | undefined): ListenMode {
  if (host === 'auto') return 'auto';
  return host === '::1' || /^127\./.test(String(host ?? '')) ? 'local' : 'custom';
}

export type QueryCost = { kind: 'off' } | { kind: 'push' } | { kind: 'ms'; ms: number };
/** Average cost per sample: each request takes about 32 ms on a USB modem. */
export function queryCost(telemetry: Record<string, ConfigValue>, key: string): QueryCost {
  const period = queryPeriods(telemetry)[key];
  if (!(period > 0)) return { kind: 'off' };
  if (telemetry.indications === 'on' && pushedQueryGroups.includes(key)) return { kind: 'push' };
  const interval = typeof telemetry.interval === 'number' && telemetry.interval > 0 ? telemetry.interval : 1;
  return { kind: 'ms', ms: 32 * Math.min(1, interval / period) };
}

export function acquisitionBudget(telemetry: Record<string, ConfigValue>) {
  const estimate = estimateAcquisition(telemetry);
  const interval = typeof telemetry.interval === 'number' && telemetry.interval > 0 ? telemetry.interval : 1;
  const seconds = typeof telemetry.collection_budget === 'number' && telemetry.collection_budget > 0 ? telemetry.collection_budget : interval;
  const budget = Math.round(seconds * 1000);
  return { ...estimate, budget, ratio: budget > 0 ? Math.min(1, estimate.average / budget) : 0, over: estimate.worst > budget };
}

/** "8 h", "1 min 30 s": long second values are easier to read in larger units. */
export function formatDuration(seconds: unknown): string | null {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 60) return null;
  const units: [number, string][] = [[86400, 'd'], [3600, 'h'], [60, 'min'], [1, 's']];
  const parts: string[] = [];
  let rest = Math.round(seconds);
  for (const [size, unit] of units) {
    const count = Math.floor(rest / size);
    if (count > 0 && parts.length < 2) { parts.push(count + ' ' + unit); rest -= count * size; }
  }
  return parts.join(' ');
}

/** The message key used to show a stored value, e.g. in "Modified · was X". */
export function displayValue(path: string, value: ConfigValue | undefined): { text: string; translate: boolean } {
  const [section, key] = path.split('.');
  if (value === undefined) return { text: "Not available", translate: true };
  const definition = fieldDefinition(section, key, value);
  if (typeof value === 'boolean') return { text: value ? "Enabled" : "Disabled", translate: true };
  if (value === '') return { text: "Blank", translate: true };
  const option = definition.options?.find(candidate => candidate.value === value);
  if (option) return { text: option.label, translate: true };
  if (typeof value === 'number' && definition.unit) return { text: value + (definition.unit === 's' ? ' s' : ''), translate: false };
  return { text: String(value), translate: false };
}

/** What a save interrupts. The data session restarts only when the saved mode connects. */
export function saveEffects(changes: ConfigChanges, saved: Config | null, scope: SaveScope): string[] {
  if (scope === 'account') {
    return changes.web && ('password' in changes.web || 'username' in changes.web)
      ? ["You will need to sign in again."] : ["Applying updates the session lifetime."];
  }
  if (scope === 'web') {
    // Worker commands carry the log level; the browser asks for the new cadence.
    if (changes.system) return ["Applying restarts the supervisor workers."];
    return ["Applying restarts web access."];
  }
  const effects: string[] = [];
  const reopens = Boolean(changes.modem && ('device' in changes.modem || 'serial' in changes.modem));
  const connection = ['modem', 'network', 'recovery'].some(section => changes[section]);
  if (reopens) effects.push("Applying reopens the modem; data and telemetry pause briefly.");
  else if (connection && saved?.modem?.mode === 'connect') effects.push("Applying restarts the data session.");
  if (changes.telemetry && !reopens) effects.push("Applying restarts telemetry.");
  return effects;
}
