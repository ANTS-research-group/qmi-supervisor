'use client';

import { intlLocale, t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '@/components/icon';
import { NumberField, SettingRow, Switch } from './form-controls';
import { useSupervisor } from './supervisor-provider';

type ApiToken = {
  id: string; name: string; scopes: string[]; created_utc: string | null;
  expires_utc: string | null; last_used_utc: string | null;
};

/** What each scope opens, in the order of doc/api-v1.md. */
const SCOPES: { scope: string; hint: string }[] = [
  { scope: 'read:status', hint: "Modems, their records and health." },
  { scope: 'read:events', hint: "Diagnosis and connection events." },
  { scope: 'read:metrics', hint: "Prometheus metrics at /metrics." },
];

function moment(value: string | null): string {
  if (!value) return t('Never');
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value
    : new Intl.DateTimeFormat(intlLocale(), { dateStyle: 'short', timeStyle: 'short' }).format(parsed);
}

/**
 * Token rows of System → API access, after the switch row: create a token (shown once) and revoke existing ones.
 * Tokens are stored by the supervisor as digests; this page never sees a token again after creating it.
 */
export function ApiAccess() {
  usePreferences();
  const { session, notify, editable } = useSupervisor();
  const [tokens, setTokens] = useState<ApiToken[] | null>(null);
  const [error, setError] = useState('');
  const [listError, setListError] = useState('');
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<string[]>(['read:status']);
  const [days, setDays] = useState<number>(NaN);
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
      setListError('');
    } catch (failure) {
      if (live.current) setListError(failure instanceof Error ? failure.message : String(failure));
    }
  }, [call]);

  useEffect(() => {
    live.current = true;
    void load();
    return () => { live.current = false; };
  }, [load]);

  const validDays = Number.isFinite(days) ? Number.isInteger(days) && days >= 1 && days <= 3650 : true;

  const create = async () => {
    setBusy(true);
    try {
      const data = await call('/api/tokens', 'POST', { name: name.trim(), scopes,
        expires_days: Number.isFinite(days) ? days : null });
      setCreated(String(data.token || ''));
      setName('');
      setDays(NaN);
      setError('');
      await load();
      notify(t("Token created. Copy it now; it is not shown again."));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (record: ApiToken) => {
    setBusy(true);
    try {
      await call('/api/tokens/revoke', 'POST', { id: record.id });
      if (created?.startsWith('qms_' + record.id + '_')) setCreated(null);
      await load();
      notify(t("Token revoked."));
    } catch (failure) {
      setListError(failure instanceof Error ? failure.message : String(failure));
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
    setScopes(current => SCOPES.map(item => item.scope)
      .filter(item => item === scope ? checked : current.includes(item)));
  };

  const locked = !editable || busy;
  return <>
    <SettingRow id="row-api-new" label={t("New token")} labelFor="api-token-name"
      hint={t("It is shown only once: store it in the system that will use it.")}>
      <div className="field-pair">
        <div className="field-pair-item">
          <label className="field-pair-label" htmlFor="api-token-name">{t("Name")}</label>
          <input id="api-token-name" value={name} maxLength={64} disabled={locked} autoComplete="off"
            placeholder={t("For example: grafana")} onChange={event => setName(event.currentTarget.value)} />
        </div>
        <div className="field-pair-item">
          <label className="field-pair-label" htmlFor="api-token-days">{t("Expires after")}</label>
          <NumberField id="api-token-days" value={days} unit="days" min={1} max={3650} step={1} disabled={locked}
            placeholder={t("Never")} aria-invalid={validDays ? undefined : true} aria-describedby="api-token-days-error"
            onValueChange={setDays} />
          <p id="api-token-days-error" className="field-message field-message-error" aria-live="polite">
            {!validDays && <><Icon name="alert-circle" size={15} /><span>{t("Use whole days between 1 and 3650, or leave it empty.")}</span></>}</p>
        </div>
      </div>
      <div className="api-scopes" role="group" aria-labelledby="api-scopes-label">
        <span className="field-pair-label" id="api-scopes-label">{t("Scopes")}</span>
        {SCOPES.map(({ scope, hint }) => <label key={scope} className="scope-option" htmlFor={'api-scope-' + scope.replace(':', '-')}>
          <Switch id={'api-scope-' + scope.replace(':', '-')} checked={scopes.includes(scope)} disabled={locked}
            onChange={event => toggleScope(scope, event.currentTarget.checked)} />
          <span><strong className="mono">{scope}</strong><span>{t(hint)}</span></span>
        </label>)}
      </div>
      <div className="api-token-actions">
        <button type="button" id="api-token-create" className="button button-secondary"
          disabled={locked || !name.trim() || !scopes.length || !validDays} onClick={() => void create()}>
          {busy ? <span className="spinner" /> : <Icon name="plus" size={15} />}{t("Create token")}</button>
      </div>
      {error && <p className="field-message field-message-error" role="alert"><Icon name="alert-circle" size={15} /><span>{t(error)}</span></p>}
      {created && <div className="api-token-created" role="status">
        <p className="field-message field-message-warning"><Icon name="alert" size={15} />
          <span>{t("Token created. Copy it now; it is not shown again.")}</span></p>
        <div className="input-group">
          <input className="mono api-token-value" readOnly value={created} aria-label={t("New token")}
            onFocus={event => event.currentTarget.select()} />
          <button type="button" id="api-token-copy" className="input-action" onClick={() => void copy()}>
            <Icon name="copy" size={15} /> {t("Copy")}</button>
        </div>
      </div>}
    </SettingRow>

    <SettingRow id="row-api-access" label={t("Tokens")} hint={t("A revoked token is refused from its next request.")}>
      {listError && <p className="field-message field-message-error" role="alert"><Icon name="alert-circle" size={15} /><span>{t(listError)}</span></p>}
      {tokens === null ? !listError && <p className="muted api-token-status" role="status">{t("Loading…")}</p>
        : tokens.length ? <ul className="api-token-list">{tokens.map(record => <li key={record.id} className="api-token" data-token={record.id}>
          <div className="api-token-head">
            <span className="api-token-name"><strong>{record.name}</strong> <span className="mono muted">{record.id}</span></span>
            <button type="button" className="button button-secondary api-token-revoke" data-token={record.id}
              disabled={locked} onClick={() => void revoke(record)}>{t("Revoke")}</button>
          </div>
          <div className="scope-chips">{record.scopes.map(scope => <span key={scope} className="scope-chip mono">{scope}</span>)}</div>
          <p className="api-token-meta">
            <span>{t("Created {time}", { time: moment(record.created_utc) })}</span>
            <span>{record.expires_utc ? t("Expires {time}", { time: moment(record.expires_utc) }) : t("Never expires")}</span>
            <span>{record.last_used_utc ? t("Last used {time}", { time: moment(record.last_used_utc) }) : t("Never used")}</span>
          </p>
        </li>)}</ul>
        : <p className="muted api-token-status" role="status">{t("No API tokens yet.")}</p>}
    </SettingRow>
  </>;
}
