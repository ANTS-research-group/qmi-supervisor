'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { Icon } from '@/components/icon';
import { Switch } from './form-controls';
import { useSupervisor } from './supervisor-provider';

type Plugin = {
  name: string; description: string; version: string; enabled: boolean;
  capabilities: string[]; os_capabilities?: string[]; valid: boolean; error: string | null;
  state?: string; restarts?: number; last_error?: string | null; connected?: boolean; pid?: number | null;
};

const STATE_TEXT: Record<string, string> = {
  running: "Running", stopped: "Stopped", backoff: "Restarting", invalid: "Invalid", pending: "Starting",
};

/** System → Plugins: manifests, state and the enable switch. */
export function Plugins() {
  usePreferences();
  const { session, notify } = useSupervisor();
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

  const toggle = async (plugin: Plugin, enabled: boolean) => {
    setBusy(plugin.name);
    try {
      await call('/api/plugins/enable', 'POST', { name: plugin.name, enabled });
      await load();
      notify(t(enabled ? "Plugin enabled." : "Plugin disabled."));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy('');
    }
  };

  return <div className="plugins">
    {error && <p className="field-message field-message-error" role="alert"><Icon name="alert-circle" size={15} /><span>{t(error)}</span></p>}
    {plugins === null ? <p className="muted" role="status">{t("Loading…")}</p>
      : !plugins.length ? <div className="empty-state"><Icon name="terminal" size={24} /><p>{t("No plugins are installed.")}</p></div>
      : <div className="table-wrap"><table className="plugin-table">
        <thead><tr><th>{t("Plugin")}</th><th>{t("State")}</th><th>{t("Capabilities")}</th>
          <th>{t("Restarts")}</th><th>{t("Last error")}</th><th /></tr></thead>
        <tbody>{plugins.map(plugin => <tr key={plugin.name} className={plugin.valid ? '' : 'plugin-invalid'}>
          <td><strong>{plugin.name}</strong>{plugin.version ? <span className="muted"> {plugin.version}</span> : null}
            <p className="muted">{plugin.description || t("No description.")}</p></td>
          <td><span className={'plugin-state plugin-state-' + (plugin.state || 'stopped')}>
            {t(STATE_TEXT[plugin.state || 'stopped'] || plugin.state || 'Stopped')}</span>
            {plugin.connected ? <span className="plugin-connected"> · {t("Connected")}</span> : null}</td>
          <td className="mono">{[...(plugin.capabilities || []), ...(plugin.os_capabilities || [])].join(', ') || '-'}</td>
          <td>{plugin.restarts ?? 0}</td>
          <td className="plugin-error">{plugin.valid ? (plugin.last_error || '-') : plugin.error}</td>
          <td className="plugin-actions">
            <label className="card-switch" htmlFor={'plugin-enabled-' + plugin.name}>
              <span>{t("Enabled")}</span>
              <Switch id={'plugin-enabled-' + plugin.name} checked={plugin.enabled}
                disabled={busy === plugin.name || (!plugin.valid && !plugin.enabled)}
                onChange={event => void toggle(plugin, event.currentTarget.checked)} /></label>
            <Link className="attention-link" prefetch={false}
              href={'/configuration/web/?section=log&plugin=' + encodeURIComponent(plugin.name)}>
              {t("Open its log")} <Icon name="arrow-up-right" size={14} /></Link>
          </td>
        </tr>)}</tbody>
      </table></div>}
  </div>;
}
