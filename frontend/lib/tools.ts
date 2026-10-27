import { t } from './messages.ts';

/** Summaries read from the raw output of the network tests; the output itself is shown untouched. */
export type PingSummary = { sent: number | null; received: number; loss: number | null; min: number | null; avg: number | null; max: number | null; last: number | null };
export type IperfSummary = {
  current: number | null; sender: number | null; receiver: number | null; jitter: number | null; loss: number | null;
  /** Bidirectional tests: what this device sent and received, per interval and at the end. */
  transmit: number | null; receive: number | null; currentTransmit: number | null; currentReceive: number | null;
};

const REPLY = /bytes from .*time[=<]([\d.]+) ?ms/;
// iputils and BusyBox word the statistics slightly differently.
const TOTALS = /(\d+) packets transmitted, (\d+) (?:packets )?received(?:, \+\d+ errors)?, ([\d.]+)% packet loss/;
const RTT = /(?:rtt|round-trip) min\/avg\/max(?:\/(?:mdev|stddev))? = ([\d.]+)\/([\d.]+)\/([\d.]+)/;

export function pingSummary(lines: readonly string[]): PingSummary | null {
  const times = lines.map(line => REPLY.exec(line)).filter(Boolean).map(match => Number(match![1]));
  const totals = lines.map(line => TOTALS.exec(line)).filter(Boolean).at(-1);
  const rtt = lines.map(line => RTT.exec(line)).filter(Boolean).at(-1);
  if (!times.length && !totals) return null;
  // Until the statistics arrive, the replies so far give a running picture.
  const average = times.length ? times.reduce((sum, value) => sum + value, 0) / times.length : null;
  return {
    sent: totals ? Number(totals[1]) : null,
    received: totals ? Number(totals[2]) : times.length,
    loss: totals ? Number(totals[3]) : null,
    min: rtt ? Number(rtt[1]) : times.length ? Math.min(...times) : null,
    avg: rtt ? Number(rtt[2]) : average,
    max: rtt ? Number(rtt[3]) : times.length ? Math.max(...times) : null,
    last: times.at(-1) ?? null,
  };
}

const RATE = /([\d.]+)\s+([KMGT]?)bits\/sec/;
// Lines may start with a --timestamps prefix, and bidirectional tests tag each stream [TX-C] or [RX-C].
const INTERVAL = /\[\s*(?:\d+|SUM)\](?:\[(TX|RX)-[CS]\])?\s+[\d.]+-[\d.]+\s+sec/;

/** Megabits per second, whatever unit iperf3 chose for the line. */
export function megabits(line: string): number | null {
  const match = RATE.exec(line);
  if (!match) return null;
  const value = Number(match[1]);
  const scale: Record<string, number> = { '': 1e-6, K: 1e-3, M: 1, G: 1e3, T: 1e6 };
  return value * scale[match[2]];
}

/** The final sender and receiver lines (the [SUM] line wins with parallel streams, as it is printed last). */
export function iperfSummary(lines: readonly string[]): IperfSummary | null {
  const result: IperfSummary = { current: null, sender: null, receiver: null, jitter: null, loss: null,
    transmit: null, receive: null, currentTransmit: null, currentReceive: null };
  let seen = false;
  for (const line of lines) {
    const interval = INTERVAL.exec(line);
    if (!interval) continue;
    const rate = megabits(line);
    if (rate === null) continue;
    seen = true;
    const role = /\b(sender|receiver)\s*$/.exec(line)?.[1], tag = interval[1];
    if (tag) {
      // The receiving end measured what was delivered, so its line wins over the sender's.
      const key = tag === 'TX' ? (role ? 'transmit' : 'currentTransmit') : (role ? 'receive' : 'currentReceive');
      if (!role || role === 'receiver' || result[key] === null) result[key] = rate;
      continue;
    }
    if (!role) { result.current = rate; continue; }
    result[role as 'sender' | 'receiver'] = rate;
    if (role === 'receiver') {
      const jitter = /([\d.]+) ms/.exec(line), loss = /\(([\d.]+)%\)/.exec(line);
      if (jitter) result.jitter = Number(jitter[1]);
      if (loss) result.loss = Number(loss[1]);
    }
  }
  return seen ? result : null;
}

/** LAN addresses that another device can use to reach this one, from the published web URLs. */
export function reachableHosts(urls: readonly string[]): string[] {
  const hosts = urls.map(url => { try { return new URL(url).hostname.replace(/^\[|\]$/g, ''); } catch { return ''; } })
    .filter(host => host && host !== 'localhost' && !/^127\./.test(host) && host !== '::1');
  return [...new Set(hosts)];
}

/* Forms. NaN marks a number left blank: the tool then keeps its own default. */
export type Family = 'auto' | '4' | '6';
export type PingForm = {
  target: string; count: number; interval: number; size: number; viaModem: boolean; family: Family;
  fragmentation: '' | 'do' | 'want' | 'dont' | 'probe'; ttl: number; tclass: number; timeout: number; deadline: number; preload: number;
  pattern: string; sndbuf: number; mark: number; identifier: number; timestamp_option: '' | 'tsonly' | 'tsandaddr'; flowlabel: number;
  timestamps: boolean; outstanding: boolean; quiet: boolean; verbose: boolean; user_latency: boolean; precise: boolean;
  adaptive: boolean; record_route: boolean; sticky_source: boolean; connect: boolean; no_loopback: boolean;
};
export const pingDefaults: PingForm = {
  target: '1.1.1.1', count: 4, interval: 1, size: 56, viaModem: true, family: 'auto', fragmentation: '', ttl: NaN, tclass: NaN,
  timeout: NaN, deadline: NaN, preload: NaN, pattern: '', sndbuf: NaN, mark: NaN, identifier: NaN, timestamp_option: '', flowlabel: NaN,
  timestamps: false, outstanding: false, quiet: false, verbose: false, user_latency: false, precise: false, adaptive: false, record_route: false,
  sticky_source: false, connect: false, no_loopback: false,
};

export type IperfForm = {
  mode: 'client' | 'server'; host: string; port: number; direction: 'upload' | 'download' | 'both'; protocol: 'tcp' | 'udp' | 'sctp';
  bitrate: number; burst: number; limit: 'time' | 'bytes' | 'blocks'; duration: number; bytes: number; blocks: number; parallel: number;
  viaModem: boolean; family: Family; interval: number; omit: number; length: number; window: number; mss: number; nodelay: boolean;
  congestion: string; zerocopy: boolean; mptcp: boolean; nstreams: number; tos: number; flowlabel: number; cport: number;
  connect_timeout: number; rcv_timeout: number; snd_timeout: number; cntl_ka: boolean; pacing_timer: number; fq_rate: number;
  server_output: boolean; repeating_payload: boolean; skip_rx_copy: boolean; counters64: boolean; dont_fragment: boolean; gsro: boolean;
  format: 'k' | 'm' | 'g' | 't'; timestamps: boolean; verbose: boolean; debug: boolean;
  one_off: boolean; listenModem: boolean; server_bitrate_limit: number; server_rate_interval: number; idle_timeout: number;
  server_max_duration: number;
};
export const iperfDefaults: IperfForm = {
  mode: 'client', host: '', port: 5201, direction: 'upload', protocol: 'tcp', bitrate: NaN, burst: NaN, limit: 'time', duration: 10,
  bytes: 100, blocks: 10000, parallel: 1, viaModem: true, family: 'auto', interval: 1, omit: NaN, length: NaN, window: NaN, mss: NaN,
  nodelay: false, congestion: '', zerocopy: false, mptcp: false, nstreams: NaN, tos: NaN, flowlabel: NaN, cport: NaN, connect_timeout: NaN,
  rcv_timeout: NaN, snd_timeout: NaN, cntl_ka: false, pacing_timer: NaN, fq_rate: NaN, server_output: false, repeating_payload: false,
  skip_rx_copy: false, counters64: false, dont_fragment: false, gsro: false, format: 'm', timestamps: false, verbose: false, debug: false,
  one_off: true, listenModem: false, server_bitrate_limit: NaN, server_rate_interval: NaN, idle_timeout: NaN, server_max_duration: NaN,
};

/** True when a field differs from its default, so an advanced row holding it is never hidden. */
export const changed = <T extends object>(form: T, defaults: T, key: keyof T) => !Object.is(form[key], defaults[key]);
const blank = (value: unknown) => value === '' || value === false || (typeof value === 'number' && Number.isNaN(value));

/** The options that differ from their defaults; untouched options are left to the tool. */
function differing<T extends object>(form: T, defaults: T, keys: readonly (keyof T)[]) {
  return Object.fromEntries(keys.filter(key => changed(form, defaults, key) && !blank(form[key])).map(key => [key, form[key]]));
}

const PING_OPTIONS = ['interval', 'size', 'family', 'fragmentation', 'ttl', 'tclass', 'timeout', 'deadline', 'preload', 'pattern', 'sndbuf', 'mark',
  'identifier', 'timestamp_option', 'flowlabel', 'timestamps', 'outstanding', 'quiet', 'verbose', 'user_latency', 'precise', 'adaptive',
  'record_route', 'sticky_source', 'connect', 'no_loopback'] as const;

export function pingPayload(form: PingForm, iface: string): Record<string, unknown> {
  const options = differing(form, pingDefaults, PING_OPTIONS);
  // Options of the other IP version would be rejected; they are left out instead.
  if (form.family !== '6') delete options.flowlabel;
  if (form.family === '6') { delete options.record_route; delete options.timestamp_option; }
  return { kind: 'ping', target: form.target.trim(), count: form.count, interface: form.viaModem && iface ? iface : null, ...options };
}

const COMMON_OPTIONS = ['family', 'interval', 'rcv_timeout', 'snd_timeout', 'cntl_ka', 'format', 'timestamps', 'verbose', 'debug'] as const;
const CLIENT_OPTIONS = [...COMMON_OPTIONS, 'bitrate', 'omit', 'length', 'window', 'tos', 'cport', 'connect_timeout', 'pacing_timer', 'fq_rate',
  'server_output', 'repeating_payload', 'skip_rx_copy'] as const;
const SERVER_OPTIONS = [...COMMON_OPTIONS, 'server_bitrate_limit', 'idle_timeout', 'server_max_duration', 'mptcp'] as const;

export function iperfPayload(form: IperfForm, iface: string): Record<string, unknown> {
  if (form.mode === 'server') {
    const payload: Record<string, unknown> = { kind: 'iperf', mode: 'server', port: form.port, ...(form.one_off ? {} : { one_off: false }),
      interface: form.listenModem && iface ? iface : null, ...differing(form, iperfDefaults, SERVER_OPTIONS) };
    // The averaging time only qualifies a rate limit, as bursts only qualify a target rate.
    if ('server_bitrate_limit' in payload) Object.assign(payload, differing(form, iperfDefaults, ['server_rate_interval']));
    return payload;
  }
  const payload: Record<string, unknown> = { kind: 'iperf', mode: 'client', target: form.host.trim(), port: form.port,
    interface: form.viaModem && iface ? iface : null, ...differing(form, iperfDefaults, CLIENT_OPTIONS) };
  if (form.bitrate > 0) Object.assign(payload, differing(form, iperfDefaults, ['burst']));
  if (form.direction !== 'upload') payload.direction = form.direction;
  if (form.protocol !== 'tcp') payload.protocol = form.protocol;
  if (form.limit === 'time') payload.duration = form.duration;
  else { payload.limit = form.limit; payload[form.limit] = form[form.limit]; }
  if (form.parallel !== 1) payload.parallel = form.parallel;
  // Protocol-specific options only travel with their protocol.
  const own = form.protocol === 'tcp' ? ['mss', 'nodelay', 'congestion', 'zerocopy', 'mptcp'] : form.protocol === 'sctp' ? ['mss', 'nodelay', 'nstreams']
    : ['counters64', 'dont_fragment', 'gsro'];
  Object.assign(payload, differing(form, iperfDefaults, own as (keyof IperfForm)[]));
  if (form.family === '6') Object.assign(payload, differing(form, iperfDefaults, ['flowlabel']));
  return payload;
}

/** "Use a number between …", the same bounds as modules/web/tools.py; blank optional numbers pass. */
function bounded(value: number, min: number, max: number, { whole = true, optional = false } = {}): string | null {
  if (Number.isNaN(value)) return optional ? null : t("Use a number between {min} and {max}.", { min, max });
  if (whole && !Number.isInteger(value)) return t("Use a whole number between {min} and {max}.", { min, max });
  return value < min || value > max ? t("Use a number between {min} and {max}.", { min, max }) : null;
}
const HOST = /^[A-Za-z0-9.:-]{1,253}$/;
const hostError = (value: string, required: boolean) => !value.trim() ? (required ? t("A host is required.") : null)
  : HOST.test(value.trim()) ? null : t("Use a numeric address or a host name, without spaces.");

export function pingErrors(form: PingForm): Record<string, string> {
  const errors: Record<string, string | null> = {
    target: hostError(form.target, true), count: bounded(form.count, 1, 1000), interval: bounded(form.interval, 0.2, 60, { whole: false }),
    size: bounded(form.size, 0, 65507), ttl: bounded(form.ttl, 1, 255, { optional: true }), tclass: bounded(form.tclass, 0, 255, { optional: true }),
    timeout: bounded(form.timeout, 1, 60, { optional: true }), deadline: bounded(form.deadline, 1, 3600, { optional: true }),
    preload: bounded(form.preload, 1, 3, { optional: true }), sndbuf: bounded(form.sndbuf, 1, 1048576, { optional: true }),
    mark: bounded(form.mark, 0, 4294967295, { optional: true }), identifier: bounded(form.identifier, 0, 65535, { optional: true }),
    flowlabel: bounded(form.flowlabel, 0, 1048575, { optional: true }),
    pattern: form.pattern && !/^[0-9A-Fa-f]{1,32}$/.test(form.pattern) ? t("Use up to 32 hexadecimal digits.") : null,
  };
  return Object.fromEntries(Object.entries(errors).filter((entry): entry is [string, string] => entry[1] !== null));
}

export function iperfErrors(form: IperfForm): Record<string, string> {
  const client = form.mode === 'client';
  // iperf3 reports every 0.1 to 60 s, or never with 0.
  const common = { interval: form.interval === 0 ? null : bounded(form.interval, 0.1, 60, { whole: false, optional: true }),
    rcv_timeout: bounded(form.rcv_timeout, 100, 3600000, { optional: true }), snd_timeout: bounded(form.snd_timeout, 1, 3600000, { optional: true }) };
  const errors: Record<string, string | null> = client ? { ...common,
    host: hostError(form.host, true), port: bounded(form.port, 1, 65535), bitrate: bounded(form.bitrate, 0, 100000, { whole: false, optional: true }),
    duration: form.limit === 'time' ? bounded(form.duration, 0, 3600) : null, bytes: form.limit === 'bytes' ? bounded(form.bytes, 1, 1000000) : null,
    blocks: form.limit === 'blocks' ? bounded(form.blocks, 1, 100000000) : null, parallel: bounded(form.parallel, 1, 32),
    burst: form.bitrate > 0 ? bounded(form.burst, 1, 1000, { optional: true }) : null, omit: bounded(form.omit, 0, 60, { optional: true }),
    length: bounded(form.length, 16, form.protocol === 'udp' ? 65507 : 1048576, { optional: true }), window: bounded(form.window, 1, 524288, { optional: true }),
    mss: form.protocol === 'udp' ? null : bounded(form.mss, 88, 9216, { optional: true }), tos: bounded(form.tos, 0, 255, { optional: true }),
    nstreams: form.protocol === 'sctp' ? bounded(form.nstreams, 1, 1024, { optional: true }) : null,
    flowlabel: form.family === '6' ? bounded(form.flowlabel, 0, 1048575, { optional: true }) : null, cport: bounded(form.cport, 1, 65535, { optional: true }),
    connect_timeout: bounded(form.connect_timeout, 1, 60000, { optional: true }), pacing_timer: bounded(form.pacing_timer, 1, 1000000, { optional: true }),
    fq_rate: bounded(form.fq_rate, 0.001, 100000, { whole: false, optional: true }),
  } : { ...common,
    port: bounded(form.port, 1024, 65535), server_bitrate_limit: bounded(form.server_bitrate_limit, 0.001, 100000, { whole: false, optional: true }),
    server_rate_interval: Number.isNaN(form.server_bitrate_limit) ? null : bounded(form.server_rate_interval, 1, 60, { optional: true }),
    idle_timeout: bounded(form.idle_timeout, 1, 86400, { optional: true }), server_max_duration: bounded(form.server_max_duration, 1, 86400, { optional: true }),
  };
  return Object.fromEntries(Object.entries(errors).filter((entry): entry is [string, string] => entry[1] !== null));
}

/** The form of a test already running on the host, from the options it was started with. */
export function pingFromDetail(detail: Record<string, unknown>): PingForm {
  const form: PingForm = { ...pingDefaults, viaModem: Boolean(detail.interface) };
  for (const key of Object.keys(pingDefaults) as (keyof PingForm)[]) {
    if (key in detail && typeof detail[key] === typeof pingDefaults[key]) (form as Record<string, unknown>)[key] = detail[key];
  }
  return form;
}

export function iperfFromDetail(detail: Record<string, unknown>): IperfForm {
  const server = detail.mode === 'server';
  const form: IperfForm = { ...iperfDefaults, mode: server ? 'server' : 'client', host: typeof detail.target === 'string' ? detail.target : '',
    viaModem: !server && Boolean(detail.interface), listenModem: server && Boolean(detail.interface) };
  for (const key of Object.keys(iperfDefaults) as (keyof IperfForm)[]) {
    if (key in detail && key !== 'mode' && typeof detail[key] === typeof iperfDefaults[key]) (form as Record<string, unknown>)[key] = detail[key];
  }
  return form;
}
