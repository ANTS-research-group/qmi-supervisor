'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { MouseEvent } from 'react';
import { requestConfigSection } from './configuration';
import Link from 'next/link';
import { Icon } from '@/components/icon';
import { SettingRow, Switch } from './form-controls';
import { useSupervisor } from './supervisor-provider';

type PluginIssue = { id?: string; tone?: string; title?: string; text?: string; action?: string | null };
type Plugin = {
  name: string; description: string; version: string; enabled: boolean;
  capabilities: string[]; os_capabilities?: string[]; valid: boolean; error: string | null;
  state?: string; restarts?: number; last_error?: string | null; connected?: boolean; pid?: number | null;
  effective_capabilities?: string[];
  grants?: { capability: string; granted: boolean }[];
  issues?: PluginIssue[];
};

const MANIFEST_DIRECTORY = '/etc/qmi-supervisor/plugins.d/';

/** Plugin process states, with the tone of the state pill used across the interface. */
const STATE: Record<string, { label: string; tone: string }> = {
  running: { label: "Running", tone: 'good' },
  pending: { label: "Starting", tone: '' },
  backoff: { label: "Restarting", tone: 'warning' },
  stopped: { label: "Stopped", tone: '' },
  invalid: { label: "Invalid", tone: 'bad' },
};

/** What a capability that needs the administrator's confirmation lets the plugin do. */
const CAPABILITY_HINT: Record<string, string> = {
  'act:objective': "Turn a modem's connection or monitoring on and off.",
  'act:reconnect': "Restart a modem's data connection.",
  'ui:cards': "Offer cards for the modem summary.",
  'ui:issues': "Report problems in the attention panel.",
};

const logHref = (name: string) => '/configuration/web/?section=log&plugin=' + encodeURIComponent(name);

/**
 * The log is another section of this same page: the URL carries the filter for the log to read when it opens,
 * and the section changes in place (a new tab or window still follows the plain link).
 */
function openLog(event: MouseEvent<HTMLAnchorElement>, name: string) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  window.history.pushState(window.history.state, '', logHref(name));
  requestConfigSection('web', 'log');
}

function statePill(plugin: Plugin) {
  const key = !plugin.valid ? 'invalid' : plugin.state || (plugin.enabled ? 'pending' : 'stopped');
  const state = STATE[key] ?? { label: key, tone: '' };
  return <span className={'state-pill' + (state.tone ? ' tone-' + state.tone : '')}><span className="state-dot" />{t(state.label)}</span>;
}

/** System → Plugins: one setting row per manifest with its state, switch, confirmed capabilities and problems. */
export function Plugins() {
  usePreferences();
  const { session, notify, editable } = useSupervisor();
  const [plugins, setPlugins] = useState<Plugin[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const live = useRef(true);
  const csrf = session?.csrf_token || undefined;

  const call = useCallback(async (path: string, method: 'GET' | 'POST', body?: unknown) => {
    const headers: Record<string, string> = {};
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      if (csrf) headers['X-CSRF-Token'] = csrf;
    }
    const response = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store',
      body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(String(data.error || "The request could not be completed."));
    return data as { plugins?: Plugin[] };
  }, [csrf]);

  const load = useCallback(async () => {
    try {
      const data = await call('/api/plugins', 'GET');
      if (!live.current) return;
      setPlugins(Array.isArray(data.plugins) ? data.plugins : []);
      setError('');
    } catch (failure) {
      if (live.current) setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, [call]);

  useEffect(() => {
    live.current = true;
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, 5000);
    return () => { live.current = false; window.clearInterval(timer); };
  }, [load]);

  const change = async (plugin: Plugin, path: string, body: unknown, done: string) => {
    setBusy(plugin.name);
    try {
      await call(path, 'POST', body);
      await load();
      notify(t(done));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy('');
    }
  };

  const installed = plugins ?? [];
  const enabled = installed.filter(plugin => plugin.enabled).length;
  return <>
    <SettingRow id="row-plugins" label={t("Installed plugins")}
      hint={t("Separate programs that run unprivileged and only use the capabilities you confirm.")}>
      {error && <p className="field-message field-message-error" role="alert"><Icon name="alert-circle" size={15} /><span>{t(error)}</span></p>}
      {plugins === null ? !error && <p className="muted plugin-summary" role="status">{t("Loading…")}</p>
        : <div className="plugin-summary">
          <p>{installed.length
            ? t(installed.length === 1 ? "{count} plugin installed, {enabled} enabled." : "{count} plugins installed, {enabled} enabled.",
              { count: installed.length, enabled })
            : t("No plugins are installed.")}</p>
          <p className="field-message"><Icon name="info" size={15} />
            <span>{t("To install one, copy its manifest to {directory} and enable it here.", { directory: MANIFEST_DIRECTORY })}</span></p>
        </div>}
    </SettingRow>

    {installed.map(plugin => {
      const id = 'row-plugin-' + plugin.name, locked = !editable || busy === plugin.name;
      const grants = plugin.grants ?? [];
      const granted = new Set(grants.filter(item => item.granted).map(item => item.capability));
      // Read scopes come from the manifest; action and UI scopes only once confirmed below.
      const direct = plugin.capabilities.filter(capability => !grants.some(item => item.capability === capability));
      const problem = plugin.valid ? plugin.last_error : plugin.error;
      return <SettingRow key={plugin.name} id={id} labelFor={'plugin-enabled-' + plugin.name}
        label={<span className="plugin-title">{plugin.name}{plugin.version && <span className="plugin-version mono">{plugin.version}</span>}</span>}
        hint={plugin.description || t("No description.")}
        badge={<div className="plugin-status">{statePill(plugin)}
          {plugin.connected && <span className="state-pill tone-good">{t("Connected")}</span>}
          {(plugin.restarts ?? 0) > 0 && <span className="plugin-restarts">{t((plugin.restarts ?? 0) === 1 ? "{count} restart" : "{count} restarts",
            { count: plugin.restarts ?? 0 })}</span>}
        </div>}>
        <div className="field-control" data-plugin={plugin.name}>
          {/* The row title (the plugin name) labels this switch, as in every other setting row. */}
          <div className="plugin-enable">
            <Switch id={'plugin-enabled-' + plugin.name} checked={plugin.enabled}
              disabled={locked || (!plugin.valid && !plugin.enabled)}
              onChange={event => void change(plugin, '/api/plugins/enable', { name: plugin.name, enabled: event.currentTarget.checked },
                event.currentTarget.checked ? "Plugin enabled." : "Plugin disabled.")} />
            <span aria-hidden="true">{t(plugin.enabled ? "Enabled" : "Disabled")}</span>
          </div>
          {problem && <p className="field-message field-message-error"><Icon name="alert-circle" size={15} />
            <span>{plugin.valid ? t("Last error: {error}", { error: problem }) : problem}</span></p>}
        </div>

        {/* Problems about no particular modem, written like the modem attention panel. */}
        {(plugin.issues ?? []).length > 0 && <div className="plugin-issues">
          <ul className="attention-issues" aria-label={t("Problems reported by {plugin}", { plugin: plugin.name })}>
            {(plugin.issues ?? []).map((issue, index) => {
              const tone = issue.tone === 'bad' ? 'bad' : issue.tone === 'warning' ? 'warning' : 'info';
              return <li key={issue.id ?? index} className={'attention-issue plugin-issue tone-' + tone + (tone === 'info' ? ' attention-issue-info' : '')}>
                <h3><Icon name={tone === 'info' ? 'info' : 'alert'} size={15} />{issue.title}</h3>
                {issue.text && <p>{issue.text}</p>}
                {issue.action && <div className="attention-action">
                  <span className="attention-action-label">{t("What to do")}</span>
                  <p>{issue.action}</p>
                </div>}
              </li>;
            })}
          </ul>
        </div>}

        {direct.length > 0 && <div className="plugin-block">
          <span className="field-pair-label">{t("Reads")}</span>
          <div className="scope-chips">{direct.map(capability => <span key={capability} className="scope-chip mono">{capability}</span>)}</div>
        </div>}

        {grants.length > 0 && <div className="plugin-block" role="group" aria-labelledby={id + '-grants'}>
          <span className="field-pair-label" id={id + '-grants'}>{t("Needs your confirmation")}</span>
          {grants.map(item => {
            const control = 'plugin-grant-' + plugin.name + '-' + item.capability.replace(':', '-');
            return <label key={item.capability} className="scope-option" htmlFor={control}>
              <Switch id={control} checked={granted.has(item.capability)} disabled={locked}
                onChange={event => void change(plugin, '/api/plugins/grants',
                  { name: plugin.name, capability: item.capability, granted: event.currentTarget.checked },
                  event.currentTarget.checked ? "Capability granted." : "Capability revoked.")} />
              <span><strong className="mono">{item.capability}</strong>
                {CAPABILITY_HINT[item.capability] && <span>{t(CAPABILITY_HINT[item.capability])}</span>}</span>
            </label>;
          })}
        </div>}

        {(plugin.os_capabilities ?? []).length > 0 && <div className="plugin-block">
          <span className="field-pair-label">{t("System privileges")}</span>
          <div className="scope-chips">{(plugin.os_capabilities ?? []).map(capability => <span key={capability} className="scope-chip scope-chip-warning mono">{capability}</span>)}</div>
        </div>}

        <Link className="metric-text-link plugin-log" prefetch={false} href={logHref(plugin.name)}
          onClick={event => openLog(event, plugin.name)}>
          {t("Open its log")} <Icon name="arrow-up-right" size={14} /></Link>
      </SettingRow>;
    })}
  </>;
}
