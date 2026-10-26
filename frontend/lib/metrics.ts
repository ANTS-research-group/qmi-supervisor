import { intlLocale, t } from './messages.ts';
import type { DataMap, MetricPoint, Snapshot } from '@/lib/types';

export type SignalMetric = 'rsrp' | 'rsrq' | 'snr';
export type ChartSeries = { key: string; label: string; unit?: string; color?: string; ratKey?: string };
export const CHART_COLORS = ['#0d9488', '#6285c6', '#d4a044', '#a076b8'];
export const SIGNALS: { key: SignalMetric; label: string; unit: string; min: number; max: number }[] = [
  { key: 'rsrp', label: 'RSRP', unit: 'dBm', min: -140, max: -50 },
  { key: 'rsrq', label: 'RSRQ', unit: 'dB', min: -30, max: 0 },
  { key: 'snr', label: 'SNR', unit: 'dB', min: -20, max: 40 },
];
export const SIGNAL_LEVELS = [
  { name: "Excellent", color: '#77b82a', ink: 'var(--signal-excellent)' }, { name: "Good", color: '#c7a600', ink: 'var(--signal-good)' },
  { name: "Fair", color: '#ef8b00', ink: 'var(--signal-fair)' }, { name: "Cell edge", color: '#d82323', ink: 'var(--signal-edge)' },
];
export const SIGNAL_LIMITS: Record<SignalMetric, number[]> = {
  rsrp: [-100, -90, -80], rsrq: [-20, -15, -10], snr: [0, 13, 20],
};

export const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
export const sampleIsFresh = (age: unknown, interval = 1) => finite(age) && age <= Math.max(3000, interval * 3000);
export const object = (value: unknown): DataMap => value && typeof value === 'object' && !Array.isArray(value) ? value as DataMap : {};
export const items = (value: unknown): DataMap[] => Array.isArray(value) ? value.filter(item => item && typeof item === 'object') : [];
/** Every published field becomes one row: nested maps are joined with dotted keys. */
export function flatten(values: unknown, prefix = ''): DataMap {
  const output: DataMap = {};
  const walk = (value: unknown, path: string) => {
    const record = value && typeof value === 'object' && !Array.isArray(value) ? value as DataMap : null;
    const entries = record ? Object.entries(record) : [];
    if (!record || !entries.length) {
      if (path) output[path] = value;
      return;
    }
    for (const [key, child] of entries) walk(child, path ? path + '.' + key : key);
  };
  walk(values, prefix);
  return output;
}
export function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return t("Not available");
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
export function number(value: unknown, digits = 1): string {
  if (!finite(value)) return t("Not available");
  return value.toLocaleString(intlLocale(), value !== 0 && Math.abs(value) < 10 ** -digits
    ? { maximumSignificantDigits: 3 } : { maximumFractionDigits: digits });
}
export const ms = (value: unknown) => finite(value) ? number(value) + ' ms' : "Not available";
export const age = (value: unknown) => finite(value) ? value < 1000 ? number(value, 0) + ' ms' : number(value / 1000) + ' s' : "Unknown";
export function date(value: unknown): string {
  if (typeof value !== 'string' && typeof value !== 'number') return t("Not available");
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Not available" : parsed.toLocaleString(intlLocale(), { hour12: false });
}
export function clockLabel(point?: MetricPoint | null): string {
  const value = point?.time;
  if (typeof value !== 'string' && typeof value !== 'number') return "Time not available";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Time not available" : parsed.toLocaleTimeString(intlLocale(), { hour12: false });
}
export function pointTime(point: MetricPoint): number {
  if (finite(point.monotonic)) return point.monotonic * 1000;
  if (finite(point.observed_monotonic)) return point.observed_monotonic * 1000;
  return typeof point.time === 'string' ? Date.parse(point.time) : NaN;
}
export const pointKey = (point: MetricPoint) => String(point.run_id ?? '') + '|' + pointTime(point);
export function orderedPoints(points: MetricPoint[]): MetricPoint[] {
  return points.slice(-1000).filter(point => point && finite(pointTime(point))).sort((a, b) => pointTime(a) - pointTime(b));
}
export function lineConnects(previous: MetricPoint, current: MetricPoint, key: string, interval = 1): boolean {
  if (!finite(previous[key]) || !finite(current[key])) return false;
  if (previous.run_id && current.run_id && previous.run_id !== current.run_id) return false;
  if (previous.device_generation !== current.device_generation) return false;
  if (previous[key + '_rat'] !== current[key + '_rat']) return false;
  if (previous[key + '_run_id'] !== current[key + '_run_id']) return false;
  if (finite(current.skipped_intervals) && current.skipped_intervals > 0) return false;
  const difference = pointTime(current) - pointTime(previous);
  return finite(difference) && difference > 0 && difference <= Math.max(250, interval * 1500);
}
export function signalValue(dynamic: DataMap | null | undefined, key: SignalMetric): { value: number | null; rat: string | null } {
  const fields: Record<SignalMetric, string[]> = {
    rsrp: ['nas_value_5g_signal_strength_rsrp', 'nas_value_lte_rsrp'],
    rsrq: ['nas_value_5g_signal_strength_extended', 'nas_value_lte_rsrq'],
    snr: ['nas_value_5g_signal_strength_snr', 'nas_value_lte_snr'],
  };
  for (const [index, field] of fields[key].entries()) {
    if (finite(dynamic?.[field])) return { value: dynamic[field], rat: index === 0 ? 'NR' : 'LTE' };
  }
  return { value: null, rat: null };
}
export function signalQuality(metric: SignalMetric, value: unknown) {
  if (!finite(value)) return null;
  const [low, middle, high] = SIGNAL_LIMITS[metric];
  const level = SIGNAL_LEVELS[value >= high ? 0 : value >= middle ? 1 : value > low ? 2 : 3];
  return { ...level, name: t(level.name) };
}
export function signalSegments(metric: SignalMetric, first: number, last: number) {
  if (!finite(first) || !finite(last)) return [];
  const cuts = [0, 1], delta = last - first;
  if (delta) for (const threshold of SIGNAL_LIMITS[metric]) {
    const fraction = (threshold - first) / delta;
    if (fraction > 0 && fraction < 1) cuts.push(fraction);
  }
  cuts.sort((a, b) => a - b);
  return cuts.slice(0, -1).map((from, index) => {
    const to = cuts[index + 1];
    return { from, to, color: signalQuality(metric, first + delta * (from + to) / 2)!.color };
  });
}
export function valueScale(values: number[], options: { metric?: SignalMetric; min?: number; max?: number } = {}) {
  const valid = values.filter(finite), observedMin = valid.length ? Math.min(...valid) : null;
  const observedMax = valid.length ? Math.max(...valid) : null;
  let min: number, max: number;
  if (options.metric && valid.length) {
    const minimumSpan = { rsrp: 10, rsrq: 4, snr: 10 }[options.metric];
    const center = (observedMin! + observedMax!) / 2, span = Math.max(minimumSpan, (observedMax! - observedMin!) * 1.24);
    min = center - span / 2; max = center + span / 2;
    if (finite(options.min) && min < options.min && observedMin! >= options.min) { max += options.min - min; min = options.min; }
    if (finite(options.max) && max > options.max && observedMax! <= options.max) { min -= max - options.max; max = options.max; }
  } else {
    min = finite(options.min) ? options.min : Math.min(0, observedMin ?? 0);
    max = finite(options.max) ? options.max : Math.max(1, (observedMax ?? 0) * 1.12);
    if (valid.length) { min = Math.min(min, observedMin!); max = Math.max(max, observedMax!); }
    if (max <= min) max = min + 1;
  }
  return { min, max, observedMin, observedMax };
}
export type ChartSelection = { point: MetricPoint | null; index: number | null; time: number; gap: boolean };
export function nearestPoint(points: MetricPoint[], time: number, series: ChartSeries[], interval = 1): ChartSelection | null {
  if (!points.length || !finite(time)) return null;
  let low = 0, high = points.length;
  while (low < high) { const middle = (low + high) >> 1; if (pointTime(points[middle]) < time) low = middle + 1; else high = middle; }
  const after = Math.min(low, points.length - 1), before = Math.max(0, low - 1);
  const index = Math.abs(pointTime(points[before]) - time) <= Math.abs(pointTime(points[after]) - time) ? before : after;
  if (before !== after && time > pointTime(points[before]) && time < pointTime(points[after])) {
    const connects = series.some(record => lineConnects(points[before], points[after], record.key, interval));
    const tolerance = Math.min(interval * 250, (pointTime(points[after]) - pointTime(points[before])) * 0.25);
    if (!connects && Math.abs(pointTime(points[index]) - time) > tolerance) return { point: null, index: null, time, gap: true };
  }
  return { point: points[index], index, time: pointTime(points[index]), gap: false };
}
export const ratLabel = (value: unknown) => value === 'nr' ? 'NR' : value === 'lte' ? 'LTE' : text(value);
export function rfValue(item: DataMap, kind: 'band' | 'bandwidth' = 'band') {
  if (kind === 'band' && item.band) return String(item.band);
  if (kind === 'bandwidth' && finite(item.bandwidth_mhz)) return number(item.bandwidth_mhz, 3) + ' MHz';
  if (item.label) return String(item.label);
  return item.raw !== null && item.raw !== undefined ? t('QMI code') + ' ' + item.raw : "Not available";
}
export function radio(snapshot: Snapshot | null): DataMap {
  return object(snapshot?.radio || snapshot?.radio_summary);
}
export function imsiValue(sample?: DataMap | null): string | null {
  const value = sample?.static?.dms_value_imsi;
  return typeof value === 'string' && value !== '' ? value : null;
}
export function apnPresentation(value?: DataMap | null) {
  const connection = object(value);
  const requested = typeof connection.requested_apn === 'string' && connection.requested_apn ? connection.requested_apn : null;
  const reported = typeof connection.reported_apn === 'string' && connection.reported_apn ? connection.reported_apn : null;
  const status = connection.apn_verification;
  const verification = ({ matched: "Matched", unavailable: "Not verifiable by the modem", mismatch: "Mismatch" } as Record<string, string>)[status] || "Unverified";
  const warning = status === 'unavailable' && requested ? t('The modem does not report the effective APN.')
    : status === 'mismatch' && requested ? t('Requested APN {requested}; reported APN {reported}; they do not match.', { requested, reported: reported || t('Not available') }) : null;
  return { requested, reported, verification, warning, tone: status === 'mismatch' ? 'error' : 'warning' };
}
export function trafficPresentation(sample: DataMap | null | undefined, configured = false) {
  const metadata = object(sample?.collection?.interface_traffic), dynamic = object(sample?.dynamic);
  const enabled = 'enabled' in metadata ? metadata.enabled === true : configured;
  const status = enabled ? metadata.status || 'initializing' : 'disabled';
  return { enabled, status, unit: metadata.rate_unit || 'Kibit/s', interface: metadata.interface || null,
    rx: status === 'ok' && finite(dynamic.throughput_download_speed_kbps) ? dynamic.throughput_download_speed_kbps : null,
    tx: status === 'ok' && finite(dynamic.throughput_upload_speed_kbps) ? dynamic.throughput_upload_speed_kbps : null,
    rxBytes: finite(metadata.rx_bytes) ? metadata.rx_bytes : null, txBytes: finite(metadata.tx_bytes) ? metadata.tx_bytes : null,
    error: metadata.error || null, interval: metadata.interval_seconds, observed: metadata.observed_at_utc };
}
export function gpsPresentation(sample: DataMap | null | undefined, configured = false) {
  const metadata = object(sample?.collection?.gps), dynamic = object(sample?.dynamic);
  const enabled = 'enabled' in metadata ? metadata.enabled === true : configured;
  const status = enabled ? metadata.status || 'unavailable' : 'disabled', modem = metadata.source === 'qmi_loc';
  const position = status === 'fixed' || status === 'stale';
  const value = (gnss: string, gpsd: string) => position && finite(dynamic[modem ? gnss : gpsd]) ? dynamic[modem ? gnss : gpsd] : null;
  const msl = modem || finite(dynamic.gpsd_tpv_altmsl);
  return { enabled, status, source: metadata.source || null, latitude: value('gnss_latitude', 'gpsd_tpv_lat'),
    longitude: value('gnss_longitude', 'gpsd_tpv_lon'), altitude: value('gnss_altitude_msl_m', msl ? 'gpsd_tpv_altmsl' : 'gpsd_tpv_alt'),
    altitudeReference: msl ? 'MSL' : 'GPSd', ellipsoid: value('gnss_altitude_ellipsoid_m', 'gpsd_tpv_althae'),
    speed: value('gnss_horizontal_speed_mps', 'gpsd_tpv_speed'), uncertainty: modem ? value('gnss_horizontal_uncertainty_m', '') : null,
    confidence: modem ? value('gnss_horizontal_confidence_percent', '') : null,
    timestamp: position ? dynamic[modem ? 'gnss_timestamp_utc' : 'gpsd_tpv_datetime'] || null : null,
    error: metadata.error || null, age: metadata.age_ms ?? metadata.tpv?.age_ms };
}
const LABELS: Record<string, string> = {
  nas_value_lte_rsrp: 'RSRP LTE', nas_value_lte_rsrq: 'RSRQ LTE', nas_value_lte_rssi: 'RSSI LTE', nas_value_lte_snr: 'SNR LTE',
  nas_value_5g_signal_strength_rsrp: 'RSRP NR', nas_value_5g_signal_strength_snr: 'SNR NR', nas_value_5g_signal_strength_extended: 'RSRQ NR',
  nas_value_nr5g_cell_information_rsrp: "NR cell RSRP", nas_value_nr5g_cell_information_rsrq: "NR cell RSRQ", nas_value_nr5g_cell_information_snr: "NR cell SNR",
  nas_value_serving_system_registration_state: "Registration status", nas_value_current_plmn_mcc: "Network MCC", nas_value_current_plmn_mnc: "Network MNC",
  nas_value_current_plmn_description: "Network name", nas_value_roaming_indicator: "Roaming", wds_value_connection_status: "Session status",
  wds_value_tx_bytes_ok: "Bytes sent", wds_value_rx_bytes_ok: "Bytes received", wds_value_tx_packets_ok: "Packets sent", wds_value_rx_packets_ok: "Packets received",
  wds_value_tx_packets_error: "Transmit errors", wds_value_rx_packets_error: "Receive errors", gpsd_tpv_lat: "GPS latitude", gpsd_tpv_lon: "GPS longitude",
  gpsd_tpv_alt: "GPS altitude", gpsd_tpv_altmsl: "MSL altitude", gpsd_tpv_speed: "GPS speed", gnss_latitude: "GNSS latitude", gnss_longitude: "GNSS longitude",
  gnss_altitude_msl_m: "MSL altitude", gnss_altitude_ellipsoid_m: "Ellipsoidal altitude", gnss_timestamp_utc: "Position timestamp",
  gnss_horizontal_speed_mps: "Horizontal speed", gnss_horizontal_uncertainty_m: "Horizontal uncertainty",
  throughput_upload_speed_kbps: "Upload rate", throughput_download_speed_kbps: "Download rate", throughput_upload_kb: "Upload traffic", throughput_download_kb: "Download traffic",
  nas_value_tx_power: "Transmit power", nas_value_sinr: 'SINR', nas_value_signal_strength_strength: "Signal strength", nas_value_signal_strength_radio_interface: "Signal-strength RAT",
  dms_value_imsi: "IMSI · SIM identity", dms_value_imei: "IMEI · modem identity",
};
/** A dotted row keeps its full key in the table; only the last segment is spoken as a label. */
export function metricLabel(key: string): string {
  const leaf = key.includes('.') ? key.slice(key.lastIndexOf('.') + 1) : key;
  return LABELS[key] || LABELS[leaf] || leaf.replace(/^(nas_value_|wds_value_|gpsd_|gnss_|throughput_)/, '').replaceAll('_', ' ');
}

export function resourceProcesses(resources: DataMap): string[] {
  return resources.manager ? ['manager', 'modem', 'telemetry', 'web'] : ['supervisor', 'web'];
}
export function processLabel(name: string): string {
  return t(({ manager: 'Process manager', modem: 'Modem', telemetry: 'Telemetry', web: 'Web', supervisor: 'Supervisor' } as Record<string, string>)[name] || name);
}
