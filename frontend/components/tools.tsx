'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { Icon } from '@/components/icon';
import { useSupervisor } from '@/components/supervisor-provider';
import { StatePill, useFleet } from '@/components/fleet';
import { KeyValues } from '@/components/dashboard';
import { FieldError, NumberField, RadioCards, Segmented, SettingRow, Stepper, Switch } from './form-controls';
import { number } from '@/lib/metrics';
import { changed, iperfDefaults, iperfErrors, iperfFromDetail, iperfPayload, iperfSummary, pingDefaults, pingErrors, pingFromDetail,
  pingPayload, pingSummary, reachableHosts } from '@/lib/tools';
import type { IperfForm, PingForm } from '@/lib/tools';
import type { DataMap } from '@/lib/types';

type Tool = 'ping' | 'iperf';
type Status = { running: boolean; kind: Tool | null; detail: DataMap; lines: string[]; returncode: number | null };
type Capabilities = { ping?: boolean; iperf3?: boolean; iperf3_bind_dev?: boolean; ping_options?: string[]; ping_pmtu_probe?: boolean; iperf3_options?: string[]; congestion?: string[] };
type Runner = {
  status: Status | null; capabilities: Capabilities; error: string; busy: boolean;
  start: (body: Record<string, unknown>) => Promise<void>; stop: () => Promise<void>;
};
const tools: { id: Tool; label: string; icon: string }[] = [{ id: 'ping', label: 'Ping', icon: 'activity' }, { id: 'iperf', label: 'iperf3', icon: 'chart' }];
const families = [{ value: 'auto', label: "Automatic" }, { value: '4', label: 'IPv4' }, { value: '6', label: 'IPv6' }];

/** What runs on the host: one test at a time, shared by every modem page. */
function describe(status: Status): string {
  const detail = status.detail ?? {};
  if (status.kind === 'ping') return detail.interface ? t("Ping to {target} from {interface}", { target: String(detail.target ?? ''), interface: String(detail.interface) })
    : t("Ping to {target} through the default route", { target: String(detail.target ?? '') });
  if (status.kind === 'iperf' && detail.mode === 'client') {
    const peer = { target: String(detail.target ?? ''), port: String(detail.port ?? '') };
    const way = detail.direction === 'download' ? t("iperf3 download from {target}:{port}", peer)
      : detail.direction === 'both' ? t("iperf3 both ways with {target}:{port}", peer) : t("iperf3 upload to {target}:{port}", peer);
    const amount = detail.limit === 'bytes' ? detail.bytes + ' MB' : detail.limit === 'blocks' ? t("{count} blocks", { count: String(detail.blocks) }) : detail.duration + ' s';
    return way + ' · ' + String(detail.protocol ?? 'tcp').toUpperCase() + ' · ' + amount;
  }
  if (status.kind === 'iperf') return t("iperf3 server on port {port}", { port: String(detail.port ?? '') });
  return '';
}

/** The single host-wide runner: polled while a test runs, read again when the page opens or comes back. */
function useRunner(): Runner {
  const { session } = useSupervisor();
  const [status, setStatus] = useState<Status | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  const csrf = session?.csrf_token || undefined;
  const call = useCallback(async (path: string, body?: unknown) => {
    const headers: Record<string, string> = {};
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      if (csrf) headers['X-CSRF-Token'] = csrf;
    }
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers,
      body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(String(data.error || "The request could not be completed."));
    return data as { tools: Status; capabilities?: Capabilities };
  }, [csrf]);
  const failed = useCallback((failure: unknown) => { if (live.current) setError(failure instanceof Error ? failure.message : String(failure)); }, []);
  const load = useCallback(async () => {
    try {
      const data = await call('/api/tools');
      if (!live.current) return;
      setStatus(data.tools); setError('');
      if (data.capabilities) setCapabilities(data.capabilities);
    } catch (failure) { failed(failure); }
  }, [call, failed]);
  const running = Boolean(status?.running);
  useEffect(() => {
    live.current = true;
    void load();
    const visible = () => { if (document.visibilityState === 'visible') void load(); };
    document.addEventListener('visibilitychange', visible);
    return () => { live.current = false; document.removeEventListener('visibilitychange', visible); };
  }, [load]);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, 1000);
    return () => window.clearInterval(timer);
  }, [running, load]);
  const act = useCallback(async (path: string, body: unknown) => {
    setBusy(true); setError('');
    try { const data = await call(path, body); if (live.current) setStatus(data.tools); }
    catch (failure) { failed(failure); } finally { if (live.current) setBusy(false); }
  }, [call, failed]);
  return { status, capabilities, error, busy, start: body => act('/api/tools/start', body), stop: () => act('/api/tools/stop', {}) };
}

/** The detail of the test running on the host when it belongs to `tool`; a new value only when another run starts. */
function useRunningDetail(runner: Runner, tool: Tool): DataMap | null {
  const status = runner.status, key = status?.running && status.kind === tool ? JSON.stringify(status.detail ?? {}) : '';
  const [detail, setDetail] = useState<DataMap | null>(null);
  useEffect(() => { setDetail(key ? JSON.parse(key) as DataMap : null); }, [key]);
  return detail;
}

/** One form per tool: the whole state, a setter per field, and the form of a test already running. */
function useForm<T extends object>(defaults: T, running: DataMap | null, fromDetail: (detail: DataMap) => T) {
  const [form, setForm] = useState(defaults);
  // Coming back to a running test shows its own options in the locked form, not the defaults.
  useEffect(() => { if (running) setForm(fromDetail(running)); }, [running, fromDetail]);
  const set = useCallback(<K extends keyof T>(key: K) => (value: T[K]) => setForm(current => ({ ...current, [key]: value })), []);
  return { form, set };
}

/** Card shell shared by both tools: state, rows, the advanced options link, run actions, the summary of the last run and its raw output. */
function ToolCard({ tool, title, icon, runner, valid, missing, hidden, onStart, summary, children }: {
  tool: Tool; title: string; icon: string; runner: Runner; valid: boolean; missing: string | null; hidden: number;
  onStart: () => void; summary: (lines: string[]) => [string, unknown][] | null; children: ReactNode;
}) {
  const { advanced, setAdvanced } = usePreferences();
  const { status, busy, error } = runner;
  const running = Boolean(status?.running), mine = status?.kind === tool, other = running && !mine;
  const lines = mine ? status?.lines ?? [] : [];
  const pairs = mine ? summary(lines) : null;
  const box = useRef<HTMLDivElement>(null), atEnd = useRef(true);
  useLayoutEffect(() => { if (box.current && atEnd.current) box.current.scrollTop = box.current.scrollHeight; }, [lines]);
  return <section className="panel config-panel tools-panel" aria-labelledby={'tool-title-' + tool}>
    <header className="config-card-header">
      <h2 id={'tool-title-' + tool}><Icon name={icon} size={20} />{title}</h2>
      <div className="config-card-actions" role="status"><StatePill state={running && mine ? 'RUNNING' : 'IDLE'} /></div>
    </header>
    {missing && <div className="metric-notice metric-notice-warning tools-notice"><Icon name="alert" size={18} /><span>{t(missing)}</span></div>}
    {other && status && <div className="metric-notice tools-notice"><Icon name="info" size={18} /><span>{t("Another test is running: {test}.", { test: describe(status) })}</span></div>}
    <div className="config-rows" id={'tool-rows-' + tool}>{children}</div>
    {/* The same "show more" as the configuration cards; it lists options and never changes one. */}
    {(advanced || hidden > 0) && <div className="config-advanced">
      <button type="button" id={'tools-advanced-' + tool} className="config-advanced-toggle" aria-expanded={advanced} aria-controls={'tool-rows-' + tool}
        onClick={() => setAdvanced(!advanced)}>
        {advanced ? t("Hide advanced options") : t(hidden === 1 ? "Show {count} advanced option" : "Show {count} advanced options", { count: hidden })}
        <Icon name="chevron-down" size={16} /></button>
    </div>}
    <div className="tools-actions">
      {error && <p className="field-message field-message-error" role="alert"><Icon name="alert-circle" size={15} /><span>{t(error)}</span></p>}
      <span className="tools-current muted">{mine && status ? (running ? '' : t("Last test:") + ' ') + describe(status) : t("No test has run yet.")}</span>
      <div className="button-row">
        <button type="button" id={'tools-stop-' + tool} className="button button-secondary" disabled={!running || busy} onClick={() => void runner.stop()}>{t("Stop")}</button>
        <button type="button" id={'tools-start-' + tool} className="button" disabled={running || busy || !valid || Boolean(missing)}
          onClick={() => { atEnd.current = true; onStart(); }}>{busy && !running && <span className="spinner" />}{t("Start test")}</button>
      </div>
    </div>
    <div className="tools-output">
      <div className="tools-output-head"><h3>{t("Output")}</h3>
        {mine && !running && status?.returncode !== null && status?.returncode !== undefined
          && <span className={'state-pill tone-' + (status.returncode === 0 ? 'good' : 'warning')}>{t("Exit code {code}", { code: status.returncode })}</span>}</div>
      {pairs && pairs.length > 0 && <div className="tools-summary"><KeyValues pairs={pairs} /></div>}
      {lines.length ? <div ref={box} className="table-wrap metrics-table-wrap output-log" tabIndex={0} aria-label={t("Test output")}
        onScroll={event => { const element = event.currentTarget; atEnd.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24; }}>
        <pre className="metric-json">{lines.join('\n')}</pre></div>
        : <p className="muted tools-empty">{t("No output yet.")}</p>}
    </div>
  </section>;
}

/** A labelled field inside a row of two or more values; the note says why it is unavailable. */
function Field({ id, label, error, note, children }: { id: string; label: string; error?: string; note?: string | null; children: ReactNode }) {
  return <div className="field-pair-item" title={note ?? undefined}><label className="field-pair-label" htmlFor={id}>{label}</label>
    <div className="field-control">{children}<FieldError id={id + '-error'} message={error} /></div></div>;
}

/** A number; a blank optional one leaves the tool's default. */
function Amount({ id, value, onChange, unit, min, max, step = 1, placeholder, disabled, error }: {
  id: string; value: number; onChange: (value: number) => void; unit?: string; min: number; max: number; step?: number | 'any';
  placeholder?: string; disabled: boolean; error?: string;
}) {
  return <NumberField id={id} value={value} unit={unit} min={min} max={max} step={step} placeholder={placeholder} disabled={disabled}
    aria-invalid={error ? true : undefined} aria-describedby={id + '-error'} onValueChange={onChange} />;
}

/** One switch of a group of on/off options. */
function Toggle({ id, label, checked, onChange, disabled, note }: {
  id: string; label: string; checked: boolean; onChange: (value: boolean) => void; disabled: boolean; note?: string | null;
}) {
  return <div className="field-pair-item" title={note ?? undefined}><label className="field-pair-label" htmlFor={id}>{label}</label>
    <div className="field-control"><Switch id={id} checked={checked} disabled={disabled} onChange={event => onChange(event.currentTarget.checked)} /></div></div>;
}

/** An option chosen from a short list, as a native select inside the row. */
function Choice({ id, value, options, onChange, disabled }: {
  id: string; value: string; options: { value: string; label: string }[]; onChange: (value: string) => void; disabled: boolean;
}) {
  return <select id={id} value={value} disabled={disabled} onChange={event => onChange(event.currentTarget.value)}>
    {options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>;
}

const ms = (value: number | null) => value === null ? null : number(value, 1) + ' ms';
const rate = (value: number | null) => value === null ? null : number(value, 1) + ' Mbit/s';
const modemHint = (iface: string) => iface ? t("Leaves through {interface}; off, the host's default route is used.", { interface: iface })
  : t("The modem has no network interface yet, so the host's default route is used.");

function PingTool({ runner, iface }: { runner: Runner; iface: string }) {
  const { advanced } = usePreferences();
  const running = useRunningDetail(runner, 'ping');
  const { form, set } = useForm<PingForm>(pingDefaults, running, pingFromDetail);
  const locked = Boolean(runner.status?.running) || runner.busy;
  const errors = pingErrors(form);
  const listed = runner.capabilities.ping_options;
  const has = (option: string) => !listed?.length || listed.includes(option);
  const off = (option: string) => locked || !has(option);
  const note = (option: string) => has(option) ? null : t("Not available in the installed {tool}.", { tool: 'ping' });
  const ipv6 = form.family === '6', useModem = form.viaModem && Boolean(iface);
  const differs = (...keys: (keyof PingForm)[]) => keys.some(key => changed(form, pingDefaults, key));
  // Advanced rows show with the advanced options, and always while they hold a value of their own.
  const rows = { family: differs('family'), fragmentation: differs('fragmentation'), waiting: differs('timeout', 'deadline'), header: differs('ttl', 'tclass'),
    sending: differs('preload', 'sndbuf'), pattern: differs('pattern'), identity: differs('identifier', 'mark'), stamp: differs('timestamp_option'),
    flow: differs('flowlabel'), behaviour: differs('adaptive', 'record_route', 'sticky_source', 'connect', 'no_loopback'),
    output: differs('timestamps', 'outstanding', 'quiet', 'verbose', 'user_latency', 'precise') };
  const show = (row: keyof typeof rows) => advanced || rows[row];
  const hidden = (Object.keys(rows) as (keyof typeof rows)[]).filter(row => !show(row)).length;
  return <ToolCard tool="ping" title="Ping" icon="activity" runner={runner} valid={Object.keys(errors).length === 0} hidden={hidden}
    missing={runner.capabilities.ping === false ? "ping is not installed on this host" : null}
    onStart={() => void runner.start(pingPayload(form, iface))}
    summary={lines => {
      const result = pingSummary(lines);
      if (!result) return null;
      return [[t("Replies"), result.sent === null ? String(result.received) : t("{received} of {sent}", { received: result.received, sent: result.sent })],
        [t("Packet loss"), result.loss === null ? null : number(result.loss, 1) + ' %'], [t("Average latency"), ms(result.avg)],
        [t("Minimum / maximum"), result.min === null || result.max === null ? null : number(result.min, 1) + ' / ' + number(result.max, 1) + ' ms']];
    }}>
    <SettingRow id="tools-target" label={t("Target")} labelFor="tools-target" hint={t("Numeric address or host name.")}>
      <div className="field-control"><input id="tools-target" className="mono" value={form.target} disabled={locked} placeholder="1.1.1.1"
        autoCapitalize="none" autoComplete="off" spellCheck={false} aria-invalid={errors.target ? true : undefined} aria-describedby="tools-target-error"
        onChange={event => set('target')(event.currentTarget.value)} />
        <FieldError id="tools-target-error" message={errors.target} /></div>
    </SettingRow>
    <SettingRow id="tools-ping-rhythm" label={t("Packets and interval")} hint={t("The interval starts at 0.2 s, so a test never floods the link.")}>
      <div className="field-pair">
        <Field id="tools-count" label={t("Packets")} error={errors.count}>
          <Amount id="tools-count" value={form.count} onChange={set('count')} min={1} max={1000} disabled={locked} error={errors.count} /></Field>
        <Field id="tools-ping-interval" label={t("Interval")} error={errors.interval}>
          <Amount id="tools-ping-interval" value={form.interval} onChange={set('interval')} unit="s" min={0.2} max={60} step="any" disabled={off('-i')} error={errors.interval} /></Field>
      </div>
    </SettingRow>
    <SettingRow id="tools-ping-size" label={t("Data size")} labelFor="tools-ping-size" hint={t("56 data bytes travel as 84-byte packets with the IPv4 and ICMP headers.")}>
      <div className="field-control"><Amount id="tools-ping-size" value={form.size} onChange={set('size')} unit="bytes" min={0} max={65507} disabled={off('-s')} error={errors.size} />
        <FieldError id="tools-ping-size-error" message={errors.size} /></div>
    </SettingRow>
    <SettingRow id="tools-interface" label={t("Send through the modem")} labelFor="tools-interface" hint={modemHint(iface)}>
      <div className="field-control"><Switch id="tools-interface" checked={useModem} disabled={locked || !iface} onChange={event => set('viaModem')(event.currentTarget.checked)} /></div>
    </SettingRow>
    {show('family') && <SettingRow id="tools-ping-family" label={t("IP version")} hint={t("Which address of a host name is tried.")}>
      <div className="field-control"><Segmented id="tools-ping-family" value={form.family} options={families} disabled={locked} labelledBy="tools-ping-family-label"
        onChange={value => set('family')(value as PingForm['family'])} /></div>
    </SettingRow>}
    {show('fragmentation') && <SettingRow id="tools-ping-fragmentation" label={t("Fragmentation")} labelFor="tools-ping-fragmentation"
      hint={note('-M') ?? t("Don't fragment with a large data size finds the path MTU.")}>
      <div className="field-control"><Choice id="tools-ping-fragmentation" value={form.fragmentation} disabled={off('-M')} onChange={value => set('fragmentation')(value as PingForm['fragmentation'])}
        options={[{ value: '', label: t("Default") }, { value: 'do', label: t("Don't fragment (DF)") }, { value: 'want', label: t("Discover the path MTU") },
          { value: 'dont', label: t("Allow fragmentation") },
          ...(runner.capabilities.ping_pmtu_probe === false ? [] : [{ value: 'probe', label: t("Probe beyond the path MTU") }])]} /></div>
    </SettingRow>}
    {show('waiting') && <SettingRow id="tools-ping-waiting" label={t("Waiting")} hint={t("How long to wait for each reply, and for the whole test.")}>
      <div className="field-pair">
        <Field id="tools-ping-timeout" label={t("Reply timeout")} error={errors.timeout} note={note('-W')}>
          <Amount id="tools-ping-timeout" value={form.timeout} onChange={set('timeout')} unit="s" min={1} max={60} placeholder={t("Default")} disabled={off('-W')} error={errors.timeout} /></Field>
        <Field id="tools-ping-deadline" label={t("Deadline")} error={errors.deadline} note={note('-w')}>
          <Amount id="tools-ping-deadline" value={form.deadline} onChange={set('deadline')} unit="s" min={1} max={3600} placeholder={t("No limit")} disabled={off('-w')} error={errors.deadline} /></Field>
      </div>
    </SettingRow>}
    {show('header') && <SettingRow id="tools-ping-header" label={t("IP header")} hint={t("TTL limits the hops; the service class sets the QoS bits.")}>
      <div className="field-pair">
        <Field id="tools-ping-ttl" label="TTL" error={errors.ttl} note={note('-t')}>
          <Amount id="tools-ping-ttl" value={form.ttl} onChange={set('ttl')} min={1} max={255} placeholder={t("Default")} disabled={off('-t')} error={errors.ttl} /></Field>
        <Field id="tools-ping-tclass" label={t("Service class")} error={errors.tclass} note={note('-Q')}>
          <Amount id="tools-ping-tclass" value={form.tclass} onChange={set('tclass')} min={0} max={255} placeholder="0" disabled={off('-Q')} error={errors.tclass} /></Field>
      </div>
    </SettingRow>}
    {show('sending') && <SettingRow id="tools-ping-sending" label={t("Sending")} hint={t("Preload sends a few packets without waiting for replies; the buffer sizes the socket.")}>
      <div className="field-pair">
        <Field id="tools-ping-preload" label={t("Preload")} error={errors.preload} note={note('-l')}>
          <Amount id="tools-ping-preload" value={form.preload} onChange={set('preload')} min={1} max={3} placeholder={t("No preload")} disabled={off('-l')} error={errors.preload} /></Field>
        <Field id="tools-ping-sndbuf" label={t("Send buffer")} error={errors.sndbuf} note={note('-S')}>
          <Amount id="tools-ping-sndbuf" value={form.sndbuf} onChange={set('sndbuf')} unit="bytes" min={1} max={1048576} placeholder={t("Default")} disabled={off('-S')} error={errors.sndbuf} /></Field>
      </div>
    </SettingRow>}
    {show('pattern') && <SettingRow id="tools-ping-pattern" label={t("Padding pattern")} labelFor="tools-ping-pattern" hint={note('-p') ?? t("Hexadecimal bytes that fill the data, to expose data-dependent faults.")}>
      <div className="field-control"><input id="tools-ping-pattern" className="mono" value={form.pattern} maxLength={32} placeholder="ff00" disabled={off('-p')}
        autoCapitalize="none" autoComplete="off" spellCheck={false} aria-invalid={errors.pattern ? true : undefined} aria-describedby="tools-ping-pattern-error"
        onChange={event => set('pattern')(event.currentTarget.value.trim())} />
        <FieldError id="tools-ping-pattern-error" message={errors.pattern} /></div>
    </SettingRow>}
    {show('identity') && <SettingRow id="tools-ping-identity" label={t("Identification")} hint={t("The ICMP identifier of the session, and the fwmark that routing rules can match.")}>
      <div className="field-pair">
        <Field id="tools-ping-identifier" label={t("ICMP identifier")} error={errors.identifier} note={note('-e')}>
          <Amount id="tools-ping-identifier" value={form.identifier} onChange={set('identifier')} min={0} max={65535} placeholder={t("Default")} disabled={off('-e')} error={errors.identifier} /></Field>
        <Field id="tools-ping-mark" label={t("Packet mark")} error={errors.mark} note={note('-m')}>
          <Amount id="tools-ping-mark" value={form.mark} onChange={set('mark')} min={0} max={4294967295} placeholder={t("No mark")} disabled={off('-m')} error={errors.mark} /></Field>
      </div>
    </SettingRow>}
    {show('stamp') && <SettingRow id="tools-ping-stamp" label={t("IP timestamps (IPv4)")} labelFor="tools-ping-stamp" hint={note('-T') ?? t("Asks each router to stamp the packet; many ignore it.")}>
      <div className="field-control"><Choice id="tools-ping-stamp" value={form.timestamp_option} disabled={off('-T') || ipv6} onChange={value => set('timestamp_option')(value as PingForm['timestamp_option'])}
        options={[{ value: '', label: t("No IP timestamps") }, { value: 'tsonly', label: t("Timestamps only") }, { value: 'tsandaddr', label: t("Timestamps and addresses") }]} /></div>
    </SettingRow>}
    {show('flow') && <SettingRow id="tools-ping-flow" label={t("Flow label (IPv6)")} labelFor="tools-ping-flow" hint={note('-F') ?? t("Used with the IPv6 version only.")}>
      <div className="field-control"><Amount id="tools-ping-flow" value={form.flowlabel} onChange={set('flowlabel')} min={0} max={1048575} placeholder={t("Random")} disabled={off('-F') || !ipv6} error={errors.flowlabel} />
        <FieldError id="tools-ping-flow-error" message={errors.flowlabel} /></div>
    </SettingRow>}
    {show('behaviour') && <SettingRow id="tools-ping-behaviour" label={t("Behaviour")}>
      <div className="field-pair field-pair-switches">
        <Toggle id="tools-ping-adaptive" label={t("Adaptive interval")} checked={form.adaptive} onChange={set('adaptive')} disabled={off('-A')} note={note('-A')} />
        <Toggle id="tools-ping-route" label={t("Record route (IPv4)")} checked={form.record_route} onChange={set('record_route')} disabled={off('-R') || ipv6} note={note('-R')} />
        <Toggle id="tools-ping-sticky" label={t("Fixed source address")} checked={form.sticky_source} onChange={set('sticky_source')} disabled={off('-B')} note={note('-B')} />
        <Toggle id="tools-ping-connect" label={t("Connected socket")} checked={form.connect} onChange={set('connect')} disabled={off('-C')} note={note('-C')} />
        <Toggle id="tools-ping-loopback" label={t("No multicast loopback")} checked={form.no_loopback} onChange={set('no_loopback')} disabled={off('-L')} note={note('-L')} />
      </div>
    </SettingRow>}
    {show('output') && <SettingRow id="tools-ping-output" label={t("Output")}>
      <div className="field-pair field-pair-switches">
        <Toggle id="tools-ping-timestamps" label={t("Timestamps")} checked={form.timestamps} onChange={set('timestamps')} disabled={off('-D')} note={note('-D')} />
        <Toggle id="tools-ping-outstanding" label={t("Missing replies")} checked={form.outstanding} onChange={set('outstanding')} disabled={off('-O')} note={note('-O')} />
        <Toggle id="tools-ping-quiet" label={t("Summary only")} checked={form.quiet} onChange={set('quiet')} disabled={off('-q')} note={note('-q')} />
        <Toggle id="tools-ping-verbose" label={t("Verbose")} checked={form.verbose} onChange={set('verbose')} disabled={off('-v')} note={note('-v')} />
        <Toggle id="tools-ping-user" label={t("User-to-user latency")} checked={form.user_latency} onChange={set('user_latency')} disabled={off('-U')} note={note('-U')} />
        <Toggle id="tools-ping-precise" label={t("Unrounded times")} checked={form.precise} onChange={set('precise')} disabled={off('-3')} note={note('-3')} />
      </div>
    </SettingRow>}
  </ToolCard>;
}

function IperfTool({ runner, iface }: { runner: Runner; iface: string }) {
  const { advanced } = usePreferences();
  const { listenUrls } = useSupervisor();
  const running = useRunningDetail(runner, 'iperf');
  const { form, set } = useForm<IperfForm>(iperfDefaults, running, iperfFromDetail);
  const locked = Boolean(runner.status?.running) || runner.busy;
  const errors = iperfErrors(form);
  const listed = runner.capabilities.iperf3_options;
  const has = (option: string) => !listed?.length || listed.includes(option);
  const off = (option: string) => locked || !has(option);
  const note = (option: string) => has(option) ? null : t("Not available in the installed {tool}.", { tool: 'iperf3' });
  const canBind = runner.capabilities.iperf3_bind_dev !== false;
  const client = form.mode === 'client', udp = form.protocol === 'udp', tcp = form.protocol === 'tcp', ipv6 = form.family === '6', paced = form.bitrate > 0;
  const congestion = runner.capabilities.congestion ?? [];
  const differs = (...keys: (keyof IperfForm)[]) => keys.some(key => changed(form, iperfDefaults, key));
  const common = { family: differs('family'), timeouts: differs('rcv_timeout', 'snd_timeout'), keepalive: differs('cntl_ka'), units: differs('format'),
    output: differs('timestamps', 'verbose', 'debug') };
  const rows: Record<string, boolean> = client ? { ...common, reports: differs('interval', 'omit'), buffers: differs('length', 'window'), tcp: differs('mss', 'congestion'),
    tcpSwitches: differs('nodelay', 'zerocopy', 'mptcp'), sctp: differs('nstreams'), header: differs('tos', 'flowlabel'), connection: differs('cport', 'connect_timeout'),
    pacing: differs('pacing_timer', 'fq_rate'), burst: differs('burst'), udpSwitches: differs('counters64', 'dont_fragment', 'gsro'),
    data: differs('repeating_payload', 'server_output', 'skip_rx_copy') }
    : { ...common, listen: differs('listenModem'), interval: differs('interval'), serverRate: differs('server_bitrate_limit', 'server_rate_interval'),
      limits: differs('idle_timeout', 'server_max_duration'), mptcp: differs('mptcp') };
  const show = (row: string) => advanced || Boolean(rows[row]);
  const hidden = Object.keys(rows).filter(row => !show(row)).length;
  const hosts = reachableHosts(listenUrls);
  const directions = [{ value: 'upload', label: "Upload" }, { value: 'download', label: "Download" }, ...(has('--bidir') ? [{ value: 'both', label: "Both ways" }] : [])];
  const protocols = [{ value: 'tcp', label: 'TCP' }, { value: 'udp', label: 'UDP' }, ...(has('--sctp') ? [{ value: 'sctp', label: 'SCTP' }] : [])];
  const limitField = form.limit === 'time' ? 'duration' : form.limit;
  return <ToolCard tool="iperf" title="iperf3" icon="chart" runner={runner} valid={Object.keys(errors).length === 0} hidden={hidden}
    missing={runner.capabilities.iperf3 === false ? "iperf3 is not installed on this host" : null}
    // Without --bind-dev the test follows the default route, as the disabled switch says.
    onStart={() => void runner.start(iperfPayload(form, canBind ? iface : ''))}
    summary={lines => {
      const result = iperfSummary(lines);
      if (!result) return null;
      if (result.transmit !== null || result.receive !== null) return [[t("Sent by this device"), rate(result.transmit)], [t("Received by this device"), rate(result.receive)]];
      if (result.currentTransmit !== null || result.currentReceive !== null) return [[t("Sending now"), rate(result.currentTransmit)], [t("Receiving now"), rate(result.currentReceive)]];
      if (result.sender === null && result.receiver === null) return [[t("Throughput now"), rate(result.current)]];
      const pairs: [string, unknown][] = [[t("Sender"), rate(result.sender)], [t("Receiver"), rate(result.receiver)]];
      if (result.jitter !== null) pairs.push([t("Jitter"), ms(result.jitter)]);
      if (result.loss !== null) pairs.push([t("Datagram loss"), number(result.loss, 2) + ' %']);
      return pairs;
    }}>
    <SettingRow id="tools-iperf-mode" label={t("Mode")}>
      <div className="field-control"><RadioCards id="tools-iperf-mode" value={form.mode} disabled={locked} grid labelledBy="tools-iperf-mode-label"
        onChange={value => set('mode')(value as IperfForm['mode'])} options={[
          { value: 'client', label: t("Client"), description: t("This device measures against an iperf3 server, through the modem.") },
          { value: 'server', label: t("Server"), description: t("Another device measures against this one.") },
        ]} /></div>
    </SettingRow>
    {client ? <>
      <SettingRow id="tools-iperf-server" label={t("iperf3 server")} hint={t("Address and port where the other side runs iperf3 -s.")}>
        <div className="field-pair tools-host-port">
          <Field id="tools-iperf-host" label={t("Host")} error={form.host ? errors.host : undefined}>
            <input id="tools-iperf-host" className="mono" value={form.host} disabled={locked} placeholder="iperf.example.org" autoCapitalize="none" autoComplete="off"
              spellCheck={false} aria-invalid={errors.host && form.host ? true : undefined} aria-describedby="tools-iperf-host-error" onChange={event => set('host')(event.currentTarget.value)} /></Field>
          <Field id="tools-iperf-port" label={t("Port")} error={errors.port}>
            <Amount id="tools-iperf-port" value={form.port} onChange={set('port')} min={1} max={65535} disabled={locked} error={errors.port} /></Field>
        </div>
      </SettingRow>
      <SettingRow id="tools-iperf-direction" label={t("Direction")} hint={t(form.direction === 'download' ? "The server sends and this device receives: the download through the modem."
        : form.direction === 'both' ? "Both ends send at once: upload and download together." : "This device sends to the server: the upload through the modem.")}>
        <div className="field-control"><Segmented id="tools-iperf-direction" value={form.direction} disabled={locked} labelledBy="tools-iperf-direction-label"
          options={directions} onChange={value => set('direction')(value as IperfForm['direction'])} /></div>
      </SettingRow>
      <SettingRow id="tools-iperf-protocol" label={t("Protocol")} hint={t(udp ? "UDP sends at a fixed rate and reports jitter and lost datagrams."
        : form.protocol === 'sctp' ? "SCTP needs SCTP support on both ends." : "TCP measures the throughput the link sustains.")}>
        <div className="field-control"><Segmented id="tools-iperf-protocol" value={form.protocol} disabled={locked} labelledBy="tools-iperf-protocol-label"
          options={protocols} onChange={value => set('protocol')(value as IperfForm['protocol'])} /></div>
      </SettingRow>
      <SettingRow id="tools-iperf-rate" label={t("Target rate")} labelFor="tools-iperf-bitrate" hint={t(udp ? "Blank keeps the iperf3 default of 1 Mbit/s for UDP."
        : "Blank or 0 lets the test go as fast as the link allows.")}>
        <div className="field-control"><Amount id="tools-iperf-bitrate" value={form.bitrate} onChange={set('bitrate')} unit="Mbit/s" min={0} max={100000} step="any"
          placeholder={udp ? '1' : t("No limit")} disabled={locked} error={errors.bitrate} /><FieldError id="tools-iperf-bitrate-error" message={errors.bitrate} /></div>
      </SettingRow>
      <SettingRow id="tools-iperf-limit" label={t("End of the test")} hint={t("After a time, an amount of data or a number of blocks.")}>
        <div className="field-control field-stack">
          <Segmented id="tools-iperf-limit" value={form.limit} disabled={locked} labelledBy="tools-iperf-limit-label" onChange={value => set('limit')(value as IperfForm['limit'])}
            options={[{ value: 'time', label: "Time" }, { value: 'bytes', label: "Data" }, { value: 'blocks', label: "Blocks" }]} />
          {form.limit === 'time' ? <Amount id="tools-iperf-duration" value={form.duration} onChange={set('duration')} unit="s" min={1} max={3600} disabled={locked} error={errors.duration} />
            : form.limit === 'bytes' ? <Amount id="tools-iperf-bytes" value={form.bytes} onChange={set('bytes')} unit="MB" min={1} max={1000000} disabled={off('--bytes')} error={errors.bytes} />
              : <Amount id="tools-iperf-blocks" value={form.blocks} onChange={set('blocks')} unit="blocks" min={1} max={100000000} disabled={off('--blockcount')} error={errors.blocks} />}
          <FieldError id={'tools-iperf-' + limitField + '-error'} message={errors[limitField]} />
        </div>
      </SettingRow>
      <SettingRow id="tools-iperf-parallel" label={t("Parallel streams")} labelFor="tools-iperf-parallel" hint={t("Several streams help to fill a fast link.")}>
        <div className="field-control"><Stepper id="tools-iperf-parallel" value={form.parallel} min={1} max={32} disabled={locked} onValueChange={set('parallel')} /></div>
      </SettingRow>
      <SettingRow id="tools-iperf-interface" label={t("Send through the modem")} labelFor="tools-iperf-interface" hint={!canBind
        ? t("This iperf3 cannot choose the interface (it needs version 3.10 or later), so the host's default route is used.") : modemHint(iface)}>
        <div className="field-control"><Switch id="tools-iperf-interface" checked={form.viaModem && Boolean(iface) && canBind} disabled={locked || !iface || !canBind}
          onChange={event => set('viaModem')(event.currentTarget.checked)} /></div>
      </SettingRow>
    </> : <>
      <SettingRow id="tools-iperf-listen" label={t("Server port")} labelFor="tools-iperf-listen">
        <div className="field-control"><Amount id="tools-iperf-listen" value={form.port} onChange={set('port')} min={1024} max={65535} disabled={locked} error={errors.port} />
          <FieldError id="tools-iperf-listen-error" message={errors.port} /></div>
      </SettingRow>
      <SettingRow id="tools-iperf-oneoff" label={t("One test then stop")} labelFor="tools-iperf-oneoff" hint={t(form.one_off
        ? "The server takes a single test and closes." : "The server keeps accepting tests until you stop it.")}>
        <div className="field-control"><Switch id="tools-iperf-oneoff" checked={form.one_off} disabled={locked} onChange={event => set('one_off')(event.currentTarget.checked)} /></div>
      </SettingRow>
      <SettingRow id="tools-iperf-command" label={t("On the other device")} hint={t("Add -R to measure the download from this device instead.")}>
        <ul className="tools-commands">{(hosts.length ? hosts : [t("this-device-address")]).map(address =>
          <li key={address}><code className="mono">iperf3 -c {address} -p {Number.isInteger(form.port) ? form.port : 5201}</code></li>)}</ul>
      </SettingRow>
    </>}
    {show('family') && <SettingRow id="tools-iperf-family" label={t("IP version")} hint={note('--version4') ?? t("Which address of a host name is used.")}>
      <div className="field-control"><Segmented id="tools-iperf-family" value={form.family} options={families} disabled={off('--version4')} labelledBy="tools-iperf-family-label"
        onChange={value => set('family')(value as IperfForm['family'])} /></div>
    </SettingRow>}
    {client && show('reports') && <SettingRow id="tools-iperf-reports" label={t("Reports")} hint={t("Seconds between reports (0 turns them off), and the first seconds left out of the result while TCP ramps up.")}>
      <div className="field-pair">
        <Field id="tools-iperf-interval" label={t("Report interval")} error={errors.interval} note={note('--interval')}>
          <Amount id="tools-iperf-interval" value={form.interval} onChange={set('interval')} unit="s" min={0} max={60} step="any" disabled={off('--interval')} error={errors.interval} /></Field>
        <Field id="tools-iperf-omit" label={t("Omit the first")} error={errors.omit} note={note('--omit')}>
          <Amount id="tools-iperf-omit" value={form.omit} onChange={set('omit')} unit="s" min={0} max={60} placeholder="0" disabled={off('--omit')} error={errors.omit} /></Field>
      </div>
    </SettingRow>}
    {!client && show('listen') && <SettingRow id="tools-iperf-listen-modem" label={t("Listen only on the modem")} labelFor="tools-iperf-listen-modem" hint={!canBind
      ? t("This iperf3 cannot choose the interface (it needs version 3.10 or later).") : iface ? t("Accepts tests arriving through {interface} only.", { interface: iface })
        : t("The modem has no network interface yet.")}>
      <div className="field-control"><Switch id="tools-iperf-listen-modem" checked={form.listenModem && Boolean(iface) && canBind} disabled={locked || !iface || !canBind}
        onChange={event => set('listenModem')(event.currentTarget.checked)} /></div>
    </SettingRow>}
    {!client && show('interval') && <SettingRow id="tools-iperf-server-interval" label={t("Report interval")} labelFor="tools-iperf-interval" hint={t("Seconds between reports; 0 turns them off.")}>
      <div className="field-control"><Amount id="tools-iperf-interval" value={form.interval} onChange={set('interval')} unit="s" min={0} max={60} step="any" disabled={off('--interval')} error={errors.interval} />
        <FieldError id="tools-iperf-interval-error" message={errors.interval} /></div>
    </SettingRow>}
    {!client && show('serverRate') && <SettingRow id="tools-iperf-server-rate" label={t("Server rate limit")} hint={note('--server-bitrate-limit') ?? t("Stops a test whose total rate, averaged over the given time, exceeds the limit.")}>
      <div className="field-pair">
        <Field id="tools-iperf-server-rate" label={t("Limit")} error={errors.server_bitrate_limit}>
          <Amount id="tools-iperf-server-rate" value={form.server_bitrate_limit} onChange={set('server_bitrate_limit')} unit="Mbit/s" min={0.001} max={100000} step="any"
            placeholder={t("No limit")} disabled={off('--server-bitrate-limit')} error={errors.server_bitrate_limit} /></Field>
        <Field id="tools-iperf-server-rate-interval" label={t("Averaged over")} error={errors.server_rate_interval}>
          <Amount id="tools-iperf-server-rate-interval" value={form.server_rate_interval} onChange={set('server_rate_interval')} unit="s" min={1} max={60} placeholder="5"
            disabled={off('--server-bitrate-limit') || Number.isNaN(form.server_bitrate_limit)} error={errors.server_rate_interval} /></Field>
      </div>
    </SettingRow>}
    {!client && show('limits') && <SettingRow id="tools-iperf-limits" label={t("Server time limits")} hint={t("The server restarts after this long without a test, and stops a test that runs longer than the maximum.")}>
      <div className="field-pair">
        <Field id="tools-iperf-idle" label={t("Idle timeout")} error={errors.idle_timeout} note={note('--idle-timeout')}>
          <Amount id="tools-iperf-idle" value={form.idle_timeout} onChange={set('idle_timeout')} unit="s" min={1} max={86400} placeholder={t("No limit")}
            disabled={off('--idle-timeout')} error={errors.idle_timeout} /></Field>
        <Field id="tools-iperf-max-duration" label={t("Maximum test duration")} error={errors.server_max_duration} note={note('--server-max-duration')}>
          <Amount id="tools-iperf-max-duration" value={form.server_max_duration} onChange={set('server_max_duration')} unit="s" min={1} max={86400} placeholder={t("No limit")}
            disabled={off('--server-max-duration')} error={errors.server_max_duration} /></Field>
      </div>
    </SettingRow>}
    {!client && show('mptcp') && <SettingRow id="tools-iperf-server-mptcp" label="MPTCP" labelFor="tools-iperf-server-mptcp-switch" hint={note('--mptcp') ?? t("Accepts Multipath TCP tests, when the kernel supports them.")}>
      <div className="field-control"><Switch id="tools-iperf-server-mptcp-switch" checked={form.mptcp} disabled={off('--mptcp')} onChange={event => set('mptcp')(event.currentTarget.checked)} /></div>
    </SettingRow>}
    {client && show('buffers') && <SettingRow id="tools-iperf-buffers" label={t("Buffers")} hint={t("Bytes per read or write (the datagram size with UDP), and the socket buffer, which sets the TCP window.")}>
      <div className="field-pair">
        <Field id="tools-iperf-length" label={t("Buffer length")} error={errors.length} note={note('--length')}>
          <Amount id="tools-iperf-length" value={form.length} onChange={set('length')} unit="bytes" min={16} max={udp ? 65507 : 1048576} placeholder={t("Default")} disabled={off('--length')} error={errors.length} /></Field>
        <Field id="tools-iperf-window" label={t("Socket buffer")} error={errors.window} note={note('--window')}>
          <Amount id="tools-iperf-window" value={form.window} onChange={set('window')} unit="KB" min={1} max={524288} placeholder={t("Default")} disabled={off('--window')} error={errors.window} /></Field>
      </div>
    </SettingRow>}
    {client && show('tcp') && <SettingRow id="tools-iperf-tcp" label="TCP" hint={t("Maximum segment size and congestion control, for TCP tests.")}>
      <div className="field-pair">
        <Field id="tools-iperf-mss" label="MSS" error={errors.mss} note={note('--set-mss')}>
          <Amount id="tools-iperf-mss" value={form.mss} onChange={set('mss')} unit="bytes" min={88} max={9216} placeholder={t("Default")} disabled={off('--set-mss') || udp} error={errors.mss} /></Field>
        <Field id="tools-iperf-congestion" label={t("Congestion control")} note={note('--congestion')}>
          <Choice id="tools-iperf-congestion" value={form.congestion} disabled={off('--congestion') || !tcp} onChange={set('congestion')}
            options={[{ value: '', label: t("Default") }, ...congestion.map(algorithm => ({ value: algorithm, label: algorithm }))]} /></Field>
      </div>
    </SettingRow>}
    {client && show('tcpSwitches') && <SettingRow id="tools-iperf-tcp-options" label={t("TCP options")}>
      <div className="field-pair field-pair-switches">
        <Toggle id="tools-iperf-nodelay" label={t("No delay (Nagle off)")} checked={form.nodelay} onChange={set('nodelay')} disabled={off('--no-delay') || udp} note={note('--no-delay')} />
        <Toggle id="tools-iperf-zerocopy" label={t("Zero copy")} checked={form.zerocopy} onChange={set('zerocopy')} disabled={off('--zerocopy') || !tcp} note={note('--zerocopy')} />
        <Toggle id="tools-iperf-mptcp" label="MPTCP" checked={form.mptcp} onChange={set('mptcp')} disabled={off('--mptcp') || !tcp} note={note('--mptcp')} />
      </div>
    </SettingRow>}
    {client && show('sctp') && <SettingRow id="tools-iperf-sctp" label={t("SCTP streams")} labelFor="tools-iperf-nstreams" hint={note('--nstreams') ?? t("Streams of the association, for SCTP tests.")}>
      <div className="field-control"><Amount id="tools-iperf-nstreams" value={form.nstreams} onChange={set('nstreams')} min={1} max={1024} placeholder="1"
        disabled={off('--nstreams') || form.protocol !== 'sctp'} error={errors.nstreams} /><FieldError id="tools-iperf-nstreams-error" message={errors.nstreams} /></div>
    </SettingRow>}
    {client && show('header') && <SettingRow id="tools-iperf-header" label={t("IP header")} hint={t("Type of service (DSCP × 4, e.g. 184 for EF) and the IPv6 flow label.")}>
      <div className="field-pair">
        <Field id="tools-iperf-tos" label="TOS" error={errors.tos} note={note('--tos')}>
          <Amount id="tools-iperf-tos" value={form.tos} onChange={set('tos')} min={0} max={255} placeholder="0" disabled={off('--tos')} error={errors.tos} /></Field>
        <Field id="tools-iperf-flowlabel" label={t("Flow label (IPv6)")} error={errors.flowlabel} note={note('--flowlabel')}>
          <Amount id="tools-iperf-flowlabel" value={form.flowlabel} onChange={set('flowlabel')} min={0} max={1048575} placeholder={t("No label")} disabled={off('--flowlabel') || !ipv6} error={errors.flowlabel} /></Field>
      </div>
    </SettingRow>}
    {client && show('connection') && <SettingRow id="tools-iperf-connection" label={t("Connection")} hint={t("A fixed local port, for firewalls, and how long to wait for the server.")}>
      <div className="field-pair">
        <Field id="tools-iperf-cport" label={t("Client port")} error={errors.cport} note={note('--cport')}>
          <Amount id="tools-iperf-cport" value={form.cport} onChange={set('cport')} min={1} max={65535} placeholder={t("Any")} disabled={off('--cport')} error={errors.cport} /></Field>
        <Field id="tools-iperf-connect" label={t("Connect timeout")} error={errors.connect_timeout} note={note('--connect-timeout')}>
          <Amount id="tools-iperf-connect" value={form.connect_timeout} onChange={set('connect_timeout')} unit="ms" min={1} max={60000} placeholder={t("Default")} disabled={off('--connect-timeout')} error={errors.connect_timeout} /></Field>
      </div>
    </SettingRow>}
    {show('timeouts') && <SettingRow id="tools-iperf-timeouts" label={t("Timeouts")} hint={t("A test stops when no data arrives for this long; the send timeout drops a TCP connection whose data goes unacknowledged.")}>
      <div className="field-pair">
        <Field id="tools-iperf-rcv-timeout" label={t("Receive timeout")} error={errors.rcv_timeout} note={note('--rcv-timeout')}>
          <Amount id="tools-iperf-rcv-timeout" value={form.rcv_timeout} onChange={set('rcv_timeout')} unit="ms" min={100} max={3600000} placeholder="120000" disabled={off('--rcv-timeout')} error={errors.rcv_timeout} /></Field>
        <Field id="tools-iperf-snd-timeout" label={t("TCP send timeout")} error={errors.snd_timeout} note={note('--snd-timeout')}>
          <Amount id="tools-iperf-snd-timeout" value={form.snd_timeout} onChange={set('snd_timeout')} unit="ms" min={1} max={3600000} placeholder={t("Default")} disabled={off('--snd-timeout')} error={errors.snd_timeout} /></Field>
      </div>
    </SettingRow>}
    {show('keepalive') && <SettingRow id="tools-iperf-keepalive" label={t("Control keepalive")} labelFor="tools-iperf-cntl-ka"
      hint={note('--cntl-ka') ?? t("TCP keepalive on the control connection, so the mobile network does not drop it during a long test.")}>
      <div className="field-control"><Switch id="tools-iperf-cntl-ka" checked={form.cntl_ka} disabled={off('--cntl-ka')} onChange={event => set('cntl_ka')(event.currentTarget.checked)} /></div>
    </SettingRow>}
    {client && show('pacing') && <SettingRow id="tools-iperf-pacing" label={t("Pacing")} hint={t("How often the rate is enforced, and kernel fair-queuing pacing (Linux).")}>
      <div className="field-pair">
        <Field id="tools-iperf-pacing-timer" label={t("Pacing timer")} error={errors.pacing_timer} note={note('--pacing-timer')}>
          <Amount id="tools-iperf-pacing-timer" value={form.pacing_timer} onChange={set('pacing_timer')} unit="µs" min={1} max={1000000} placeholder="1000" disabled={off('--pacing-timer')} error={errors.pacing_timer} /></Field>
        <Field id="tools-iperf-fq" label={t("Fair-queuing rate")} error={errors.fq_rate} note={note('--fq-rate')}>
          <Amount id="tools-iperf-fq" value={form.fq_rate} onChange={set('fq_rate')} unit="Mbit/s" min={0.001} max={100000} step="any" placeholder={t("No limit")} disabled={off('--fq-rate')} error={errors.fq_rate} /></Field>
      </div>
    </SettingRow>}
    {client && show('burst') && <SettingRow id="tools-iperf-bursts" label={t("Bursts")} labelFor="tools-iperf-burst" hint={t(paced
      ? "Packets sent together at the target rate." : "Packets sent together at the target rate; set a target rate to use bursts.")}>
      <div className="field-control"><Amount id="tools-iperf-burst" value={form.burst} onChange={set('burst')} unit="packets" min={1} max={1000} placeholder={t("No bursts")}
        disabled={locked || !paced} error={errors.burst} /><FieldError id="tools-iperf-burst-error" message={errors.burst} /></div>
    </SettingRow>}
    {client && show('udpSwitches') && <SettingRow id="tools-iperf-udp-options" label={t("UDP options")}>
      <div className="field-pair field-pair-switches">
        <Toggle id="tools-iperf-counters" label={t("64-bit counters")} checked={form.counters64} onChange={set('counters64')} disabled={off('--udp-counters-64bit') || !udp} note={note('--udp-counters-64bit')} />
        <Toggle id="tools-iperf-df" label={t("Don't fragment")} checked={form.dont_fragment} onChange={set('dont_fragment')} disabled={off('--dont-fragment') || !udp} note={note('--dont-fragment')} />
        <Toggle id="tools-iperf-gsro" label="GSO/GRO" checked={form.gsro} onChange={set('gsro')} disabled={off('--gsro') || !udp} note={note('--gsro')} />
      </div>
    </SettingRow>}
    {client && show('data') && <SettingRow id="tools-iperf-data" label={t("Data and results")}>
      <div className="field-pair field-pair-switches">
        <Toggle id="tools-iperf-repeating" label={t("Repeating payload")} checked={form.repeating_payload} onChange={set('repeating_payload')} disabled={off('--repeating-payload')} note={note('--repeating-payload')} />
        <Toggle id="tools-iperf-server-output" label={t("Server results")} checked={form.server_output} onChange={set('server_output')} disabled={off('--get-server-output')} note={note('--get-server-output')} />
        <Toggle id="tools-iperf-skip-rx" label={t("Discard received data")} checked={form.skip_rx_copy} onChange={set('skip_rx_copy')} disabled={off('--skip-rx-copy')} note={note('--skip-rx-copy')} />
      </div>
    </SettingRow>}
    {show('units') && <SettingRow id="tools-iperf-units" label={t("Units")} labelFor="tools-iperf-units" hint={t("How rates are written in the output.")}>
      <div className="field-control"><Choice id="tools-iperf-units" value={form.format} disabled={locked} onChange={value => set('format')(value as IperfForm['format'])}
        options={[{ value: 'k', label: 'Kbit/s' }, { value: 'm', label: 'Mbit/s' }, { value: 'g', label: 'Gbit/s' }, { value: 't', label: 'Tbit/s' }]} /></div>
    </SettingRow>}
    {show('output') && <SettingRow id="tools-iperf-output" label={t("Output")}>
      <div className="field-pair field-pair-switches">
        <Toggle id="tools-iperf-timestamps" label={t("Timestamps")} checked={form.timestamps} onChange={set('timestamps')} disabled={off('--timestamps')} note={note('--timestamps')} />
        <Toggle id="tools-iperf-verbose" label={t("Verbose")} checked={form.verbose} onChange={set('verbose')} disabled={off('--verbose')} note={note('--verbose')} />
        <Toggle id="tools-iperf-debug" label={t("Debug")} checked={form.debug} onChange={set('debug')} disabled={off('--debug')} note={note('--debug')} />
      </div>
    </SettingRow>}
  </ToolCard>;
}

/** Network tests through the selected modem: ping, or iperf3 as client or server. */
export function ModemTools() {
  usePreferences();
  const { selectedModem, snapshot } = useSupervisor();
  // useFleet also covers a backend without fleet inventory, where the default modem comes from the snapshot.
  const entry = useFleet().find(item => item.id === selectedModem);
  const iface = String(entry?.interface || entry?.device?.interface || snapshot?.network?.interface || snapshot?.device?.interface || '');
  const runner = useRunner();
  const [tool, setTool] = useState<Tool>('ping');
  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get('tool');
    if (requested === 'ping' || requested === 'iperf') setTool(requested);
  }, []);
  // A test already running when the page opens wins over the remembered tab: its form is the one that is locked.
  const adopted = useRef(false);
  useEffect(() => {
    if (adopted.current || !runner.status) return;
    adopted.current = true;
    if (runner.status.running && runner.status.kind) setTool(runner.status.kind);
  }, [runner.status]);
  const select = (next: Tool) => {
    setTool(next);
    const url = new URL(window.location.href);
    url.searchParams.set('tool', next);
    window.history.replaceState(window.history.state, '', url);
  };
  const keyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next: Tool = event.key === 'Home' ? 'ping' : event.key === 'End' ? 'iperf' : tool === 'ping' ? 'iperf' : 'ping';
    select(next);
    document.getElementById('tool-' + next)?.focus();
  };
  if (entry && entry.detected === false) return <div className="card empty-state metric-empty">
    <Icon name="modem" size={26} /><p>{t("This modem is not connected. Its network tests will be available when it is detected again.")}</p></div>;
  return <div className="tools-page">
    <div id="tools-selector" className="metrics-chart-selector" role="tablist" aria-label={t("Network test")}>
      {tools.map(item => <button key={item.id} id={'tool-' + item.id} type="button" role="tab" className="metrics-chart-tab"
        aria-selected={tool === item.id} aria-controls={'tool-panel-' + item.id} tabIndex={tool === item.id ? 0 : -1}
        onClick={() => select(item.id)} onKeyDown={keyDown}><Icon name={item.icon} size={16} />{item.label}</button>)}
    </div>
    {/* Both panels stay mounted so each form keeps its values while the other tool is shown. */}
    {tools.map(item => <div key={item.id} id={'tool-panel-' + item.id} role="tabpanel" aria-labelledby={'tool-' + item.id} hidden={tool !== item.id}>
      {item.id === 'ping' ? <PingTool runner={runner} iface={iface} /> : <IperfTool runner={runner} iface={iface} />}
    </div>)}
  </div>;
}
