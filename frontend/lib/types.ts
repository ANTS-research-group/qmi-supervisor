// QMI keys vary with the installed libqmi version. Preserve raw diagnostic data.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type DataMap = Record<string, any>;
export type Snapshot = DataMap;
/** Latest fresh radio reading published with each inventory entry. */
export interface ModemSignal {
  rat?: string | null; band?: string | null; operator?: string | null;
  rsrp?: number | null; snr?: number | null;
}
export interface ModemRecord {
  id: string; detected: boolean; configured: boolean; state: string;
  device?: { product?: string; manufacturer?: string; serial?: string; path?: string; interface?: string; usb_path?: string } | null;
  selector?: { device?: string; serial?: string } | null;
  interface?: string | null; apn?: string; error?: string | null;
  connection_enabled: boolean; monitoring_enabled: boolean;
  features?: DataMap; network?: DataMap; signal?: ModemSignal | null;
  assignment_pending?: boolean; cleanup_pending?: boolean; modem_alive?: boolean;
}
export type MetricPoint = DataMap;
export type ConfigValue = string | number | boolean;
export type Config = Record<string, Record<string, ConfigValue>>;
export type ConfigChanges = Config;
export type AuthState = 'checking' | 'authenticated' | 'required';
export type Tone = 'success' | 'warning' | 'error' | 'info';
export interface ConnectivityStatus {
  enabled: boolean;
  state: 'disabled' | 'checking' | 'up' | 'down';
  stale?: boolean;
  checked_monotonic?: number | null;
  check_age_ms?: number | null;
  target?: string | null;
  error?: string | null;
  consecutive_successes?: number;
  consecutive_failures?: number;
  route_mode?: 'requested' | 'fallback' | 'disabled' | 'blocked';
  preferred?: boolean;
  effective_route_metric?: number | null;
  route_error?: string | null;
  dns_error?: string | null;
  /** network.routes installed through the modem, and why one could not be. */
  routes?: string[];
  routes_error?: string | null;
}
export interface Notice { id: number; message: string; tone: Tone }
export interface Freshness {
  heartbeat_age_ms?: number | null;
  snapshot_age_ms?: number | null;
  sample_age_ms?: number | null;
  state_fresh?: boolean | null;
}
export interface Session {
  authenticated: boolean;
  username: string | null;
  csrf_token: string | null;
  expires_in: number | null;
  methods: { password: boolean };
  readonly: boolean;
}
export interface ConfigResponse {
  config: Config;
  revision?: string;
  modem_id?: string;
  effects?: string[];
  editable?: boolean;
  listen_urls?: string[];
  auto_apply?: boolean;
  restart_required?: boolean;
}
export interface StatusResponse { snapshot: Snapshot | null; freshness?: Freshness; warning?: string }
