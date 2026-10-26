'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { useEffect, useMemo, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useRouter } from 'next/navigation';
import { useSupervisor } from '@/components/supervisor-provider';
import { Icon } from '@/components/icon';
import { ChartPanel } from '@/components/chart';
import { KeyValues, Panel, PreservedDetails, RadioCells, SampleStrip, StableText,
  TableWrap, TrafficPanel, ValuesTable } from '@/components/dashboard';
import type { DataMap } from '@/lib/types';
import { sanitizeConfig } from '@/lib/config';
import { SIGNALS, date, finite, flatten, imsiValue, metricLabel, ms, number, object, processLabel,
  resourceProcesses, sampleIsFresh, signalQuality, signalValue, text, trafficPresentation } from '@/lib/metrics';

type Tab = 'charts' | 'data';
type ChartView = 'signal' | 'performance';
let rememberedSearch = '';
let rememberedHideEmpty = false;

function SignalCharts() {
  usePreferences();
  const { snapshot, config, connected, freshness } = useSupervisor();
  const history = Array.isArray(snapshot?.history) ? snapshot.history : [];
  const interval = finite(config?.telemetry?.interval) ? config.telemetry.interval : 1;
  return <div className="metric-stack"><div className="chart-grid metric-signal-detail-grid">
    {SIGNALS.map(signal => {
      const current = connected && freshness.state_fresh === true && sampleIsFresh(freshness.sample_age_ms, interval) ? signalValue(snapshot?.sample?.dynamic, signal.key) : { value: null, rat: null };
      const quality = signalQuality(signal.key, current.value);
      return <ChartPanel key={signal.key} title={signal.label} points={history} interval={interval}
        series={[{ key: signal.key, label: signal.label, unit: signal.unit }]} metric={signal.key} min={signal.min} max={signal.max}
        current={finite(current.value) ? number(current.value) + ' ' + signal.unit : t("Not available")} currentColor={quality?.ink}
        subtitle={quality ? quality.name + ' · ' + current.rat : t("No current sample")} />;
    })}
  </div><RadioCells compact /></div>;
}

function PerformanceCharts() {
  usePreferences();
  const { snapshot, config, resourceHistory } = useSupervisor();
  const telemetry = object(snapshot?.telemetry), timing = object(snapshot?.timing || snapshot?.sample?.timing);
  const history = Array.isArray(snapshot?.history) ? snapshot.history : [], pipeline = Array.isArray(snapshot?.pipeline_history) ? snapshot.pipeline_history : [];
  const interval = finite(config?.telemetry?.interval) ? config.telemetry.interval : 1;
  const traffic = trafficPresentation(snapshot?.sample, config?.telemetry?.enabled !== false && config?.telemetry?.throughput === true);
  return <div className="metric-stack">{traffic.enabled && <TrafficPanel compact />}
    {traffic.enabled && <div className="chart-grid"><ChartPanel title={t("WWAN · Download")} points={history} interval={interval} unit={traffic.unit}
      current={finite(traffic.rx) ? number(traffic.rx, 3) + ' ' + traffic.unit : t("Not available")}
      series={[{ key: 'throughput_download_speed_kbps', label: 'RX', unit: traffic.unit }]} />
      <ChartPanel title={t("WWAN · Upload")} points={history} interval={interval} unit={traffic.unit}
        current={finite(traffic.tx) ? number(traffic.tx, 3) + ' ' + traffic.unit : t("Not available")}
        series={[{ key: 'throughput_upload_speed_kbps', label: 'TX', unit: traffic.unit, color: '#6285c6' }]} /></div>}
    <div className="chart-grid"><ChartPanel title={t("Acquisition duration")} points={history} interval={interval} unit="ms"
      series={[{ key: 'acquisition_duration_ms', label: t("Acquisition") }]} current={ms(timing.acquisition_duration_ms)} subtitle={'P95 ' + ms(telemetry.acquisition_p95_ms)} />
      <ChartPanel title={t("Actual period and lag")} points={history} interval={interval} unit="ms" series={[
        { key: 'effective_interval_ms', label: t("Actual period") }, { key: 'schedule_lag_ms', label: t("Lag") },
      ]} current={ms(timing.effective_interval_ms)} subtitle={t('Configured target:') + ' ' + ms(interval * 1000)} />
    <ChartPanel title={t("Storage queue")} points={pipeline} interval={1} unit={t("samples")}
        series={[{ key: 'pending', label: t("Pending") }]} current={text(telemetry.pending)} subtitle={t('Current age:') + ' ' + ms(telemetry.queue_age_ms)} />
      <ChartPanel title={t("Write duration")} points={pipeline} interval={1} unit="ms" series={[
        { key: 'last_write_duration_ms', label: t("Latest attempt") }, { key: 'last_write_latency_ms', label: t("Queue + attempt") },
      ]} current={ms(telemetry.last_write_duration_ms)} subtitle={t('Write P95:') + ' ' + ms(telemetry.write_p95_ms)} />
    </div>
    <div className="section-heading metric-section-heading"><div><h2>{t("Device resources")}</h2><p className="muted">{t("System sampling every 5 seconds.")}</p></div><Icon name="cpu" size={19} /></div>
    <div className="chart-grid"><ChartPanel title={t("Process CPU")} points={resourceHistory} interval={5} unit="%" series={[
      ...resourceProcesses(object(snapshot?.resources)).map(name => ({ key: name + '_cpu', label: processLabel(name) })),
    ]} subtitle={t("Each process restart breaks its trace.")} /><ChartPanel title={t("Resident memory")} points={resourceHistory} interval={5} unit="MiB" series={[
      ...resourceProcesses(object(snapshot?.resources)).map(name => ({ key: name + '_rss', label: processLabel(name) })),
    ]} subtitle={t("Observed RSS for each process.")} /></div>
    <PreservedDetails id="performance-reference" title={t("Acquisition, storage and resource details")}><div className="metric-explanation">
      <p>{t("Acquisition and write times describe separate operations. The queue is bounded; drops and failures have separate counters.")}</p>
      <p>{t("CSV writes and buffer flushes are not fsync and do not guarantee physical persistence.")}</p>
      <p>{t("100% CPU is one CPU core. A multithreaded process can exceed it. RSS includes shared pages and is not private memory.")}</p>
    </div></PreservedDetails>
  </div>;
}

function GraphMetrics({ initialView = 'signal' }: { initialView?: ChartView }) {
  usePreferences();
  const [view, setView] = useState<ChartView>(initialView);
  useEffect(() => {
    const restore = () => {
      const requested = new URLSearchParams(window.location.search).get('view');
      setView(requested === 'signal' || requested === 'performance' ? requested : initialView);
    };
    restore();
    window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, [initialView]);
  function select(next: ChartView) {
    setView(next);
    const url = new URL(window.location.href);
    url.pathname = '/metrics/charts/';
    url.searchParams.set('view', next);
    window.history.replaceState(window.history.state, '', url);
  }
  function keyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'signal' : event.key === 'End' ? 'performance' : view === 'signal' ? 'performance' : 'signal';
    select(next);
    document.getElementById('metrics-chart-' + next)?.focus();
  }
  return <div className="metric-stack"><div id="metrics-chart-selector" className="metrics-chart-selector" role="tablist" aria-label={t("Chart type")}>
    {([['signal', t("Signal"), 'signal'], ['performance', t("Performance"), 'activity']] as const).map(([key, label, icon]) =>
      <button key={key} id={'metrics-chart-' + key} className="metrics-chart-tab" type="button" role="tab" aria-controls="metrics-chart-panel"
        aria-selected={view === key} tabIndex={view === key ? 0 : -1} onClick={() => select(key)} onKeyDown={keyDown}>
        <Icon name={icon} size={16} />{t(label)}</button>)}
  </div><div id="metrics-chart-panel" role="tabpanel" aria-labelledby={'metrics-chart-' + view} tabIndex={0}>
    {view === 'signal' ? <SignalCharts /> : <PerformanceCharts />}</div></div>;
}

function ResourceData() {
  usePreferences();
  const { snapshot } = useSupervisor();
  const resources = object(snapshot?.resources);
  return <Panel title={t("Resource sampling status")} subtitle={t('Last system reading:') + ' ' + date(resources.sampled_at_utc)}>
    <ValuesTable id="process-resources" values={flatten(resources)} /></Panel>;
}

/** Raw interface counters and their observation window; rates are not derived here. */
function TrafficData() {
  usePreferences();
  const { snapshot } = useSupervisor();
  const metadata = object(object(snapshot?.sample?.collection).interface_traffic);
  return <Panel title={t("Interface traffic")}><ValuesTable id="traffic-data" values={flatten(metadata)} /></Panel>;
}

const groups = [
  { id: 'signal', title: "Signal and radio", match: (key: string) => /signal|rsrp|rsrq|rssi|snr|sinr|rx_chain|tx_power|tx_rx/.test(key) },
  { id: 'cell', title: "Cell and operator", match: (key: string) => key.startsWith('nas_') },
  { id: 'session', title: "Session and counters", match: (key: string) => key.startsWith('wds_') },
  { id: 'gps', title: 'GPS', match: (key: string) => key.startsWith('gpsd_') || key.startsWith('gnss_') },
  { id: 'traffic', title: "WWAN traffic", match: (key: string) => key.startsWith('throughput_') },
  { id: 'other', title: "Other metrics", match: () => true },
];
function DataMetrics() {
  usePreferences();
  const { snapshot, config } = useSupervisor();
  const [search, setSearch] = useState(rememberedSearch), [hideEmpty, setHideEmpty] = useState(rememberedHideEmpty);
  const sample = object(snapshot?.sample), collection = object(sample.collection), dynamic = object(sample.dynamic), units = object(collection.units);
  const timing = object(snapshot?.timing || sample.timing);
  const query = search.toLocaleLowerCase('es-ES').trim(), buckets: DataMap[] = groups.map(() => ({}));
  let count = 0;
  for (const [key, value] of Object.entries(dynamic)) {
    if (hideEmpty && (value === null || value === undefined || value === '')) continue;
    if (query && !key.toLocaleLowerCase('es-ES').includes(query) && !metricLabel(key).toLocaleLowerCase('es-ES').includes(query)) continue;
    buckets[groups.findIndex(group => group.match(key))][key] = value; count++;
  }
  const requests = object(collection.requests), staticRequests = object(collection.static_requests);
  const attach = object(collection.connection?.attach), attachVisible = Object.keys(attach).length > 0;
  const fallbackMessages: Record<string, string> = {
    fallback_unsupported: t("The modem does not support initial attach profile configuration."),
    fallback_information_unavailable: t("The modem does not provide the information needed to configure its initial attach profile."),
    fallback_no_current_profile: t("The modem does not report a current initial attach profile."),
    fallback_pending_only: t("Only a pending initial attach profile is reported."),
    fallback_unsafe_profile: t("This initial attach profile is not changed automatically."),
    fallback_profile_type_unavailable: t("The type needed for initial attach profile configuration is not reported."),
  };
  return <div className="metric-stack"><div className="card metric-search-bar"><div className="field metric-search-field">
    <label htmlFor="metrics-search">{t("Search metrics")}</label><div><Icon name="search" size={17} /><input id="metrics-search" type="search" value={search}
      placeholder={t("Name, identifier or function…")} autoComplete="off" onChange={event => { rememberedSearch = event.currentTarget.value; setSearch(rememberedSearch); }} /></div></div>
    <label className="metric-checkbox"><input type="checkbox" checked={hideEmpty} onChange={event => { rememberedHideEmpty = event.currentTarget.checked; setHideEmpty(rememberedHideEmpty); }} /> {t("Hide unavailable fields")}</label>
    <span className="badge metric-badge-neutral" role="status">{count === 1 ? t("{count} field", { count }) : t("{count} fields", { count })}</span></div>
    <div id="data-groups" className="metric-stack">{groups.map((group, index) => {
      const count = Object.keys(buckets[index]).length;
      const enabled = group.id === 'gps' ? config?.telemetry?.enabled !== false && config?.telemetry?.gps === true : group.id === 'traffic' ? config?.telemetry?.enabled !== false && config?.telemetry?.throughput === true : false;
      if (!count && (query || hideEmpty || !enabled)) return null;
      return <Panel key={group.id} title={group.title} action={<span className="badge metric-badge-neutral">{count}</span>}>
        {count ? <ValuesTable id={'data-group-' + group.id} values={buckets[index]} units={units} /> : <p className="muted">{t("Feature enabled; no data is available yet.")}</p>}</Panel>;
    })}
      {!count && <div className="card empty-state"><Icon name="search" size={25} /><p>{query || hideEmpty ? t("No metrics match the filters.") : t("No dynamic metrics are available.")}</p></div>}
    </div>
    <Panel title={t("Acquisition and scheduling")}><ValuesTable id="data-acquisition" values={{ ...timing,
      configured_interval_ms: finite(config?.telemetry?.interval) ? config.telemetry.interval * 1000 : null,
      skipped_intervals_total: snapshot?.skipped_intervals }} units={{ acquisition_duration_ms: 'ms', effective_interval_ms: 'ms',
        schedule_lag_ms: 'ms', configured_interval_ms: 'ms' }} /></Panel>
    {attachVisible && <PreservedDetails id="attach-details" title={t("Initial attach")}><div className="metric-detail-content"><KeyValues pairs={[
      [t("Profile"), Number.isInteger(attach.profile_id) ? t("Profile {id}", { id: attach.profile_id }) : null],
      [t("Stored profile APN"), typeof attach.profile_apn === 'string' ? attach.profile_apn || t("Blank") : null],
    ]} />{typeof attach.status === 'string' && attach.status.startsWith('fallback_') && <p className="metric-inline-error">
      {fallbackMessages[attach.status] || t("Initial attach profile configuration is unavailable.")}</p>}
      <ValuesTable id="data-attach" values={flatten(attach)} /><p className="chart-note muted">{t("A stored profile APN does not verify the session APN.")}</p></div></PreservedDetails>}
    <Panel title={t("Latest sample queries")} subtitle={t("Each query timestamp identifies its actual observation window.")}>
      <ValuesTable id="sample-queries" values={flatten(requests)} /></Panel>
    {Object.keys(staticRequests).length > 0 && <PreservedDetails id="static-queries" title={t("Cached identity queries")}><div className="metric-detail-content">
      <ValuesTable id="static-queries-table" values={flatten(staticRequests)} /></div></PreservedDetails>}
    <Panel title={t("Storage and destinations")} subtitle={t("The acquired sample ID and latest write attempt ID can differ.")}>
      <ValuesTable id="data-persistence" values={object(snapshot?.telemetry)} /></Panel>
    <ResourceData />
    <Panel title={t("Identity and firmware")}><ValuesTable id="data-static" values={{ ...object(sample.static),
      dms_value_imsi: imsiValue(sample) }} /></Panel>
    <TrafficData />
    <Panel title={t("Sample data")}><PreservedDetails id="sample-json-details" title={t("Latest sample · read-only JSON")}>
      <TableWrap id="sample-json-scroll"><pre className="metric-json"><StableText value={JSON.stringify(sample, null, 2)} /></pre></TableWrap>
    </PreservedDetails></Panel>
    <SavedConfiguration />
  </div>;
}

/** The stored profile as the supervisor applies it; pending drafts and credentials are never shown. */
function SavedConfiguration() {
  usePreferences();
  const { config } = useSupervisor();
  const json = useMemo(() => config ? JSON.stringify(sanitizeConfig(config), null, 2) : '', [config]);
  return <Panel title={t("Saved configuration")} subtitle={t("Read-only JSON. Credentials are omitted.")}>
    <PreservedDetails id="config-json-details" title={t("Show JSON")}>
      <TableWrap id="config-json-scroll"><pre id="config-json" className="metric-json" tabIndex={0} aria-label={t("Complete JSON configuration")}>{json}</pre></TableWrap>
    </PreservedDetails></Panel>;
}

export function Metrics({ tab, initialView }: { tab: Tab; initialView?: ChartView }) {
  usePreferences();
  const { modems, selectedModem } = useSupervisor();
  if (modems.find(entry => entry.id === selectedModem)?.detected === false) return <div className="card empty-state metric-empty">
    <Icon name="modem" size={26} /><p>{t("This modem is not connected. Its data will appear when it is detected again.")}</p></div>;
  return <div className={'metrics-page metrics-' + tab}><SampleStrip />
    {tab === 'charts' ? <GraphMetrics initialView={initialView} /> : <DataMetrics />}</div>;
}

export function LegacyMetricsRedirect({ view }: { view: ChartView }) {
  usePreferences();
  const router = useRouter();
  useEffect(() => {
    router.replace('/metrics/charts/?view=' + view, { scroll: false });
  }, [router, view]);
  return <Metrics tab="charts" initialView={view} />;
}
