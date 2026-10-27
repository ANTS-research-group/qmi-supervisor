import type { Config, ConfigChanges, ConfigValue } from '@/lib/types';
import { parseConnectivityTargets } from './connectivity.ts';

export type FieldOption = { value: string; label: string; description?: string };
export type FieldDefinition = {
  label: string;
  kind: 'text' | 'number' | 'integer' | 'boolean' | 'select';
  hint?: string;
  min?: number;
  max?: number;
  positive?: boolean;
  options?: readonly FieldOption[];
  /** The default control follows `kind`: switch, select, number or text input. */
  control?: 'segmented' | 'stepper' | 'chips';
  unit?: 's' | 'samples';
  mono?: boolean;
  /** Also show long second values as minutes or hours. */
  duration?: boolean;
};

const seconds = (label: string, extra: Partial<FieldDefinition> = {}): FieldDefinition => ({ label, kind: 'number', positive: true, unit: 's', ...extra });
const port = (label: string): FieldDefinition => ({ label, kind: 'integer', min: 1, max: 65535 });
const query = (label: string): FieldDefinition => ({ label, kind: 'number', min: 0, max: 86400, unit: 's' });
const secrets = new Set(['password', 'password_hash', 'password_confirm', 'password_confirmation']);

/** One save covers a modem profile, the shared web interface or the user account. */
export type SaveScope = 'profile' | 'web' | 'account';

/** Account fields travel in the shared web section but are edited and saved from the profile page. */
const accountKeys = new Set(['username', 'password', 'session_timeout']);

/** Whether a stored field belongs to a save scope; the transient password is validated only with the account. */
export function inScope(scope: SaveScope, section: string, key: string): boolean {
  if (scope === 'profile') return section !== 'web' && section !== 'system';
  if (section !== 'web' && section !== 'system') return false;
  return accountKeys.has(key) === (scope === 'account');
}
export type FieldAvailability = { disabled: boolean; reason?: string };

/** Availability describes runtime use; it never removes values or validation errors. */
export function fieldAvailability(draft: Config | null, section: string, key: string): FieldAvailability {
  if (!draft) return { disabled: true, reason: "Configuration is not available yet." };
  const modem = draft.modem ?? {}, network = draft.network ?? {}, telemetry = draft.telemetry ?? {}, web = draft.web ?? {};
  if (section === 'network') {
    if (network.default_route === false && (['route_policy', 'route_metric', 'dns'].includes(key) || key.startsWith('connectivity_'))) {
      return { disabled: true, reason: "Enable the default route to use this option." };
    }
    if (network.connectivity_check === false && key.startsWith('connectivity_') && key !== 'connectivity_check') {
      return { disabled: true, reason: "Enable connectivity checking to use this option." };
    }
  }
  if (section === 'telemetry') {
    if (telemetry.profile !== 'custom' && key.startsWith('query_')) return { disabled: true, reason: "Select the Custom query profile to use this option." };
    if (telemetry.gps === false && ['gps_source', 'gps_max_age'].includes(key)) return { disabled: true, reason: "Enable GPS to use this option." };
    if (key === 'udp_port' && !String(telemetry.udp_host ?? '').trim()) return { disabled: true, reason: "Configure a UDP destination to enable its port." };
    if (key === 'collector_port' && !String(telemetry.collector_host ?? '').trim()) return { disabled: true, reason: "Configure a collector destination to enable its port." };
    if (telemetry.csv === false && telemetry.json === false && ['directory', 'flush_interval', 'queue_size'].includes(key)) {
      return { disabled: true, reason: "Enable CSV or JSON to use this storage option." };
    }
  }
  if (section === 'web' && web.enabled === false && ['host', 'port', 'history_size', 'max_workers', 'address_refresh', 'poll_interval'].includes(key)) {
    return { disabled: true, reason: "Enable the web interface to use this option." };
  }
  return { disabled: false };
}

/** QMI query groups; mirrors modules/device/query_plan.py. */
export const queryGroups = [
  { key: 'signal', label: "Signal info", metrics: "LTE RSRP, RSRQ, RSSI and SNR; NR RSRP, RSRQ and SNR.", period: 1 },
  { key: 'cell', label: "Cell information", metrics: "Serving NR cell (PCI, cell ID, ARFCN, TAC) and LTE serving and neighbour cells.", period: 1 },
  { key: 'txrx', label: "Tx/Rx chains", metrics: "Receive power per antenna chain and transmit power.", period: 1 },
  { key: 'serving', label: "Serving system", metrics: "Registration and attach state, operator (PLMN) and roaming.", period: 5 },
  { key: 'bands', label: "RF bands", metrics: "Active bands, channels and bandwidths.", period: 10 },
  { key: 'strength', label: "Legacy signal strength", metrics: "Generic strength and legacy 3G/EV-DO values; LTE/NR values come from signal info.", period: 0 },
  { key: 'home', label: "Home network", metrics: "Home operator of the SIM.", period: 3600 },
  { key: 'wds_stats', label: "Packet statistics", metrics: "Packets, bytes, errors and drops of the data session.", period: 1 },
  { key: 'wds_status', label: "Bearer status", metrics: "Whether the data session is connected.", period: 1 },
  { key: 'wds_rates', label: "Channel rates", metrics: "Current and maximum channel rates reported by the modem.", period: 10 },
] as const;
const allQueryGroups = queryGroups.map(group => group.key as string);
export const queryPresets: Record<string, string[]> = {
  fast: ['strength', 'signal', 'wds_stats', 'wds_status'],
  standard: allQueryGroups.filter(key => key !== 'home' && key !== 'txrx'),
  full: allQueryGroups,
};
/** Groups that QMI indications can serve without a request. */
export const pushedQueryGroups = ['wds_stats', 'wds_status', 'serving'];

/** Effective refresh period of each group in seconds; 0 means not requested. */
export function queryPeriods(telemetry: Record<string, ConfigValue>): Record<string, number> {
  const interval = typeof telemetry.interval === 'number' && telemetry.interval > 0 ? telemetry.interval : 1;
  const preset = queryPresets[String(telemetry.profile)];
  return Object.fromEntries(allQueryGroups.map(key => {
    if (preset) return [key, preset.includes(key) ? interval : 0];
    const value = telemetry['query_' + key];
    return [key, typeof value === 'number' && Number.isFinite(value) && value > 0 ? Math.max(value, interval) : 0];
  }));
}

/** query_* values that make the custom profile request exactly what the current profile does. */
export function customFromProfile(telemetry: Record<string, ConfigValue>): Record<string, number> {
  return Object.fromEntries(Object.entries(queryPeriods(telemetry)).map(([key, period]) => ['query_' + key, period]));
}

/** USB modems answer one QMI request at a time: about 24 ms for the first and 32 ms for each further one. */
export function estimateAcquisition(telemetry: Record<string, ConfigValue>): { average: number; worst: number } {
  const interval = typeof telemetry.interval === 'number' && telemetry.interval > 0 ? telemetry.interval : 1;
  const periods = Object.entries(queryPeriods(telemetry))
    .filter(([key, period]) => period > 0 && !(telemetry.indications === 'on' && pushedQueryGroups.includes(key)));
  const duration = (requests: number) => requests > 0 ? 24 + (requests - 1) * 32 : 0;
  return {
    average: Math.round(duration(periods.reduce((sum, [, period]) => sum + interval / period, 0))),
    worst: duration(periods.length),
  };
}

export function scopedChanges(base: Config | null, draft: Config | null, password = '', scope?: SaveScope): ConfigChanges {
  const changes = diffConfig(base, draft, password);
  if (!scope) return changes;
  return Object.fromEntries(Object.entries(changes)
    .map(([section, values]) => [section, Object.fromEntries(Object.entries(values).filter(([key]) => inScope(scope, section, key)))] as const)
    .filter(([, values]) => Object.keys(values).length > 0));
}

export function scopedErrors(errors: Record<string, string>, scope?: SaveScope): Record<string, string> {
  return Object.fromEntries(Object.entries(errors).filter(([path]) => {
    const [section, key] = path.split('.');
    return !scope || inScope(scope, section, key);
  }));
}

/** Validate this actual API candidate: unrelated drafts cannot affect the request. */
export function scopedDraft(base: Config | null, draft: Config | null, scope?: SaveScope): Config | null {
  if (!base || !draft) return null;
  if (!scope) return sanitizeConfig(draft);
  const result = sanitizeConfig(base);
  for (const [section, values] of Object.entries(draft)) {
    for (const [key, value] of Object.entries(values)) {
      if (!secrets.has(key) && inScope(scope, section, key)) (result[section] ??= {})[key] = value;
    }
  }
  return result;
}

export function discardScopedDraft(base: Config | null, draft: Config | null, scope?: SaveScope): Config | null {
  if (!base) return null;
  if (!scope || !draft) return sanitizeConfig(base);
  const result = sanitizeConfig(draft);
  for (const section of new Set([...Object.keys(base), ...Object.keys(result)])) {
    for (const key of new Set([...Object.keys(base[section] ?? {}), ...Object.keys(result[section] ?? {})])) {
      if (!inScope(scope, section, key) || secrets.has(key)) continue;
      if (Object.hasOwn(base[section] ?? {}, key)) (result[section] ??= {})[key] = base[section][key];
      else delete result[section]?.[key];
    }
  }
  return result;
}

/** Rebase pending edits, including edits made while this request was in flight. */
export function mergeSavedConfig(base: Config | null, current: Config | null, saved: Config, submitted: ConfigChanges): Config {
  const result = sanitizeConfig(saved);
  if (!base || !current) return result;
  for (const [section, values] of Object.entries(current)) {
    for (const [key, value] of Object.entries(values)) {
      if (secrets.has(key)) continue;
      const wasSubmitted = Object.hasOwn(submitted[section] ?? {}, key);
      const preserve = wasSubmitted ? !Object.is(value, submitted[section][key]) : !Object.is(value, base[section]?.[key]);
      if (preserve) (result[section] ??= {})[key] = value;
    }
  }
  return result;
}

export const configFields: Record<string, Record<string, FieldDefinition>> = {
  modem: {
    device: { label: "Control port", kind: 'text', mono: true, hint: "Absolute path, for example /dev/cdc-wdm0." },
    serial: { label: "USB serial", kind: 'text', mono: true },
    apn: { label: 'APN', kind: 'text', mono: true, hint: "Provided by the operator." },
    plmn: { label: "Network selection", kind: 'text', mono: true, hint: "Operator by MCC + MNC (5 or 6 digits)." },
    mode: { label: "Mode", kind: 'select', control: 'segmented', hint: "Monitoring only reads the radio without changing routes or addresses.",
      options: [{ value: 'connect', label: "Connect and configure networking" }, { value: 'monitor', label: "Monitoring only" }] },
  },
  network: {
    method: { label: "IPv4 configuration", kind: 'select', control: 'segmented', options: [{ value: 'qmi', label: 'QMI' }, { value: 'dhcp', label: 'DHCP' }] },
    route_metric: { label: "Route metric", kind: 'integer', min: 0, max: 4294967295, mono: true },
    route_policy: { label: "Role for Internet access", kind: 'select', options: [{ value: 'fixed', label: "Fixed metric" }, { value: 'primary', label: "Primary route" }, { value: 'backup', label: "Backup route" }] },
    default_route: { label: "Add default route", kind: 'boolean' },
    dns: { label: "DNS management", kind: 'select', hint: "Preserving DNS keeps the existing resolver configuration.", options: [{ value: 'auto', label: "Automatic" }, { value: 'none', label: "Preserve DNS" }, { value: 'resolvectl', label: 'resolvectl' }, { value: 'resolvconf', label: 'resolvconf' }] },
    connectivity_check: { label: "Verify WWAN connectivity", kind: 'boolean', hint: "The preferred route applies after verifying modem connectivity." },
    connectivity_targets: { label: "TCP check targets", kind: 'text', control: 'chips', hint: "Up to 4 IPv4:port targets. On a private network use a service inside it." },
    connectivity_interval: { label: "Check interval", kind: 'number', min: 1, max: 60, unit: 's' },
    connectivity_timeout: { label: "Check timeout", kind: 'number', min: 0.1, max: 5, unit: 's', hint: "Must not exceed the check interval." },
    connectivity_failures: { label: "Failures before falling back", kind: 'integer', min: 1, max: 10, control: 'stepper' },
    connectivity_successes: { label: "Successes before restoring", kind: 'integer', min: 1, max: 10, control: 'stepper' },
  },
  recovery: {
    retry_initial: seconds("Initial retry delay"),
    retry_max: seconds("Maximum retry delay", { duration: true }),
    registration_timeout: seconds("Registration timeout", { duration: true }),
    operation_timeout: { label: "Operation timeout", kind: 'integer', min: 1, unit: 's' },
    poll_interval: seconds("Connection health interval"),
  },
  telemetry: {
    enabled: { label: "Collect telemetry", kind: 'boolean' },
    interval: seconds("Target interval"),
    profile: { label: "Query profile", kind: 'select', control: 'segmented', hint: "Each query adds about 32 ms to the samples that include it.",
      options: [{ value: 'fast', label: "Fast" }, { value: 'standard', label: "Standard" }, { value: 'full', label: "Full" }, { value: 'custom', label: "Custom" }] },
    indications: { label: "QMI indications", kind: 'select', control: 'segmented', hint: "Modem-pushed packet statistics, bearer status and serving system. Shadow compares them with queries before enabling.", options: [{ value: 'off', label: "Off" }, { value: 'shadow', label: "Shadow (compare)" }, { value: 'on', label: "On" }] },
    query_signal: query("Signal info"),
    query_cell: query("Cell information"),
    query_txrx: query("Tx/Rx chains"),
    query_serving: query("Serving system"),
    query_bands: query("RF bands"),
    query_strength: query("Legacy signal strength"),
    query_home: query("Home network"),
    query_wds_stats: query("Packet statistics"),
    query_wds_status: query("Bearer status"),
    query_wds_rates: query("Channel rates"),
    collection_budget: seconds("Acquisition budget", { hint: "Must not exceed the target interval." }),
    request_timeout: { label: "Query timeout", kind: 'integer', min: 1, max: 60, unit: 's' },
    queue_size: { label: "Write queue", kind: 'integer', min: 1, max: 4096, unit: 'samples' },
    flush_interval: seconds("File flush interval"),
    directory: { label: "Output directory", kind: 'text', mono: true },
    csv: { label: "Save CSV", kind: 'boolean' },
    json: { label: "Save JSON", kind: 'boolean' },
    udp_host: { label: "UDP destination", kind: 'text', mono: true },
    udp_port: port("UDP port"),
    collector_host: { label: "Collector destination", kind: 'text', mono: true },
    collector_port: port("Collector port"),
    throughput: { label: "WWAN traffic", kind: 'boolean', hint: "RX and TX rates measured on the modem interface." },
    gps: { label: "GPS enabled", kind: 'boolean' },
    gps_source: { label: "GPS source", kind: 'select', control: 'segmented', options: [{ value: 'gpsd', label: "External GPSd" }, { value: 'modem', label: "Modem GPS (QMI)" }] },
    gps_max_age: seconds("Maximum GPS age"),
  },
  web: {
    enabled: { label: "Web interface", kind: 'boolean', hint: "Turning it off makes this interface unavailable until it is enabled on the device." },
    host: { label: "Listen address", kind: 'text', mono: true, hint: "A numeric IP address of this device." },
    port: port("Web port"),
    history_size: { label: "Chart history", kind: 'integer', min: 1, max: 1000, unit: 'samples' },
    max_workers: { label: "HTTP workers", kind: 'integer', min: 1, max: 32, hint: "Concurrent API and asset requests." },
    address_refresh: { label: "Address refresh", kind: 'number', min: 5, max: 600, unit: 's',
      hint: "How often the management LAN address is rechecked." },
    poll_interval: { label: "Data refresh", kind: 'number', min: 1, max: 30, unit: 's',
      hint: "How often the open page asks for new readings." },
    username: { label: "Username", kind: 'text', hint: "1 to 64 letters, digits or _ . @ - characters" },
    session_timeout: { label: "Session lifetime", kind: 'integer', min: 300, max: 604800, unit: 's', duration: true, hint: "Between 5 minutes and 7 days." },
  },
  system: {
    log_level: { label: "Log level", kind: 'select', control: 'segmented',
      hint: "Detail of the supervisor and worker logs.",
      options: [{ value: 'DEBUG', label: "Debug" }, { value: 'INFO', label: "Info" },
        { value: 'WARNING', label: "Warning" }, { value: 'ERROR', label: "Error" }] },
    log_file: { label: "Log file", kind: 'text', mono: true,
      hint: "Absolute path of a rotating log file. Empty keeps only the systemd journal." },
  },
};

export function sanitizeConfig(config: Config): Config {
  return Object.fromEntries(Object.entries(config).map(([section, values]) => [
    section,
    Object.fromEntries(Object.entries(values).filter(([key]) => !secrets.has(key))),
  ]));
}

export function diffConfig(base: Config | null, draft: Config | null, password = ''): ConfigChanges {
  const changes: ConfigChanges = {};
  if (!base || !draft) return changes;
  for (const [section, values] of Object.entries(draft)) {
    for (const [key, value] of Object.entries(values)) {
      if (secrets.has(key) || Object.is(base[section]?.[key], value)) continue;
      (changes[section] ??= {})[key] = value;
    }
  }
  if (password !== '') (changes.web ??= {}).password = password;
  return changes;
}

export function countChanges(changes: ConfigChanges): number {
  return Object.values(changes).reduce((count, fields) => count + Object.keys(fields).length, 0);
}

export function isNumericIP(host: string): boolean {
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
    return host.split('.').every(part => Number(part) <= 255 && String(Number(part)) === part);
  }
  if (!host.includes(':') || /[\s/%\[\]]/.test(host)) return false;
  try { return new URL(`http://[${host}]/`).hostname.startsWith('['); } catch { return false; }
}

export function fieldDefinition(section: string, key: string, value: ConfigValue): FieldDefinition {
  return configFields[section]?.[key] ?? {
    label: key.replaceAll('_', ' '),
    kind: typeof value === 'boolean' ? 'boolean' : typeof value === 'number' ? 'number' : 'text',
  };
}

export function validateConfig(draft: Config | null, password = ''): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!draft) return errors;
  for (const [section, values] of Object.entries(draft)) {
    for (const [key, value] of Object.entries(values)) {
      if (secrets.has(key)) continue;
      const definition = fieldDefinition(section, key, value);
      const path = `${section}.${key}`;
      if (definition.kind === 'number' || definition.kind === 'integer') {
        if (typeof value !== 'number' || !Number.isFinite(value)) errors[path] = "Enter a finite number.";
        else if (definition.kind === 'integer' && !Number.isInteger(value)) errors[path] = "Enter an integer.";
        else if (definition.positive && value <= 0) errors[path] = "Must be greater than 0.";
        else if (definition.min !== undefined && value < definition.min) errors[path] = `Minimum: ${definition.min}.`;
        else if (definition.max !== undefined && value > definition.max) errors[path] = `Maximum: ${definition.max}.`;
      } else if (definition.kind === 'boolean') {
        if (typeof value !== 'boolean') errors[path] = "Select a boolean value.";
      } else if (definition.kind === 'select') {
        if (!definition.options?.some(option => option.value === value)) errors[path] = "Select a valid option.";
      } else if (typeof value !== 'string' || /[\r\n\0]/.test(value)) errors[path] = "Must fit on one line.";
    }
  }
  const modem = draft.modem ?? {}, network = draft.network ?? {}, recovery = draft.recovery ?? {}, telemetry = draft.telemetry ?? {}, web = draft.web ?? {}, system = draft.system ?? {};
  if (network.connectivity_targets !== undefined && !parseConnectivityTargets(network.connectivity_targets)) errors['network.connectivity_targets'] = "Use 1 to 4 valid IPv4:port targets, without duplicates, loopback or multicast destinations.";
  if (typeof network.connectivity_timeout === 'number' && typeof network.connectivity_interval === 'number' && network.connectivity_timeout > network.connectivity_interval) errors['network.connectivity_timeout'] = "Must not exceed the check interval.";
  if (modem.device !== undefined && modem.device !== 'auto' && !(typeof modem.device === 'string' && modem.device.startsWith('/'))) errors['modem.device'] = "Use auto or an absolute path.";
  if (modem.mode === 'connect' && (typeof modem.apn !== 'string' || !modem.apn.trim())) errors['modem.apn'] = "An APN is required to connect.";
  if (modem.plmn !== undefined && modem.plmn !== '' && !(typeof modem.plmn === 'string' && /^(auto|[0-9]{5,6})$/.test(modem.plmn))) errors['modem.plmn'] = "Use auto, blank or a 5-/6-digit MCC+MNC.";
  if (typeof recovery.retry_initial === 'number' && typeof recovery.retry_max === 'number' && recovery.retry_max < recovery.retry_initial) errors['recovery.retry_max'] = "Must not be shorter than the initial retry delay.";
  if (typeof telemetry.interval === 'number' && typeof telemetry.collection_budget === 'number' && telemetry.collection_budget > telemetry.interval) errors['telemetry.collection_budget'] = "Must not exceed the target interval.";
  if (telemetry.profile === 'custom' && modem.mode === 'connect' && telemetry.query_wds_status === 0) errors['telemetry.query_wds_status'] = "Bearer status is required to supervise a connection.";
  if (typeof telemetry.directory === 'string' && !telemetry.directory.trim()) errors['telemetry.directory'] = "The directory must not be empty.";
  if (typeof system.log_file === 'string' && system.log_file !== '' && !system.log_file.startsWith('/')) errors['system.log_file'] = "Use an absolute path, or leave it blank.";
  if (web.host !== undefined && web.host !== 'auto' && !(typeof web.host === 'string' && isNumericIP(web.host))) errors['web.host'] = "Use auto or a numeric IP address.";
  if (web.username !== undefined && !(typeof web.username === 'string' && /^[A-Za-z0-9_.@-]{1,64}$/.test(web.username))) errors['web.username'] = "Use 1 to 64 letters, digits or _ . @ - characters.";
  if (password !== '') {
    if (Array.from(password).length < 10) errors['web.password'] = "Use at least 10 characters.";
    else if (new TextEncoder().encode(password).length > 256) errors['web.password'] = "The password exceeds 256 UTF-8 bytes.";
    else if (/\p{Cc}/u.test(password)) errors['web.password'] = "Control characters are not allowed.";
  }
  return errors;
}
