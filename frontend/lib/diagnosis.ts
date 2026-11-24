// Failure presentation: codes, labels and issue collection.
// Pure functions only (no React) so node --experimental-strip-types can run it.
import { modemName } from './fleet.ts';
import { t } from './messages.ts';
import type { Config, DataMap, Diagnosis, Freshness, ModemRecord, Snapshot } from './types';

export interface IssueTechnical {
  code?: string;
  step?: string;
  operation?: string;
  label?: string;
  detail?: string;
}

export interface IssueLink {
  href: string;
  label: string;
}

export type IssueTone = 'info' | 'warning' | 'bad';

export interface Issue {
  id: string;
  tone: IssueTone;
  /** English message ID, for t(title, vars). */
  title: string;
  /** Interpolation values for t(title, vars). */
  vars?: Record<string, string | number>;
  /** English message ID. */
  explanation?: string;
  /** English message ID. */
  action?: string;
  /** Already translated lines (SIM slot mapping, recovery ladder, ...). */
  lines?: string[];
  since?: string;
  attempts?: number;
  nextSeconds?: number;
  technical?: IssueTechnical;
  link?: IssueLink;
}

export interface IssueSources {
  snapshot: Snapshot | null;
  entry?: ModemRecord | null;
  config: Config | null;
  freshness?: Freshness;
  /** Either the whole features block or its telemetry sub-block. */
  telemetryFeature?: DataMap | null;
  apn?: { warning: string | null; tone: string } | null;
  routes?: { conflict?: { network: string; overlaps: string; dev: string } | null; error?: string | null } | null;
  traffic?: { error?: string | null; enabled?: boolean } | null;
  gps?: { error?: string | null; enabled?: boolean } | null;
  /** Already-translated reason, or null. */
  attachFallback?: string | null;
  /** The fleet inventory, to name a peer modem that reaches the network. */
  modems?: ModemRecord[];
  modemId?: string;
  label?: string | null;
  nextSeconds?: number | null;
}

/** Every code published by modules/runtime/diagnosis.py (LOG_TITLES and POLICIES). */
export const CODE_TEXT: Record<string, { title: string; explanation?: string; action?: string }> = {
  // Generic and host
  'qmi.no_answer': {
    title: 'The modem did not answer in time',
    explanation: 'This happened while {operation}.',
    action: 'Usually temporary right after the modem starts. If it keeps happening, unplug and replug the modem or power-cycle it.',
  },
  'qmi.endpoint_hangup': {
    title: 'The connection to the modem closed while waiting for an answer',
    explanation: 'The modem restarted or left the USB bus during the request.',
    action: 'If the modem does not come back, check the USB cable, hub and power supply.',
  },
  'qmi.error': {
    title: 'The modem refused a request',
    explanation: 'The request made while {operation} returned an error.',
    action: 'See the technical detail and the system log.',
  },
  'unknown': {
    title: 'Unexpected error',
    explanation: 'The step failed while {operation}.',
    action: 'See the technical detail and the system log.',
  },
  'device.removed': {
    title: 'The modem left the USB bus or was enumerated again',
    explanation: 'The supervisor reopens it when it is detected again.',
    action: 'If it keeps happening, check the USB cable, hub and power supply.',
  },
  'device.locked': {
    title: 'Another process is using this modem',
    explanation: 'The modem is reserved by another supervisor worker or tool.',
    action: 'Close other programs that use the modem; the supervisor retries by itself.',
  },
  'host.modemmanager_active': {
    title: 'ModemManager is running and would compete for the modem',
    explanation: 'Two managers cannot control the same modem.',
    action: 'Run qmi-supervisor-configure --modem-manager disable, or keep the connection objective off.',
  },
  'host.udhcpc_missing': {
    title: 'DHCP needs udhcpc, which is not installed',
    explanation: 'The profile uses DHCP on the modem interface.',
    action: 'Install the dependencies with qmi-supervisor-configure.',
  },
  'config.pending': {
    title: 'A configuration change could not be applied yet',
    explanation: 'The supervisor keeps the previous configuration and retries.',
    action: 'See the technical detail; correct the configuration if the problem persists.',
  },
  'worker.restarting': {
    title: 'The modem process stopped and is being restarted',
    explanation: 'The supervisor restarts it automatically.',
    action: 'If it keeps restarting, see the system log.',
  },
  'worker.starting': {
    title: 'The modem process is starting',
  },
  'session.lost': {
    title: 'The data session or its IPv4 configuration was lost',
    explanation: 'The supervisor reconnects by itself.',
    action: 'If it repeats, check coverage and the APN.',
  },
  'ip.incomplete': {
    title: 'The data session is up but IPv4 is not configured on the host',
    explanation: 'The modem connected, but the address, route or DNS could not be applied.',
    action: 'See the technical detail and the system log.',
  },
  'telemetry.stalled': {
    title: 'Monitoring has stored no sample since {time}',
    explanation: 'The modem did not complete the acquisition.',
  },
  // Recovery
  'recovery.saved_client_unanswered': {
    title: 'The modem does not answer the data session left by a previous run',
    explanation: 'The supervisor saved that session so it could stop it safely. The modem no longer answers through it, so the supervisor keeps the record instead of risking cutting a session it cannot confirm, and does not connect.',
    action: 'If no other program uses this modem, unplug and replug it, power-cycle it or reboot the host. On the new USB connection the supervisor discards the old record safely and connects again. Restarting only the supervisor, or a software USB port reset, does not help.',
  },
  'recovery.cleanup_unconfirmed': {
    title: 'The stop of a previous data session could not be confirmed',
    explanation: 'The supervisor keeps the record and retries.',
    action: 'See the technical detail and the system log.',
  },
  'recovery.host_cleanup_unconfirmed': {
    title: 'The host network of a previous run could not be cleaned',
    explanation: 'Addresses or routes left by a previous run could not be removed.',
    action: 'See the technical detail and the system log.',
  },
  'recovery.foreign_record': {
    title: 'The saved connection record belongs to another modem',
    explanation: "This profile's previous session was started on a different modem or USB port.",
    action: 'Reconnect the modem this profile used before so its old session can be stopped.',
  },
  // SIM
  'sim.absent': {
    title: 'No SIM card detected',
    explanation: 'The modem reports logical slot {slot} as empty.',
    action: 'Insert a SIM in the slot the modem uses. If a card is already there, re-seat it: when card detection is set the wrong way, a present card reads as absent.',
  },
  'sim.no_atr': {
    title: 'The SIM card does not answer',
    explanation: 'The modem powers the card in logical slot {slot} but gets no answer. The card may be dead, badly seated or reversed. With card detection off, an empty slot looks the same.',
    action: 'Re-seat the SIM in the slot the modem uses. If it still fails, test it in another device or try a known-good SIM.',
  },
  'sim.contact_lost': {
    title: 'The modem lost contact with the SIM',
    explanation: 'The card in logical slot {slot} stopped answering while in use.',
    action: 'Check that the card and any adapter are firmly seated.',
  },
  'sim.card_error': {
    title: 'The SIM card reports an error ({error})',
    explanation: 'The modem reports an electrical or protocol error for the card in logical slot {slot}.',
    action: 'Try a known-good SIM; replace this card if the error follows it.',
  },
  'sim.not_reported': {
    title: 'The modem does not report any SIM slot',
    explanation: 'The SIM interface returned no card information.',
    action: "Restart the modem; if it persists, check the modem's SIM hardware.",
  },
  'sim.initializing': {
    title: 'The SIM is starting',
    explanation: 'The card answered and is still initializing.',
  },
  'sim.pin_required': {
    title: 'The SIM is locked with a PIN ({retries} attempts left)',
    explanation: 'The supervisor never enters PINs.',
    action: 'Disable the PIN in a phone, or unlock the SIM, then reinsert it. The supervisor checks again every minute.',
  },
  'sim.puk_required': {
    title: 'The SIM is blocked and needs its PUK ({retries} attempts left)',
    explanation: 'Too many wrong PINs were entered.',
    action: 'Unblock it with the PUK in a phone, then reinsert it.',
  },
  'sim.blocked': {
    title: 'The SIM is permanently blocked',
    explanation: 'The PUK attempts are exhausted.',
    action: 'Replace the SIM.',
  },
  'sim.network_locked': {
    title: 'The modem is locked to another network or SIM ({feature})',
    explanation: 'The SIM-lock (personalization) does not accept this card.',
    action: 'Use a SIM the modem accepts, or ask the modem vendor to remove the lock.',
  },
  'sim.app_illegal': {
    title: 'The network has invalidated this SIM',
    explanation: 'After a rejection, the SIM application is marked illegal until the modem restarts.',
    action: 'Fix the subscription in the core, then restart the modem.',
  },
  'sim.in_inactive_slot': {
    title: 'The SIM is in a slot the modem does not use',
    explanation: 'A card is detected in physical slot {occupied}, but the modem uses physical slot {active}.',
    action: 'Move the SIM to physical slot {active} (often labelled SIM{active} or USIM{active}). The supervisor never switches slots by itself.',
  },
  'sim.operator_action_required': {
    title: 'The SIM still fails after {resets} modem restarts',
    explanation: 'Automatic modem restarts are paused until {time} because the card diagnosis has not changed.',
    action: 'Fix the SIM by hand. A working card is still detected without waiting.',
  },
  // Registration
  'registration.modem_silent': {
    title: 'The modem does not answer network queries',
    explanation: 'Every registration query in the last {duration} failed.',
    action: 'Unplug and replug the modem or power-cycle it.',
  },
  'registration.sim_invalidated': {
    title: 'The network has invalidated this SIM for {domain}',
    explanation: 'After a rejection the modem will not try this SIM again until it restarts.',
    action: 'Fix the subscription, then restart the modem so it tries the SIM again.',
  },
  'registration.rejected': {
    title: '{plmn} rejects this SIM on {rat}: {cause}',
    explanation: 'Seen {count} times since {since}.',
    action: 'See the cause in the technical detail.',
  },
  'registration.home_not_found': {
    title: 'The modem has not camped on its home network {home}',
    explanation: 'For {duration} it only found other networks ({networks}) with emergency service only. The network has not blocked this SIM (SIM reject state: {sim_reject}).',
    action: "Check that a {home} cell is on the air here and that this modem's radio settings (RAT, bands, 5G SA) allow it, then check that this SIM is provisioned in the {home} core.",
  },
  'registration.limited_service': {
    title: 'No network accepts this SIM here',
    explanation: 'The modem only gets emergency service ({networks}).',
    action: 'Check the subscription of this SIM and the coverage of its network.',
  },
  'registration.no_coverage': {
    title: 'No mobile network found',
    explanation: 'The modem has not seen any cell for {duration}.',
    action: 'Check the antennas and the coverage at this site.',
  },
  'registration.restricted': {
    title: 'The modem is set to camp only, without registering',
    explanation: 'Its registration restriction is {restriction}.',
    action: "Review the modem's network selection settings.",
  },
  'registration.wrong_network': {
    title: 'The modem is registered on {plmn}, not on the requested {expected}',
    explanation: 'The profile requests PLMN {expected}.',
    action: 'Check the PLMN setting of this profile.',
  },
  'registration.timeout': {
    title: 'Network registration timed out',
    explanation: 'No registration within {seconds} s and no cause observed.',
    action: 'Check the SIM, coverage and provisioning.',
  },
  // Startup and call end
  'startup.transient': {
    title: 'Modem still starting after a new USB connection',
    explanation: 'This is normal for up to {seconds} s after the modem starts.',
  },
  'connect.rejected': {
    title: 'The network rejected the data session',
    explanation: 'Reason: {reason}.',
    action: 'Check the APN and that this SIM may use it.',
  },
};

/** Operation tags set by stage() in diagnosis.py, rendered inside sentences. */
export const OPERATION_LABEL: Record<string, string> = {
  'open.lock': 'reserving the modem',
  'open.session': 'opening the modem',
  'open.allocate': 'allocating a QMI client',
  'open.recover.saved_stop': 'stopping the data session of a previous run',
  'open.recover.release': 'releasing the client of a previous run',
  'open.recover.host': 'cleaning the host network of a previous run',
  'prepare.network': 'preparing the network interface',
  'prepare.sim': 'checking the SIM',
  'prepare.attach_apn': 'setting the initial APN',
  'prepare.plmn': 'selecting the network',
  'prepare.data_format': 'setting the data format',
  'register.wait': 'waiting for network registration',
  'connect.start': 'starting the data session',
  'configure.ip': 'configuring IPv4',
  'health.check': 'checking the connection',
};

/** Enum values interpolated into titles/explanations. */
export const VALUE_LABEL: Record<string, Record<string, string>> = {
  service_status: {
    none: 'none',
    limited: 'limited',
    available: 'available',
    'limited-regional': 'limited regional',
    'power-save': 'power save',
  },
  sim_reject: {
    unavailable: 'unavailable',
    available: 'available',
    'cs-invalid': 'invalid for voice',
    'ps-invalid': 'invalid for data',
    'cs-ps-invalid': 'invalid for voice and data',
  },
  domain: {
    cs: 'voice',
    ps: 'data',
    'cs-ps': 'voice and data',
    none: 'none',
  },
  rat: {
    '5gnr': '5G NR',
    lte: 'LTE',
    umts: 'UMTS',
    gsm: 'GSM',
  },
  card_error: {
    'power-down': 'powered down',
    poll: 'poll error',
    'no-atr-received': 'no answer to reset',
    'voltage-mismatch': 'voltage mismatch',
    parity: 'parity error',
    'possibly-removed': 'possibly removed',
    technical: 'technical problem',
    unknown: 'unknown',
  },
  presence: {
    unknown: 'unknown',
    absent: 'absent',
    present: 'present',
  },
  restriction: {
    'camped-only': 'camped only',
    limited: 'limited service',
    unrestricted: 'unrestricted',
  },
  slot_state: {
    inactive: 'inactive',
    active: 'active',
  },
};

/** Diagnosis parameters that carry enum values and the table that names them. */
const PARAM_GROUP: Record<string, string> = {
  service: 'service_status',
  status: 'service_status',
  sim_reject: 'sim_reject',
  domain: 'domain',
  rat: 'rat',
  error: 'card_error',
  presence: 'presence',
  state: 'slot_state',
  restriction: 'restriction',
};

/** MCC-MNC split: '99910' -> '999-10', shorter codes untouched. */
export function formatPlmn(value: string): string {
  const text = String(value ?? '').trim();
  return text.length > 3 ? text.slice(0, 3) + '-' + text.slice(3) : text;
}

/** One diagnosis parameter as an interpolation value, translating known enums. */
function paramValue(name: string, value: unknown): string | number {
  if (Array.isArray(value)) {
    const items = value.map(item => paramValue(name, item));
    // PLMNs read better as 214-03; other lists keep their values.
    return name === 'networks' ? items.map(item => formatPlmn(String(item))).join(', ') : items.join(', ');
  }
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') return value;
  if (typeof value === 'boolean') return String(value);
  const text = String(value);
  if (name === 'plmn' || name === 'home' || name === 'expected') return formatPlmn(text);
  const group = PARAM_GROUP[name];
  if (group) {
    const table = VALUE_LABEL[group];
    return table && Object.hasOwn(table, text) ? t(table[text]) : text;
  }
  for (const table of Object.values(VALUE_LABEL)) {
    if (Object.hasOwn(table, text)) return t(table[text]);
  }
  return text;
}

/** Placeholder names inside one catalogue message. */
function placeholders(message: string): string[] {
  return [...message.matchAll(/\{(\w+)\}/g)].map(match => match[1]);
}

/** True when every placeholder of the message has a non-empty value. */
function complete(message: string, vars?: Record<string, string | number>): boolean {
  return placeholders(message).every(name => vars && Object.hasOwn(vars, name)
    && vars[name] !== undefined && vars[name] !== null && String(vars[name]) !== '');
}

/** A catalogue message translated and interpolated; unknown placeholders disappear. */
export function issueText(message: string, vars?: Record<string, string | number>): string {
  const translated = t(message);
  if (!translated.includes('{')) return translated;
  let missing = false;
  const result = translated.replace(/\{(\w+)\}/g, (match, name) => {
    if (vars && Object.hasOwn(vars, name) && vars[name] !== undefined && vars[name] !== null) {
      return String(vars[name]);
    }
    missing = true;
    return '';
  });
  // Removing a placeholder can leave doubled spaces or a space before punctuation.
  return missing ? result.replace(/\s{2,}/g, ' ').replace(/\s+([.,;:])/g, '$1').trim() : result;
}

function severityTone(severity: Diagnosis['severity'] | undefined): IssueTone {
  if (severity === 'error') return 'bad';
  if (severity === 'info') return 'info';
  return 'warning';
}

const GENERIC_TITLE = 'This modem could not finish its last step.';

/** One issue from a published diagnosis or from an older backend's plain error. */
export function diagnosisIssue(diagnosis: Diagnosis | null | undefined, error?: string | null,
  options: { nextSeconds?: number | null; label?: string | null } = {}): Issue | null {
  const detail = typeof error === 'string' ? error.trim() : '';
  const rawDetail = typeof error === 'string' ? error : '';
  const nextSeconds = typeof options.nextSeconds === 'number' ? options.nextSeconds : undefined;
  const label = options.label ?? undefined;

  if (diagnosis && diagnosis.active === false) return null;

  const code = diagnosis && typeof diagnosis.code === 'string' ? diagnosis.code : '';
  const text = code ? CODE_TEXT[code] : undefined;

  if (!text) {
    if (!detail) return null;
    const technical: IssueTechnical = { code: code || undefined, step: diagnosis?.step, operation: diagnosis?.operation ?? undefined, label, detail: rawDetail };
    const generic: Issue = { id: 'diagnosis.generic', tone: 'warning', title: GENERIC_TITLE, technical };
    if (nextSeconds !== undefined) generic.nextSeconds = nextSeconds;
    generic.since = diagnosis?.failing_since_utc ?? diagnosis?.since_utc;
    if (typeof diagnosis?.attempts === 'number') generic.attempts = diagnosis.attempts;
    return generic;
  }

  const params = diagnosis?.params && typeof diagnosis.params === 'object' ? diagnosis.params : {};
  const vars: Record<string, string | number> = {};
  for (const [name, value] of Object.entries(params)) vars[name] = paramValue(name, value);
  if (diagnosis?.step) vars.step = diagnosis.step;
  if (diagnosis?.operation) vars.operation = t(OPERATION_LABEL[diagnosis.operation] ?? diagnosis.operation);

  const issue: Issue = {
    id: 'diagnosis.' + code,
    tone: severityTone(diagnosis?.severity),
    title: text.title,
    technical: {
      code,
      step: diagnosis?.step,
      operation: diagnosis?.operation ?? undefined,
      label,
      detail: diagnosis?.detail ?? error ?? undefined,
    },
  };
  if (Object.keys(vars).length) issue.vars = vars;
  // An explanation whose placeholders have no values is worse than none.
  if (text.explanation && complete(text.explanation, vars)) issue.explanation = text.explanation;
  if (text.action && complete(text.action, vars)) issue.action = text.action;
  issue.since = diagnosis?.failing_since_utc ?? diagnosis?.since_utc;
  if (typeof diagnosis?.attempts === 'number') issue.attempts = diagnosis.attempts;
  if (nextSeconds !== undefined) issue.nextSeconds = nextSeconds;
  return issue;
}

/** Fleet indicator tone for one inventory entry. A plain error never becomes 'bad'. */
export function issueTone(entry: ModemRecord): IssueTone | null {
  // A fleet-level error is not an actor diagnosis: it is shown first and never hidden.
  if (entry.error_source === 'fleet' && entry.error) return 'warning';
  const diagnosis = entry.diagnosis;
  if (diagnosis && diagnosis.active !== false) {
    if (diagnosis.severity === 'error') return 'bad';
    if (diagnosis.severity === 'warning') return 'warning';
    if (diagnosis.severity === 'info') return 'info';
  }
  if (entry.error_source === 'fleet' || entry.error) return 'warning';
  return null;
}

/** Only warning and bad need attention; info issues never raise the indicator. */
export function attentionTone(issues: Issue[]): 'warning' | 'bad' | null {
  let tone: 'warning' | 'bad' | null = null;
  for (const issue of issues) {
    if (issue.tone === 'bad') return 'bad';
    if (issue.tone === 'warning') tone = 'warning';
  }
  return tone;
}

const TONE_ORDER: Record<IssueTone, number> = { bad: 0, warning: 1, info: 2 };

const STALL_EXPLANATION: Record<string, string> = {
  modem_not_readable: 'The modem cannot be read while its connection keeps failing (step {step}). Fix that problem first.',
  modem_absent: 'The modem is not detected.',
  acquisition_failed: 'The modem did not complete the acquisition.',
};
const STALL_GENERIC = 'The modem did not complete the acquisition.';

const SHORT_UTC = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
});

/** A short UTC stamp for the stall title: '2026-01-01 10:00 UTC'. */
function shortUtcTime(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const parts = Object.fromEntries(SHORT_UTC.formatToParts(parsed).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} UTC`;
}

/** SIM slot and ladder lines: the mapping and what has been tried. */
function simLines(snapshot: Snapshot | null): string[] {
  const sim = snapshot?.sim && typeof snapshot.sim === 'object' ? snapshot.sim as DataMap : {};
  const recovery = sim.recovery && typeof sim.recovery === 'object' ? sim.recovery as DataMap : {};
  const time = typeof recovery.last_power_cycle_utc === 'string' ? shortUtcTime(recovery.last_power_cycle_utc) : '';
  const restart = typeof recovery.last_reset_utc === 'string' ? shortUtcTime(recovery.last_reset_utc) : '';
  const resets = typeof recovery.resets === 'number' ? recovery.resets : 0;
  const lines: string[] = [];
  // Only a modem answer carries slot lines: a null `slots` means it was asked
  // and did not report, and a missing key means no status was read at all.
  if (Object.hasOwn(sim, 'slots')) {
    const slots = Array.isArray(sim.slots)
      ? sim.slots.filter((slot): slot is DataMap => Boolean(slot) && typeof slot === 'object') : [];
    const active = slots.find(slot => slot.state === 'active');
    if (active && typeof active.logical_slot === 'number') {
      lines.push(t("Slot in use: physical slot {active} (logical slot {logical}), as reported by the modem.",
        { active: active.physical_slot, logical: active.logical_slot }));
    }
    for (const slot of slots) {
      const state = typeof slot.state === 'string' ? slot.state : 'unknown';
      const card = typeof slot.card === 'string' ? slot.card : 'unknown';
      lines.push(t("Physical slot {slot}: {state}, card {presence}.", {
        slot: slot.physical_slot, state: t(VALUE_LABEL.slot_state[state] ?? state),
        presence: t(VALUE_LABEL.presence[card] ?? card) }));
    }
    if (!active) lines.push(t("This modem does not report which physical slot it uses."));
  }
  if (time && restart) {
    lines.push(t("Already tried: SIM power cycle at {time}; modem restart at {restart} ({resets} so far).",
      { time, restart, resets }));
  } else if (time) {
    lines.push(t("Already tried: SIM power cycle at {time}.", { time }));
  }
  if (time || restart) {
    lines.push(t("The supervisor keeps checking: SIM power cycle at most every 2 min, modem restart at most every 30 min."));
  }
  return lines;
}

/** Registration lines: the home network and a peer that reaches it. */
function registrationLines(sources: IssueSources, params: Record<string, unknown>): string[] {
  const snapshot = sources.snapshot;
  const registration = snapshot?.registration && typeof snapshot.registration === 'object'
    ? snapshot.registration as DataMap : {};
  const home = typeof registration.home === 'string' ? registration.home : '';
  const lines = home ? [t("Home network: {home}", { home: formatPlmn(home) })] : [];
  const forbidden = Array.isArray(params.forbidden) ? params.forbidden.map(String) : [];
  if (home && forbidden.includes(home)) {
    lines.push(t("The SIM lists {home} as forbidden; the modem will not try it in automatic mode. Set the PLMN of this profile to {home} to force it.",
      { home: formatPlmn(home) }));
  }
  if (params.nr5g_enabled === false) {
    lines.push(t("5G is disabled in this modem's mode preference."));
  }
  const manual = typeof params.manual_plmn === 'string' ? params.manual_plmn : '';
  const expected = typeof params.expected === 'string' ? params.expected : '';
  if (params.selection === 'manual' && manual && manual !== expected) {
    lines.push(t("The modem is set to manual selection of {plmn}.", { plmn: formatPlmn(manual) }));
  }
  const peer = params.peer && typeof params.peer === 'object' ? params.peer as DataMap : {};
  const peerId = typeof peer.id === 'string' ? peer.id : '';
  const target = typeof params.home === 'string' ? params.home
    : typeof params.expected === 'string' ? params.expected : '';
  if (peerId && target) {
    const record = sources.modems?.find(modem => modem.id === peerId);
    lines.push(t("Another modem on this host ({modem}) is registered on {home} right now, so that network reaches this site.",
      { modem: record ? modemName(record) : peerId, home: formatPlmn(target) }));
  }
  return lines;
}

/** Recovery lines: what a brand-new client found after the saved one timed out. */
function recoveryProbeLines(params: Record<string, unknown>): string[] {
  const lines: string[] = [];
  const escalation = params.escalation && typeof params.escalation === 'object'
    ? params.escalation as DataMap : null;
  if (escalation && escalation.action === 'reset') {
    lines.push(t("The supervisor restarted the modem to clear the old record (enabled in Recovery settings)."));
  }
  const probe = params.probe && typeof params.probe === 'object' ? params.probe as DataMap : null;
  if (probe && probe.modem_answers === false) {
    lines.push(t("The modem does not answer a new client either."));
  } else if (probe && probe.modem_answers === true && probe.packet_status === 'disconnected') {
    const time = typeof probe.at_utc === 'string' ? shortUtcTime(probe.at_utc) : '';
    lines.push(t("A new client reaches the modem and it reports no active data session (checked at {time}).",
      { time }));
  }
  return lines;
}

/** The telemetry feature block, from whichever source carries it first. */
function featureBlocks(sources: IssueSources): DataMap[] {
  return [sources.telemetryFeature, sources.entry?.features, sources.snapshot?.features]
    .filter((block): block is DataMap => Boolean(block) && typeof block === 'object');
}

function telemetryStall(sources: IssueSources): DataMap | null {
  for (const block of featureBlocks(sources)) {
    const telemetry = block.telemetry && typeof block.telemetry === 'object' ? block.telemetry : block;
    if (telemetry.stall && typeof telemetry.stall === 'object') return telemetry.stall;
  }
  return null;
}

/** Every current issue, most severe first: bad, warning, then info. */
export function collectIssues(sources: IssueSources): Issue[] {
  const issues: Issue[] = [];
  const snapshot = sources.snapshot;
  const diagnosis = sources.entry?.diagnosis ?? snapshot?.diagnosis ?? null;
  const modemId = sources.modemId ?? sources.entry?.id ?? 'default';

  const diagnosisValue = diagnosisIssue(
    sources.entry?.diagnosis ?? snapshot?.diagnosis,
    sources.entry?.error ?? snapshot?.error,
    { nextSeconds: sources.nextSeconds, label: sources.label },
  );
  if (diagnosisValue) {
    const code = diagnosisValue.technical?.code ?? '';
    const rawParams = (sources.entry?.diagnosis ?? snapshot?.diagnosis)?.params;
    const params = rawParams && typeof rawParams === 'object' ? rawParams as Record<string, unknown> : {};
    const lines = code.startsWith('sim.') ? simLines(snapshot)
      : code.startsWith('registration.') ? registrationLines(sources, params) : [];
    if (code === 'recovery.saved_client_unanswered') {
      lines.push(...recoveryProbeLines(params));
    }
    if (lines.length) diagnosisValue.lines = lines;
    issues.push(diagnosisValue);
  }

  const stall = telemetryStall(sources);
  if (stall) {
    const reason = typeof stall.reason_code === 'string' ? stall.reason_code : '';
    const step = diagnosis?.step ?? (typeof stall.step === 'string' ? stall.step : '');
    // The step-specific wording needs a step; without one, keep the generic cause.
    const explanation = reason === 'modem_not_readable' && !step
      ? STALL_GENERIC : STALL_EXPLANATION[reason] ?? STALL_GENERIC;
    issues.push({
      id: 'telemetry.stalled',
      tone: 'warning',
      title: 'Monitoring has stored no sample since {time}',
      vars: { time: shortUtcTime(stall.since_utc), ...(step ? { step } : {}) },
      explanation,
    });
  }

  if (sources.config?.telemetry?.enabled === false) {
    issues.push({
      id: 'monitoring.disabled',
      tone: 'info',
      title: 'Monitoring is disabled',
      explanation: 'The session can continue without acquiring new samples.',
    });
  }

  const waitingForRegistration = snapshot?.state === 'WAIT_REGISTRATION';
  const registrationDiagnosis = diagnosisValue?.technical?.code?.startsWith('registration.') === true;
  if (waitingForRegistration && !registrationDiagnosis) {
    issues.push({
      id: 'registration.waiting',
      tone: 'info',
      title: 'Waiting for network registration',
      explanation: 'Available radio and SIM observations are shown while data activation is pending.',
    });
  }

  if (sources.apn?.warning) {
    issues.push({
      id: 'apn.warning',
      tone: sources.apn.tone === 'error' ? 'bad' : 'warning',
      title: sources.apn.warning,
    });
  }

  if (sources.attachFallback) {
    issues.push({ id: 'apn.attach_fallback', tone: 'info', title: sources.attachFallback });
  }

  const conflict = sources.routes?.conflict;
  if (conflict) {
    issues.push({
      id: 'routes.conflict',
      tone: 'warning',
      title: 'Route conflict',
      vars: { network: conflict.network, overlaps: conflict.overlaps, dev: conflict.dev },
      explanation: 'The rest of the mobile network is not reachable through the modem yet: the automatic route to {network} overlaps {overlaps} on {dev}. Check that conflict, then add the network by hand.',
      link: { href: `/configuration/modem/?modem=${encodeURIComponent(modemId)}&section=routing`, label: 'Review the routes' },
    });
  }

  if (sources.routes?.error) {
    issues.push({
      id: 'routes.error',
      tone: 'warning',
      title: 'Some routes through the modem could not be added.',
      link: { href: `/configuration/web/?section=log&modem=${encodeURIComponent(modemId)}`, label: 'Open the system log' },
    });
  }

  if (sources.traffic?.enabled && sources.traffic.error) {
    issues.push({
      id: 'traffic.error',
      tone: 'info',
      title: 'WWAN traffic is not available',
      technical: { detail: sources.traffic.error },
    });
  }

  if (sources.gps?.enabled && sources.gps.error) {
    issues.push({
      id: 'gps.error',
      tone: 'info',
      title: 'GPS is not available',
      technical: { detail: sources.gps.error },
    });
  }

  // Array.prototype.sort is stable: issues of one tone keep the source order above.
  return issues.sort((first, second) => TONE_ORDER[first.tone] - TONE_ORDER[second.tone]);
}
