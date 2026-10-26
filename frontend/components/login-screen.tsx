'use client';

import { t } from '@/lib/messages';
import { usePreferences } from './preferences';

import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Icon } from './icon';
import { useSupervisor } from './supervisor-provider';

export function LoginScreen() {
  usePreferences();
  const { auth, session, login, connected, loggingOut, refresh } = useSupervisor();
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const usernameInput = useRef<HTMLInputElement>(null);
  const checking = auth === 'checking';
  const configured = session?.methods.password !== false;
  const disabled = checking || busy || loggingOut || !configured;
  useEffect(() => { if (auth === 'required' && configured) usernameInput.current?.focus(); }, [auth, configured]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (disabled) return;
    setBusy(true); setError('');
    try { await login(username, password); setPassword(''); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t("Could not sign in.")); }
    finally { setBusy(false); }
  };
  return <div className="login-stage"><section className="login-card" aria-labelledby="login-title">
    <div className="login-brand"><Icon name="radio" size={29} /></div>
    <span className="eyebrow">QMI SUPERVISOR</span>
    <h1 id="login-title">{t("Sign in")}</h1>
    <p className="muted">{t("Sign in with your username and password.")}</p>
    <form id="session-form" onSubmit={submit} aria-busy={checking || busy}>
      <div className="field"><label htmlFor="session-username">{t("Username")}</label><div className="input-with-icon"><Icon name="user" size={18} /><input ref={usernameInput} id="session-username" name="username" value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" maxLength={64} disabled={disabled} required /></div></div>
      <div className="field"><label htmlFor="session-password">{t("Password")}</label><div className="input-with-icon"><Icon name="lock" size={18} /><input id="session-password" name="password" type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" disabled={disabled} required /></div></div>
      {!configured && <p className="login-setup-message" role="status">{t("The web account is not configured yet. Set it up on the device and refresh access.")}</p>}
      <p id="session-error" className="field-error" role="alert">{t(error)}</p>
      <button id="session-connect" className="button login-submit" type="submit" disabled={disabled}>
        {checking ? <><span className="spinner" /> {t("Checking access…")}</> : loggingOut ? t("Signing out…") : busy ? <><span className="spinner" /> {t("Signing in…")}</> : <>{t("Sign in")} <Icon name="arrow-up-right" size={18} /></>}
      </button>
    </form>
    <div className="login-status"><span className={'status-dot ' + (connected ? 'status-good' : 'status-warning')} />
      <span>{checking ? t("Checking connection") : connected ? t("Connection established") : t("Device is not responding")}</span>
      <button className="icon-button" type="button" title={t("Refresh access")} aria-label={t("Refresh access")} disabled={busy || loggingOut} onClick={() => void refresh()}><Icon name="refresh" size={15} /></button>
    </div>
  </section><p className="login-caption">QMI Supervisor <span>·</span> {t("Device access")}</p></div>;
}
