'use client';

import { intlLocale, t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '@/components/icon';
import { Switch } from './form-controls';
import { useSupervisor } from './supervisor-provider';

type ApiToken = {
  id: string; name: string; scopes: string[]; created_utc: string | null;
  expires_utc: string | null; last_used_utc: string | null;
};

const SCOPES = ['read:status', 'read:events', 'read:metrics'];

function moment(value: string | null): string {
  if (!value) return t('Never');
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value
    : new Intl.DateTimeFormat(intlLocale(), { dateStyle: 'short', timeStyle: 'short' }).format(parsed);
}

/** Machine API switch, token creation and revocation (root-managed tokens). */
export function ApiAccess() {
  usePreferences();
  const { draft, edit, session, notify } = useSupervisor();
  const [tokens, setTokens] = useState<ApiToken[] | null>(null);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<string[]>(['read:status']);
  const [days, setDays] = useState('');
  const [created, setCreated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
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
    return data as { tokens?: ApiToken[]; token?: string; revoked?: boolean };
  }, [csrf]);

  const load = useCallback(async () => {
    try {
      const data = await call('/api/tokens', 'GET');
      if (!live.current) return;
      setTokens(Array.isArray(data.tokens) ? data.tokens : []);
      setError('');
    } catch (failure) {
      if (live.current) setError(failure instanceof Error ? failure.message : String(failure));
    }
  }, [call]);

  useEffect(() => {
    live.current = true;
    void load();
    return () => { live.current = false; };
  }, [load]);

  const create = async () => {
    setBusy(true);
    try {
      const expires = days.trim() ? Number(days) : null;
      const data = await call('/api/tokens', 'POST', { name: name.trim(), scopes, expires_days: expires });
      setCreated(String(data.token || ''));
      setName('');
      setDays('');
      await load();
      notify(t("Token created. Copy it now; it is not shown again."));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id: string) => {
    setBusy(true);
    try {
      await call('/api/tokens/revoke', 'POST', { id });
      if (created?.includes(id)) setCreated(null);
      await load();
      notify(t("Token revoked."));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created);
      notify(t("Token copied."));
    } catch {
      setError(t("The token could not be copied; select it and copy it by hand."));
    }
  };

  const toggleScope = (scope: string, checked: boolean) => {
    setScopes(current => checked ? [...current.filter(item => item !== scope), scope]
      : current.filter(item => item !== scope));
  };

  return <div className="api-access">
    <label className="card-switch" htmlFor="web-api-enabled"><span>{t("Machine API")}</span>
      <Switch id="web-api-enabled" data-setting="web.api" checked={draft?.web?.api === true}
        onChange={event => edit('web', 'api', event.currentTarget.checked)} /></label>
    <p className="field-message config-card-note"><Icon name="info" size={15} />
      <span>{t("Read-only API v1 and Prometheus metrics for other systems. Tokens are managed here or with the configurator CLI; the API exposes the identifiers this supervisor knows.")}</span></p>

    {error && <p className="field-message field-message-error" role="alert"><Icon name="alert-circle" size={15} /><span>{t(error)}</span></p>}

    {created && <div className="api-token-created" role="status">
      <p>{t("Token created. Copy it now; it is not shown again.")}</p>
      <code className="mono api-token-value">{created}</code>
      <button type="button" id="api-token-copy" className="button button-secondary" onClick={() => void copy()}>
        <Icon name="database" size={15} />{t("Copy token")}</button>
    </div>}

    <div className="api-token-form">
      <label className="api-token-name"><span>{t("Name")}</span>
        <input id="api-token-name" value={name} maxLength={64}
          onChange={event => setName(event.currentTarget.value)} /></label>
      <div className="api-token-scopes" role="group" aria-label={t("Scopes")}>
        {SCOPES.map(scope => <label key={scope} className="card-switch">
          <span className="mono">{scope}</span>
          <Switch id={'api-scope-' + scope.replace(':', '-')} checked={scopes.includes(scope)}
            onChange={event => toggleScope(scope, event.currentTarget.checked)} /></label>)}
      </div>
      <label className="api-token-days"><span>{t("Expires in days")}</span>
        <input id="api-token-days" inputMode="numeric" value={days} placeholder={t("Never")}
          onChange={event => setDays(event.currentTarget.value.replace(/[^0-9]/g, '').slice(0, 4))} /></label>
      <button type="button" id="api-token-create" className="button" disabled={busy || !name.trim() || !scopes.length}
        onClick={() => void create()}>{t("Create token")}</button>
    </div>

    {tokens === null ? <p className="muted" role="status">{t("Loading…")}</p>
      : tokens.length ? <div className="table-wrap"><table className="api-token-table">
        <thead><tr><th>{t("Name")}</th><th>{t("Scopes")}</th><th>{t("Created")}</th>
          <th>{t("Expires")}</th><th>{t("Last used")}</th><th /></tr></thead>
        <tbody>{tokens.map(record => <tr key={record.id}>
          <td>{record.name}<span className="mono muted"> {record.id}</span></td>
          <td className="mono">{record.scopes.join(', ')}</td>
          <td>{moment(record.created_utc)}</td>
          <td>{moment(record.expires_utc)}</td>
          <td>{moment(record.last_used_utc)}</td>
          <td><button type="button" className="button button-secondary api-token-revoke"
            data-token={record.id} disabled={busy}
            onClick={() => void revoke(record.id)}>{t("Revoke")}</button></td>
        </tr>)}</tbody>
      </table></div>
      : <p className="muted" role="status">{t("No API tokens yet.")}</p>}
  </div>;
}
