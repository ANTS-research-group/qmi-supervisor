'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import type { KeyboardEvent, ReactNode } from 'react';
import { useSupervisor } from '@/components/supervisor-provider';
import { SystemLog } from '@/components/system-logs';
import { configFields, customFromProfile, fieldAvailability, fieldDefinition, queryGroups, queryPeriods, scopedChanges, scopedErrors } from '@/lib/config';
import type { FieldDefinition, SaveScope } from '@/lib/config';
import { acquisitionBudget, displayValue, hasChange, hiddenRowCount, identificationMode, listenMode, plmnMode, plmnParts, queryCost,
  routeRole, routeRoleChanges, routeRoles, rowPresent, rowVisible, sectionFields, sectionsFor, valueAt, visibleSections } from '@/lib/config-layout';
import type { IdentificationMode, ListenMode, PlmnMode, RouteRole, RowDefinition, SectionDefinition, VisibilityContext } from '@/lib/config-layout';
import type { ConfigValue } from '@/lib/types';
import { ChipList, ErrorCount, FieldError, ModifiedBadge, NumberField, PasswordInput, PendingDot, RadioCards, Segmented, SettingRow, Stepper, Switch } from './form-controls';
import { Icon } from './icon';

const remembered: Record<SaveScope, string | null> = { profile: null, web: null, account: null };
const sectionListeners = new Set<(scope: SaveScope, id: string) => void>();

/** A link to a section of the page already open does not remount the workbench, so it asks for the section here. */
export function requestConfigSection(scope: SaveScope, id: string) {
  remembered[scope] = id;
  for (const listener of sectionListeners) listener(scope, id);
}

const fieldId = (path: string) => 'field-' + path.replace('.', '-');
const unitLabel = (definition: FieldDefinition) => definition.unit === 's' ? <span className="sr-only"> ({t("seconds")})</span> : null;
const groupLabels: Record<string, string> = { mobile: "Mobile network", telemetry: "Telemetry" };

function Origin({ paths }: { paths: string[] }) {
  usePreferences();
  const { config, draft } = useSupervisor();
  const changed = paths.filter(path => valueAt(config, path) !== undefined && !Object.is(valueAt(config, path), valueAt(draft, path)));
  if (!changed.length) return null;
  if (changed.length > 1) return <ModifiedBadge />;
  const shown = displayValue(changed[0], valueAt(config, changed[0]));
  return <ModifiedBadge previous={shown.translate ? t(shown.text) : shown.text} />;
}

function FieldControl({ path, labelledBy }: { path: string; labelledBy?: string }) {
  usePreferences();
  const { draft, edit, errors, editable, saving } = useSupervisor();
  const value = valueAt(draft, path);
  if (value === undefined) return null;
  const [section, key] = path.split('.');
  const definition = fieldDefinition(section, key, value);
  const id = fieldId(path), error = errors[path], availability = fieldAvailability(draft, section, key);
  // The API validates retained settings too. A pending invalid value stays
  // editable so switching a feature off can never trap an uncorrectable error.
  const disabled = !editable || saving || (availability.disabled && !error);
  const reason = availability.disabled ? (error ? "Correct this value before saving." : availability.reason) : null;
  const describedBy = [reason ? id + '-note' : '', id + '-error'].filter(Boolean).join(' ');
  const common = { id, name: path, disabled, 'data-setting': path, 'aria-invalid': error ? true : undefined, 'aria-describedby': describedBy };
  const set = (next: ConfigValue) => edit(section, key, next);
  const note = reason ? <p id={id + '-note'} className="field-message">{t(reason)}</p> : null;
  if (definition.control === 'chips') return <div className="field-control" data-field={path}>
    <ChipList id={id} name={path} value={value} onChange={set} disabled={disabled} invalid={Boolean(error)} describedBy={describedBy}
      setting={path} fieldError={error} errorId={id + '-error'} kind={definition.chips} />{note}</div>;
  let control: ReactNode;
  if (definition.kind === 'boolean') control = <Switch {...common} checked={value === true} onChange={event => set(event.currentTarget.checked)} />;
  else if (definition.control === 'segmented') control = <Segmented id={id} value={value} options={definition.options ?? []} onChange={set}
    disabled={disabled} labelledBy={labelledBy} describedBy={describedBy} invalid={Boolean(error)} setting={path} />;
  else if (definition.kind === 'select') control = <select {...common} value={String(value)} onChange={event => set(event.currentTarget.value)}>
    {definition.options?.map(option => <option key={option.value} value={option.value}>{t(option.label)}</option>)}</select>;
  else if (definition.control === 'stepper') control = <Stepper {...common} value={value} min={definition.min ?? 0} max={definition.max ?? 100} onValueChange={set} />;
  else if (definition.kind === 'number' || definition.kind === 'integer') control = <NumberField {...common} value={value} unit={definition.unit}
    duration={definition.duration} min={definition.min ?? (definition.positive ? 0 : undefined)} max={definition.max}
    step={definition.kind === 'integer' ? 1 : 'any'} onValueChange={set} />;
  else control = <input {...common} type="text" className={definition.mono ? 'mono' : undefined} value={String(value)} autoCapitalize="none"
    spellCheck={false} autoComplete={path === 'web.username' ? 'username' : 'off'} onChange={event => set(event.currentTarget.value)} />;
  return <div className="field-control" data-field={path}>{control}{note}<FieldError id={id + '-error'} message={error} /></div>;
}

function FieldRow({ row, path }: { row: RowDefinition; path: string }) {
  usePreferences();
  const { draft } = useSupervisor();
  const [section, key] = path.split('.');
  const definition = fieldDefinition(section, key, valueAt(draft, path)!);
  const id = 'row-' + row.id, grouped = definition.control === 'segmented', hint = row.hint ?? definition.hint;
  return <SettingRow id={id} label={<>{t(row.label ?? definition.label)}{unitLabel(definition)}</>} labelFor={grouped ? undefined : fieldId(path)}
    hint={hint ? t(hint) : undefined} badge={<Origin paths={[path]} />}>
    <FieldControl path={path} labelledBy={grouped ? id + '-label' : undefined} />
  </SettingRow>;
}

/** Related values share one row, each with a short label of its own. */
function PairRow({ row, paths }: { row: RowDefinition; paths: string[] }) {
  usePreferences();
  const { draft } = useSupervisor();
  const switches = paths.every(path => typeof valueAt(draft, path) === 'boolean');
  return <SettingRow id={'row-' + row.id} label={t(row.label ?? '')} hint={row.hint ? t(row.hint) : undefined} badge={<Origin paths={paths} />}>
    <div className={'field-pair' + (switches ? ' field-pair-switches' : '')}>
      {paths.map(path => {
        const [section, key] = path.split('.'), definition = fieldDefinition(section, key, valueAt(draft, path)!);
        return <div className="field-pair-item" key={path}>
          <label className="field-pair-label" htmlFor={fieldId(path)}>{t(definition.label)}{unitLabel(definition)}</label>
          <FieldControl path={path} />
        </div>;
      })}
    </div>
  </SettingRow>;
}

function RouteRoleRow({ row }: { row: RowDefinition }) {
  usePreferences();
  const { draft, config, edit, editable, saving, errors } = useSupervisor();
  const role = routeRole(draft?.network), saved = routeRole(config?.network);
  const label = (value: RouteRole) => t(routeRoles.find(option => option.value === value)!.label);
  const error = errors['network.route_policy'] ?? errors['network.default_route'];
  return <SettingRow id={'row-' + row.id} label={t(row.label!)} hint={t(row.hint!)} badge={config && role !== saved ? <ModifiedBadge previous={label(saved)} /> : null}>
    <div className="field-control" data-field="network.route_policy">
      {/* Choosing a role also re-enables the default route, so the cards are never disabled by it. */}
      <RadioCards id="route-role" value={role} grid disabled={!editable || saving} labelledBy={'row-' + row.id + '-label'}
        describedBy="route-role-error" setting="network.default_route network.route_policy"
        options={routeRoles.map(option => ({ value: option.value, label: t(option.label), description: t(option.description) }))}
        onChange={next => { for (const [key, value] of Object.entries(routeRoleChanges(next as RouteRole))) edit('network', key, value); }} />
      <FieldError id="route-role-error" message={error} />
    </div>
  </SettingRow>;
}

function IdentifyRow({ row }: { row: RowDefinition }) {
  usePreferences();
  const { draft, config, edit, editable, saving, snapshot, modems, selectedModem } = useSupervisor();
  const modem = draft?.modem ?? {};
  const [choice, setChoice] = useState<IdentificationMode | null>(null);
  useEffect(() => { setChoice(null); }, [config?.modem?.device, config?.modem?.serial]);
  const mode = choice ?? identificationMode(modem);
  const detected = modems.find(entry => entry.id === selectedModem)?.device ?? snapshot?.device ?? null;
  const detectedSerial = typeof detected?.serial === 'string' ? detected.serial : '';
  const detectedPath = typeof detected?.path === 'string' ? detected.path : '';
  const choose = (next: IdentificationMode) => {
    setChoice(next);
    if (next === 'auto') { edit('modem', 'device', 'auto'); edit('modem', 'serial', ''); return; }
    if (next === 'serial') {
      edit('modem', 'device', 'auto');
      const serial = String(modem.serial || detectedSerial);
      if (serial) edit('modem', 'serial', serial);
      return;
    }
    const path = modem.device && modem.device !== 'auto' ? String(modem.device) : detectedPath;
    if (path) edit('modem', 'device', path);
  };
  const saved = identificationMode(config?.modem);
  const names: Record<IdentificationMode, string> = { auto: t("Automatic"), serial: t("USB serial number"), port: t("Control port") };
  return <SettingRow id={'row-' + row.id} label={t(row.label!)} hint={t(row.hint!)}
    badge={config && saved !== identificationMode(modem) ? <ModifiedBadge previous={names[saved]} /> : <Origin paths={row.fields} />}>
    <div className="field-control field-stack">
      <RadioCards id="identify" value={mode} disabled={!editable || saving} labelledBy={'row-' + row.id + '-label'} setting="modem.device modem.serial"
        onChange={next => choose(next as IdentificationMode)} options={[
          { value: 'auto', label: names.auto, description: t("Any compatible modem not reserved by another profile.") },
          { value: 'serial', label: names.serial, description: <>{t("Stable even if the USB port changes.")}
            {detectedSerial && <> {t("Detected:")} <span className="mono">{detectedSerial}</span></>}</> },
          { value: 'port', label: names.port, description: t("A fixed path. Needed when two modems share a serial number.") },
        ]} />
      {mode !== 'auto' && <div className="field-inline-group">
        {mode === 'port' && <div className="field-inline"><label htmlFor="field-modem-device">{t("Control port")}</label><FieldControl path="modem.device" /></div>}
        <div className="field-inline"><label htmlFor="field-modem-serial">{t(mode === 'port' ? "USB serial (optional)" : "USB serial")}</label><FieldControl path="modem.serial" /></div>
      </div>}
      {selectedModem === 'default' && mode !== 'auto' && <p className="field-message field-message-warning"><Icon name="info" size={15} />
        <span>{t("Pinning this modem turns off automatic detection of additional modems.")}</span></p>}
    </div>
  </SettingRow>;
}

function PlmnRow({ row }: { row: RowDefinition }) {
  usePreferences();
  const { draft, config, edit, editable, saving, errors } = useSupervisor();
  const value = draft?.modem?.plmn ?? '';
  const [choice, setChoice] = useState<PlmnMode | null>(null);
  useEffect(() => { setChoice(null); }, [config?.modem?.plmn]);
  const mode = choice ?? plmnMode(value), parts = plmnParts(value), error = errors['modem.plmn'];
  const definition = configFields.modem.plmn, locked = !editable || saving;
  const choose = (next: string) => {
    setChoice(next as PlmnMode);
    if (next === 'keep') edit('modem', 'plmn', '');
    else if (next === 'auto') edit('modem', 'plmn', 'auto');
  };
  return <SettingRow id={'row-' + row.id} label={t(definition.label)} hint={t(definition.hint!)} badge={<Origin paths={['modem.plmn']} />}>
    <div className="field-control field-stack" data-field="modem.plmn">
      <Segmented id="plmn-mode" value={mode} disabled={locked} labelledBy={'row-' + row.id + '-label'} setting="modem.plmn" onChange={choose}
        options={[{ value: 'keep', label: "Keep current" }, { value: 'auto', label: "Automatic" }, { value: 'fixed', label: "Fixed operator" }]} />
      {mode === 'fixed' && <div className="input-group">
        <input id="field-modem-plmn" name="modem.plmn" data-setting="modem.plmn" className="mono" inputMode="numeric" autoComplete="off"
          aria-label={t("MCC + MNC")} placeholder="21407" disabled={locked} value={plmnMode(value) === 'fixed' ? String(value) : ''}
          aria-invalid={error ? true : undefined} aria-describedby="field-modem-plmn-error" onChange={event => edit('modem', 'plmn', event.currentTarget.value.trim())} />
        {parts && <span className="input-badge">MCC {parts.mcc} · MNC {parts.mnc}</span>}
      </div>}
      <FieldError id="field-modem-plmn-error" message={error} />
    </div>
  </SettingRow>;
}

function ListenRow({ row }: { row: RowDefinition }) {
  usePreferences();
  const { draft, config, edit, editable, saving } = useSupervisor();
  const [choice, setChoice] = useState<ListenMode | null>(null);
  useEffect(() => { setChoice(null); }, [config?.web?.host]);
  const mode = choice ?? listenMode(draft?.web?.host);
  const choose = (next: string) => {
    setChoice(next as ListenMode);
    if (next === 'local') edit('web', 'host', '127.0.0.1');
    else if (next === 'auto') edit('web', 'host', 'auto');
  };
  return <SettingRow id={'row-' + row.id} label={t(row.label!)} hint={t(row.hint!)} badge={<Origin paths={['web.host']} />}>
    <div className="field-control field-stack">
      <RadioCards id="web-listen" value={mode} disabled={!editable || saving || draft?.web?.enabled === false} labelledBy={'row-' + row.id + '-label'}
        setting="web.host" onChange={choose} options={[
          { value: 'local', label: t("This device only"), description: t("Only from this device (localhost).") },
          { value: 'auto', label: t("This device and the local network"), description: t("Also a management LAN address, even when the modem is the preferred uplink.") },
          { value: 'custom', label: t("Specific IP address"), description: t("One numeric address of this device.") },
        ]} />
      {mode === 'custom' && <div className="field-inline"><label htmlFor="field-web-host">{t("Listen address")}</label><FieldControl path="web.host" /></div>}
    </div>
  </SettingRow>;
}

function ListenUrlsRow({ row }: { row: RowDefinition }) {
  usePreferences();
  const { listenUrls } = useSupervisor();
  const urls = listenUrls.filter(url => /^https?:\/\//.test(url));
  return <SettingRow id={'row-' + row.id} label={t(row.label!)}>
    {urls.length ? <ul className="listen-urls">{urls.map(url => <li key={url}><a href={url} rel="noreferrer" className="mono">{url}</a></li>)}</ul>
      : <p className="field-message">{t("No access addresses have been published.")}</p>}
  </SettingRow>;
}

function PasswordRow({ row }: { row: RowDefinition }) {
  usePreferences();
  const { password, setPassword, errors, editable, saving } = useSupervisor();
  return <SettingRow id={'row-' + row.id} label={t(row.label!)} labelFor="field-web-password" hint={t(row.hint!)}
    badge={password ? <ModifiedBadge previous={null} /> : null}>
    <div className="field-control" data-field="web.password">
      <PasswordInput id="field-web-password" name="new-password" value={password} onChange={setPassword} disabled={!editable || saving}
        invalid={Boolean(errors['web.password'])} describedBy="field-web-password-error" />
      {/* The live requirement already explains a short password. */}
      <FieldError id="field-web-password-error" message={errors['web.password'] === "Use at least 10 characters." ? null : errors['web.password']} />
    </div>
  </SettingRow>;
}

function QueryRows({ row }: { row: RowDefinition }) {
  usePreferences();
  const { draft, edit, errors, editable, saving } = useSupervisor();
  const telemetry = draft?.telemetry ?? {};
  // A backend without per-group scheduling exposes no query_* options.
  const scheduling = telemetry.query_signal !== undefined, custom = telemetry.profile === 'custom', locked = !editable || saving;
  const definition = configFields.telemetry.profile;
  const options = (definition.options ?? []).filter(option => scheduling || option.value !== 'custom');
  const budget = acquisitionBudget(telemetry), periods = queryPeriods(telemetry);
  const choose = (next: string) => {
    if (next === 'custom' && !custom) for (const [key, value] of Object.entries(customFromProfile(telemetry))) edit('telemetry', key, value);
    edit('telemetry', 'profile', next);
  };
  return <>
    <SettingRow id={'row-' + row.id} label={t(definition.label)} hint={t(definition.hint!)} badge={<Origin paths={['telemetry.profile']} />}>
      <div className="field-control field-stack" data-field="telemetry.profile">
        <Segmented id="field-telemetry-profile" value={telemetry.profile} options={options} onChange={choose} disabled={locked}
          labelledBy={'row-' + row.id + '-label'} describedBy="query-estimate field-telemetry-profile-error" invalid={Boolean(errors['telemetry.profile'])} setting="telemetry.profile" />
        <div className={'cost-meter' + (budget.over ? ' is-over' : '')} id="query-estimate" role="status">
          <div className="cost-meter-text"><span>{t("Cost per sample")}</span>
            <span><strong className="mono">≈ {budget.average} ms</strong> {t("of {budget} ms", { budget: budget.budget })}</span></div>
          <div className="cost-meter-track" aria-hidden="true"><span style={{ width: (budget.ratio * 100).toFixed(1) + '%' }} /></div>
          {budget.over && <p className="field-message field-message-warning"><Icon name="alert" size={15} />
            <span>{t("Up to {worst} ms when every query coincides, more than the acquisition budget.", { worst: budget.worst })}</span></p>}
        </div>
        <FieldError id="field-telemetry-profile-error" message={errors['telemetry.profile']} />
      </div>
    </SettingRow>
    {scheduling && custom && <div className="query-table" role="group" aria-label={t("Query schedule")}>
      <div className="query-head" aria-hidden="true"><span>{t("Query")}</span><span>{t("Every")}</span><span>{t("Cost")}</span></div>
      {queryGroups.map(group => {
        const path = 'telemetry.query_' + group.key, id = fieldId(path), error = errors[path];
        const enabled = periods[group.key] > 0, required = group.key === 'wds_status' && draft?.modem?.mode === 'connect' && enabled;
        const cost = queryCost(telemetry, group.key);
        return <div className="query-row" key={group.key}>
          <div className="query-name">
            <Switch id={id + '-enabled'} data-setting={path + '.enabled'} checked={enabled} disabled={locked || required}
              aria-label={t(group.label)} onChange={event => edit('telemetry', 'query_' + group.key,
                event.currentTarget.checked ? Math.max(group.period || 1, Number(telemetry.interval) || 1) : 0)} />
            <div><strong>{t(group.label)}</strong><span>{t(group.metrics)}</span>
              {required && <span>{t("Required to supervise the connection in connect mode.")}</span>}</div>
          </div>
          <div className="query-every">{enabled ? <>
            <label htmlFor={id} className="sr-only">{t("{query} every", { query: t(group.label) })}</label>
            <NumberField id={id} name={path} data-setting={path} value={telemetry['query_' + group.key]} unit="s" min={0} max={86400} step="any"
              disabled={locked} aria-invalid={error ? true : undefined} aria-describedby={id + '-error'}
              onValueChange={next => edit('telemetry', 'query_' + group.key, next)} /></> : <span className="muted">{t("Not requested")}</span>}
            <FieldError id={id + '-error'} message={error} />
          </div>
          <span className="query-cost mono">{cost.kind === 'ms' ? (cost.ms < 1 ? '< 1 ms' : '≈ ' + Math.round(cost.ms) + ' ms') : cost.kind === 'push' ? t("By indication") : '—'}</span>
        </div>;
      })}
      <div className="query-actions"><button type="button" id="query-recommended" className="button button-secondary" disabled={locked} onClick={() => {
        // Every full-profile metric; slow-changing groups are refreshed less often.
        for (const group of queryGroups) edit('telemetry', 'query_' + group.key, group.period);
        edit('telemetry', 'profile', 'custom');
      }}>{t("Use recommended periods")}</button></div>
    </div>}
  </>;
}

function HeaderSwitch({ path }: { path: string }) {
  usePreferences();
  const { draft, edit, editable, saving, errors } = useSupervisor();
  const value = valueAt(draft, path);
  if (value === undefined) return null;
  const [section, key] = path.split('.');
  const availability = fieldAvailability(draft, section, key);
  return <label className="card-switch" htmlFor={fieldId(path)} title={availability.disabled ? t(availability.reason ?? '') : undefined}>
    <span>{t(fieldDefinition(section, key, value).label)}</span>
    <Switch id={fieldId(path)} name={path} data-setting={path} checked={value === true}
      disabled={!editable || saving || (availability.disabled && !errors[path])}
      aria-describedby={availability.disabled ? fieldId(path) + '-note' : undefined}
      onChange={event => edit(section, key, event.currentTarget.checked)} /></label>;
}

/** Why the card's own switch is unavailable, e.g. verification without a default route. */
function HeaderNote({ path }: { path: string }) {
  usePreferences();
  const { draft } = useSupervisor();
  const [section, key] = path.split('.');
  const availability = fieldAvailability(draft, section, key);
  if (valueAt(draft, path) === undefined || !availability.disabled) return null;
  return <p id={fieldId(path) + '-note'} className="field-message config-card-note"><Icon name="info" size={15} /><span>{t(availability.reason ?? '')}</span></p>;
}

function RowView({ row }: { row: RowDefinition }) {
  const { draft } = useSupervisor();
  if (row.composite === 'route-role') return <RouteRoleRow row={row} />;
  if (row.composite === 'identify') return <IdentifyRow row={row} />;
  if (row.composite === 'plmn') return <PlmnRow row={row} />;
  if (row.composite === 'queries') return <QueryRows row={row} />;
  if (row.composite === 'listen') return <ListenRow row={row} />;
  if (row.composite === 'listen-urls') return <ListenUrlsRow row={row} />;
  if (row.composite === 'password') return <PasswordRow row={row} />;
  if (row.composite === 'system-log') return <SystemLog />;
  const present = row.fields.filter(path => valueAt(draft, path) !== undefined);
  return present.length === 1 && !row.label ? <FieldRow row={row} path={present[0]} /> : <PairRow row={row} paths={present} />;
}

/**
 * "Show more" at the foot of the card: it lists the rarely changed rows and never changes a value, so it reads
 * as a link rather than as a switch beside the section's own. The choice is a browser preference for every section.
 */
function AdvancedToggle({ hidden, controls }: { hidden: number; controls?: string }) {
  const { advanced, setAdvanced } = usePreferences();
  if (!advanced && hidden === 0) return null;
  return <div className="config-advanced">
    <button type="button" id="advanced-toggle" className="config-advanced-toggle" aria-expanded={advanced} aria-controls={controls}
      onClick={() => setAdvanced(!advanced)}>
      {advanced ? t("Hide advanced settings") : t(hidden === 1 ? "Show {count} advanced setting" : "Show {count} advanced settings", { count: hidden })}
      <Icon name="chevron-down" size={16} /></button>
  </div>;
}

function SectionCard({ section, context }: { section: SectionDefinition; context: VisibilityContext }) {
  const { advanced } = usePreferences();
  const rows = section.rows.filter(row => rowVisible(row, context));
  const hidden = advanced ? 0 : hiddenRowCount(section, context);
  const hasAdvanced = section.rows.some(row => row.advanced);
  return <section className="panel config-panel" aria-labelledby={'config-title-' + section.id}>
    <header className="config-card-header">
      <h2 id={'config-title-' + section.id}><Icon name={section.icon} size={20} />{t(section.label)}</h2>
      {section.toggle && <div className="config-card-actions"><HeaderSwitch path={section.toggle} /></div>}
      {section.toggle && <HeaderNote path={section.toggle} />}
    </header>
    {rows.length > 0 ? <div className="config-rows" id={'config-rows-' + section.id}>{rows.map(row => <RowView key={row.id} row={row} />)}</div>
      // Hiding advanced settings inside an all-advanced section keeps the card instead of jumping to another one.
      : !section.toggle && <p className="config-empty">{t("Every setting in this section is advanced.")}</p>}
    {hasAdvanced && <AdvancedToggle hidden={hidden} controls={rows.length > 0 ? 'config-rows-' + section.id : undefined} />}
  </section>;
}

export function Configuration({ scope }: { scope: SaveScope }) {
  const { advanced } = usePreferences();
  const { config, draft, editable, errors, password } = useSupervisor();
  const sections = sectionsFor(scope), tab = scope === 'profile' ? 'modem' : scope;
  const [selected, setSelected] = useState<string | null>(remembered[scope]);
  const [compactMenu, setCompactMenu] = useState(false);
  const [menuExpanded, setMenuExpanded] = useState(false);
  const menuToggle = useRef<HTMLButtonElement>(null);
  const focusNavigation = useRef(false);
  const lastNavigationFocus = useRef<'toggle' | 'navigation' | null>(null);
  useEffect(() => {
    // A deep link (?section=) opens its section once; afterwards the workbench remembers the last one.
    const requested = new URLSearchParams(window.location.search).get('section');
    if (requested && sections.some(section => section.id === requested)) { remembered[scope] = requested; setSelected(requested); }
    const listener = (target: SaveScope, id: string) => { if (target === scope) setSelected(id); };
    sectionListeners.add(listener);
    return () => { sectionListeners.delete(listener); };
  }, [scope, sections]);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 800px)');
    const rememberFocus = () => {
      const active = document.activeElement;
      if (active === menuToggle.current) lastNavigationFocus.current = 'toggle';
      else if (document.getElementById('config-subnav-list')?.contains(active)) lastNavigationFocus.current = 'navigation';
      else if (active !== document.body) lastNavigationFocus.current = null;
    };
    const update = () => {
      const navigation = document.getElementById('config-subnav-list');
      if (media.matches && (navigation?.contains(document.activeElement) || lastNavigationFocus.current === 'navigation')) menuToggle.current?.focus({ preventScroll: true });
      focusNavigation.current = !media.matches && (document.activeElement === menuToggle.current || lastNavigationFocus.current === 'toggle');
      setCompactMenu(media.matches);
      setMenuExpanded(false);
    };
    update();
    document.addEventListener('focusin', rememberFocus);
    media.addEventListener('change', update);
    return () => { media.removeEventListener('change', update); document.removeEventListener('focusin', rememberFocus); };
  }, []);
  useEffect(() => {
    if (!compactMenu && focusNavigation.current) {
      // The tablist must be unhidden by React before its selected tab can take focus.
      document.getElementById('config-subnav-list')
        ?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus({ preventScroll: true });
      focusNavigation.current = false;
    }
  }, [compactMenu]);
  useEffect(() => { setMenuExpanded(false); }, [scope]);
  const changes = useMemo(() => scopedChanges(config, draft, password, scope), [config, draft, password, scope]);
  const scopeErrors = useMemo(() => scopedErrors(errors, scope), [errors, scope]);
  const context: VisibilityContext = { draft, advanced, errors: scopeErrors, changes };
  const shown = visibleSections(sections, context);
  // The open section stays listed even when its settings are all advanced and the option was just turned off.
  const available = sections.filter(section => shown.includes(section) || (section.id === selected && section.rows.some(row => rowPresent(row, draft))));
  const active = available.find(section => section.id === selected) ?? available[0];
  function closeMenu() {
    setMenuExpanded(false);
    menuToggle.current?.focus({ preventScroll: true });
  }
  function choose(id: string, collapse = true) {
    remembered[scope] = id;
    setSelected(id);
    if (compactMenu && collapse) closeMenu();
  }
  function keyboard(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === 'Escape' && compactMenu && menuExpanded) {
      event.preventDefault();
      closeMenu();
      return;
    }
    const directions: Record<string, number> = {
      ArrowDown: (index + 1) % available.length, ArrowUp: (index + available.length - 1) % available.length,
      Home: 0, End: available.length - 1,
    };
    if (!(event.key in directions)) return;
    event.preventDefault();
    const next = available[directions[event.key]];
    choose(next.id, false);
    document.getElementById('config-subnav-' + next.id)?.focus();
  }
  if (!draft || !active) return <div className="card config-loading" role="status">{t("Loading configuration…")}</div>;
  const collapsed = compactMenu && !menuExpanded;
  // A scope with one section (the account) shows just its card: an index of one entry adds nothing.
  const single = sections.length === 1;
  const panel = single ? {} : { role: 'tabpanel', tabIndex: 0, 'aria-labelledby': 'config-subnav-' + active.id };
  return <div className="configuration" data-config-scope={scope}>
    {!editable && <p className="config-readonly-message">{t("Sign in to edit configuration.")}</p>}
    <div className={'config-workbench' + (single ? ' is-single' : '')}>
      {!single && <div className="config-navigation">
        <button ref={menuToggle} type="button" id="config-subnav-toggle" className="config-subnav-toggle"
          aria-expanded={!compactMenu || menuExpanded} aria-controls="config-subnav-list"
          onClick={() => setMenuExpanded(value => !value)}
          onKeyDown={event => {
            if (event.key === 'Escape' && compactMenu && menuExpanded) { event.preventDefault(); closeMenu(); }
          }}>
          <Icon name={active.icon} size={18} /><span>{t(active.label)}</span><Icon name="chevron-down" size={18} />
        </button>
        <nav id="config-subnav-list" className="config-subnav" role="tablist" aria-orientation="vertical" hidden={collapsed}
          aria-label={t(scope === 'web' ? "System settings sections" : scope === 'account' ? "Account sections" : "Modem settings sections")}>
          {available.map((section, index) => {
            const fields = sectionFields(section), errorCount = fields.filter(path => scopeErrors[path]).length
              + (section.rows.some(row => row.composite === 'password') && scopeErrors['web.password'] ? 1 : 0);
            const pending = fields.some(path => hasChange(changes, path)) || (section.rows.some(row => row.composite === 'password') && password !== '');
            return <Fragment key={section.id}>
              {section.group && section.group !== available[index - 1]?.group && <span className="config-subnav-group" aria-hidden="true">{t(groupLabels[section.group])}</span>}
              <button type="button" id={'config-subnav-' + section.id} data-config-section={section.id}
                className="config-subnav-button" role="tab" aria-selected={active.id === section.id}
                aria-controls={'config-panel-' + tab + '-' + section.id} tabIndex={active.id === section.id ? 0 : -1}
                onClick={() => choose(section.id)} onKeyDown={event => keyboard(event, index)}>
                <Icon name={section.icon} size={17} /><span>{t(section.label)}</span>
                {errorCount > 0 ? <ErrorCount count={errorCount} /> : pending && <PendingDot className="config-section-pending" />}
              </button>
            </Fragment>;
          })}
        </nav>
      </div>}
      <div className="config-sections">
        <div className="config-subsection" id={'config-panel-' + tab + '-' + active.id} {...panel}>
          <SectionCard section={active} context={context} />
        </div>
      </div>
    </div>
  </div>;
}

/** The former telemetry tab is now a section of the modem settings. */
export function LegacyTelemetryRedirect() {
  const router = useRouter();
  useEffect(() => {
    const modem = new URLSearchParams(window.location.search).get('modem');
    requestConfigSection('profile', 'capture');
    router.replace('/configuration/modem/?' + new URLSearchParams({ ...(modem ? { modem } : {}), section: 'capture' }).toString(), { scroll: false });
  }, [router]);
  return <Configuration scope="profile" />;
}
