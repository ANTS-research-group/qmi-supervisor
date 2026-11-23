'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import type { MouseEvent, ReactNode } from 'react';
import { t } from '@/lib/messages';
import { date } from '@/lib/metrics';
import { availableUpdate, indicatorTone, releaseAction, stepText, updateErrorText,
  HISTORY_ACTION_TEXT, HISTORY_RESULT_TEXT, RELATION_TEXT, STEP_STATE_TEXT } from '@/lib/updates';
import type { Message, UpdateHistoryEntry, UpdateInstall, UpdateRelease, UpdateResponse } from '@/lib/updates';
import { Icon } from './icon';
import { usePreferences } from './preferences';
import { useSupervisor } from './supervisor-provider';
import { UpdateRequestError, useUpdates } from './updates-provider';
import { requestConfigSection } from './configuration';

const messageText = (message: Message) => t(message.id, message.values);
const relationTone = (relation: string) => relation === 'installed' ? 'good' : relation === 'newer' ? 'info' : '';
const badgeClass = (tone: string) => 'update-badge' + (tone ? ' tone-' + tone : '');

function buildLabel(installed: UpdateResponse['installed']): string {
  const parts = [installed.commit, installed.built_utc ? date(installed.built_utc) : null].filter(Boolean);
  return parts.length ? parts.join(' · ') : t("Not available");
}

function CheckStatus({ installable }: { installable: UpdateRelease | null }) {
  usePreferences();
  const { data, phase } = useUpdates();
  if (!data) return null;
  const checking = phase === 'checking' || data.check.running;
  const prefix = data.check.checked_at_utc ? date(data.check.checked_at_utc) + ' · ' : '';
  if (checking) return <>{prefix}{t("Checking…")}</>;
  if (data.check.state === 'failed') return <>
    {prefix}<span className="field-message-error">{t("The last check failed: {reason}", {
      reason: t(updateErrorText(data.check.error_code), { code: data.check.error_code ?? '' }) })}</span></>;
  if (data.check.state === 'never') return <>{t("Never")}</>;
  if (installable) return <>{prefix}{t("Update available: {version}", { version: installable.version })}</>;
  return <>{prefix}{t("Up to date")}</>;
}

function ResultText({ install }: { install: UpdateInstall }) {
  usePreferences();
  const version = install.version ?? '';
  if (install.state === 'interrupted') return <p className="field-message field-message-warning">{t("The update was interrupted.")}</p>;
  if (install.state === 'succeeded') return <p className="field-message">{t(install.action === 'rollback'
    ? "Version {version} was restored." : "Updated to version {version}.", { version })}</p>;
  return <>
    <p className="field-message field-message-error">{install.state === 'rolled_back' && install.from_version
      ? t("The update to {version} failed. Version {previous} was restored.", { version, previous: install.from_version })
      : t("The update to {version} failed.", { version })}</p>
    <p className="field-message field-message-error">{t(updateErrorText(install.error_code), { code: install.error_code ?? '' })}</p>
  </>;
}

function ReleaseRows({ release, allowDowngrade, unitsDown, onAction }: {
  release: UpdateRelease; allowDowngrade: boolean; unitsDown: boolean;
  onAction: (request: DialogRequest) => void;
}) {
  const { advanced } = usePreferences();
  const action = releaseAction(release, { advanced, allowDowngrade });
  let cell: ReactNode = null;
  if (action.kind === 'update') cell = <button type="button" data-update-version={release.version} data-update-kind="update"
    className="button" disabled={unitsDown} onClick={() => onAction({ mode: 'update', version: release.version })}>{t("Update to {version}", { version: release.version })}</button>;
  else if (action.kind === 'downgrade') cell = <button type="button" data-update-version={release.version} data-update-kind="downgrade"
    className="button button-secondary" disabled={unitsDown} onClick={() => onAction({ mode: 'downgrade', version: release.version })}>{t("Install {version}", { version: release.version })}</button>;
  else if (action.kind === 'installed') cell = <span className="muted">{t("Already installed")}</span>;
  else if (action.reason) cell = <span className="muted">{t("Not installable: {reason}", { reason: messageText(action.reason) })}</span>;
  return <tbody data-release={release.version}>
    <tr>
      <td className="mono">{release.version}</td>
      <td>{release.published_at_utc ? date(release.published_at_utc) : t("Not available")}</td>
      <td><span className={badgeClass(release.prerelease ? 'warning' : '')}>{t(release.prerelease ? "Pre-release" : "Stable")}</span></td>
      <td><span className={badgeClass(relationTone(release.relation))}>{t(RELATION_TEXT[release.relation] ?? "Unknown")}</span></td>
      <td>{cell}</td>
    </tr>
    <tr><td colSpan={5}><details data-notes={release.version}><summary>{t("Release notes")}</summary>
      <pre className="update-notes">{release.notes.trim() ? release.notes : t("No release notes.")}</pre>
      {release.notes_truncated && <p className="field-message">{t("The notes were shortened.")}</p>}
    </details></td></tr>
  </tbody>;
}

function HistoryRow({ entry }: { entry: UpdateHistoryEntry }) {
  usePreferences();
  const when = entry.finished_at_utc ? date(entry.finished_at_utc)
    : entry.started_at_utc ? date(entry.started_at_utc) : t("Not available");
  const change = entry.version ? (entry.from_version ? entry.from_version + ' → ' + entry.version : entry.version) : t("Not available");
  const failed = entry.result === 'failed' || entry.result === 'rolled_back';
  return <tr>
    <td>{when}</td>
    <td>{t(HISTORY_ACTION_TEXT[entry.action] ?? entry.action)}</td>
    <td className="mono">{change}</td>
    <td><span className={badgeClass(failed ? 'bad' : entry.result === 'succeeded' ? 'good' : '')}>
      {t(HISTORY_RESULT_TEXT[entry.result] ?? entry.result)}</span>
      {failed && entry.error_code && <>
        <p className="field-message field-message-error">{t(updateErrorText(entry.error_code), { code: entry.error_code })}</p>
        <details><summary>{t("Technical detail")}</summary>
          <pre className="update-notes">{entry.error_code}{entry.error ? '\n' + entry.error : ''}</pre></details>
      </>}
    </td>
  </tr>;
}

type DialogRequest = { mode: 'update' | 'downgrade' | 'rollback'; version: string };

function UpdateDialog({ request, data, onClose }: { request: DialogRequest; data: UpdateResponse; onClose: () => void }) {
  usePreferences();
  const { startInstall, startRollback } = useUpdates();
  const { dirtyCount } = useSupervisor();
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
    input.current?.focus();
    return () => {
      if (dialog?.open) dialog.close();
      if (opener.current?.isConnected) opener.current.focus({ preventScroll: true });
    };
  }, []);

  const close = () => { setPassword(''); onClose(); };
  const confirm = async () => {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      if (request.mode === 'rollback') await startRollback(password);
      else await startInstall(request.version, request.mode === 'downgrade', password);
      setPassword('');
      onClose();
    } catch (failure) {
      if (failure instanceof UpdateRequestError) {
        if (failure.status === 401) { onClose(); return; }
        setError(failure.message);
        if (failure.code === 'INVALID_PASSWORD') input.current?.select();
      } else setError("The request could not be completed.");
    } finally { setBusy(false); }
  };

  const title = request.mode === 'update' ? t("Update to {version}?", { version: request.version })
    : request.mode === 'downgrade' ? t("Install the older version {version}?", { version: request.version })
    : t("Restore version {version}?", { version: request.version });
  const confirmLabel = request.mode === 'update' ? t("Update now")
    : request.mode === 'downgrade' ? t("Install {version}", { version: request.version })
    : t("Restore {version}", { version: request.version });
  return <dialog ref={ref} id="update-dialog" className="update-dialog" aria-labelledby="update-dialog-title"
    aria-describedby="update-dialog-text" onCancel={event => { event.preventDefault(); close(); }}>
    <form method="dialog" onSubmit={event => { event.preventDefault(); void confirm(); }}>
      <h2 id="update-dialog-title">{title}</h2>
      <ul id="update-dialog-text">
        <li>{t("The supervisor service restarts during the installation.")}</li>
        <li>{t("Modems disconnect and usually reconnect within about a minute; data sessions and monitoring pause meanwhile.")}</li>
        <li>{t("This page loses its connection and reloads by itself when the new version answers.")}</li>
        <li>{data.settings.auto_rollback ? t("If the new version does not work, the previous one is restored automatically.")
          : t("Automatic rollback is off. If the new version does not start, this page cannot restore the previous one; it must be done on the device.")}</li>
      </ul>
      {request.mode === 'downgrade' && <p id="update-downgrade-warning" className="field-message field-message-warning">
        <Icon name="alert" size={15} /><span>{t("{version} is older than the installed version {installed}. An older version may reject settings saved by this one and fail to start, and fixes made since then are lost.",
          { version: request.version, installed: data.installed.version })}</span></p>}
      {dirtyCount > 0 && <p className="field-message field-message-warning">
        <Icon name="alert" size={15} /><span>{t("You have unsaved changes. They are lost when the page reloads.")}</span></p>}
      <div className="field">
        <label htmlFor="update-password">{t("Password")}</label>
        <input ref={input} id="update-password" name="password" type="password" autoComplete="current-password" value={password}
          disabled={busy} aria-describedby="update-password-hint update-dialog-error" onChange={event => setPassword(event.currentTarget.value)} />
        <p id="update-password-hint" className="hint">{t("Enter your password to confirm.")}</p>
      </div>
      {error && <p id="update-dialog-error" className="field-message field-message-error" role="alert">
        <Icon name="alert-circle" size={15} /><span>{t(error)}</span></p>}
      <div className="button-row">
        <button type="button" id="update-cancel" className="button button-ghost" disabled={busy} onClick={close}>{t("Cancel")}</button>
        <button type="submit" id="update-confirm" className="button" disabled={busy || !password}>
          {busy ? <><span className="spinner" />{t("Starting…")}</> : confirmLabel}
        </button>
      </div>
    </form>
  </dialog>;
}

/**
 * The composite System row: installed version and build, the last check, the
 * released versions with their notes, the progress of a transaction, its result
 * and the last ten entries. It never renders a card or a panel of its own.
 */
export function UpdatesPanel() {
  const { advanced } = usePreferences();
  const { data, phase, pending, downSince, watch, markSeen, retryAfter, startCheck } = useUpdates();
  const [dialog, setDialog] = useState<DialogRequest | null>(null);
  const [now, setNow] = useState(0);

  useEffect(() => watch(), [watch]);
  useEffect(() => { if (data) markSeen(); }, [data, markSeen]);
  useEffect(() => {
    if (downSince === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(timer);
  }, [downSince]);

  if (!data) return <div className="updates"><p className="muted" role="status">{t("Loading…")}</p></div>;

  const install = data.install;
  const available = availableUpdate(data);
  const unitsDown = Boolean(data.units_error);
  const checking = phase === 'checking' || data.check.running;
  const seconds = Math.ceil(Math.max(retryAfter, data.check.retry_after ?? 0));
  const ownInstall = Boolean(pending);
  const installing = Boolean(pending) || Boolean(install?.running) || ['starting', 'running', 'restarting'].includes(phase);
  const activeVersion = install?.running ? (install.version ?? '') : pending?.target ?? install?.version ?? '';
  const activeAction = install?.running ? install.action : pending?.action ?? 'install';
  const steps = install?.running ? install.steps : [];
  const minutesDown = downSince !== null ? Math.max(0, Math.floor(((now || Date.now()) - downSince) / 60000)) : 0;
  const finishedAt = install?.finished_at_utc ? Date.parse(install.finished_at_utc) : NaN;
  const showResult = Boolean(install && !install.running && install.state !== 'running'
    && Number.isFinite(finishedAt) && Date.now() - finishedAt < 86400000);
  const lastResult = data.history[0]?.result;
  const showRollback = Boolean(data.rollback.available && data.rollback.version
    && (advanced || lastResult === 'failed' || lastResult === 'rolled_back'));

  return <div className="updates">
    <section id="update-installed" className="update-block">
      <dl className="metric-key-values">
        <div><dt>{t("Installed version")}</dt><dd id="update-installed-version" className="mono">{data.installed.version}</dd></div>
        <div><dt>{t("Build")}</dt><dd className="mono">{buildLabel(data.installed)}</dd></div>
        <div><dt>{t("Last check")}</dt><dd id="update-check-status"><CheckStatus installable={available} /></dd></div>
      </dl>
      <div className="update-actions">
        <button type="button" id="update-check" className="button button-secondary" disabled={checking || seconds > 0 || unitsDown}
          onClick={() => void startCheck()}>
          {checking ? <><span className="spinner" />{t("Checking…")}</>
            : seconds > 0 ? t("Check again in {seconds} s", { seconds })
            : <><Icon name="refresh" size={15} />{t("Check now")}</>}
        </button>
      </div>
      {data.state_error && <p className="field-message field-message-error" role="alert">
        <Icon name="alert-circle" size={15} /><span>{t("The update information is not available.")}</span></p>}
      {unitsDown && <p className="field-message field-message-error" role="alert">
        <Icon name="alert-circle" size={15} /><span>{t("The updater is not available on this device.")}</span></p>}
    </section>

    {phase === 'reload-required' && <div id="update-reload-hint" className="update-block" role="status">
      <p>{ownInstall ? t("Version {version} is installed. Reload the page to use it.", { version: data.installed.version })
        : t("Reload the page to load version {version}.", { version: data.installed.version })}</p>
      <div className="update-actions">
        <button type="button" id="update-reload" className="button" onClick={() => window.location.reload()}>{t("Reload page")}</button>
      </div>
    </div>}

    {available && <section id="update-primary" className="update-block">
      <p>{t("Version {version} is available.", { version: available.version })}</p>
      <div className="update-actions">
        <button type="button" id="update-install-primary" className="button" disabled={unitsDown}
          onClick={() => setDialog({ mode: 'update', version: available.version })}>
          {t("Update to {version}", { version: available.version })}</button>
      </div>
    </section>}

    {installing && <section id="update-progress" className="update-block">
      <h3>{t(activeAction === 'rollback' ? "Restoring {version}" : "Installing {version}", { version: activeVersion })}</h3>
      {steps.length > 0 && <ol id="update-steps" className="update-steps" aria-live="polite">
        {steps.map(step => <li key={step.id} data-step={step.id} data-state={step.state}>
          {step.state === 'running' ? <span className="spinner" />
            : <Icon name={step.state === 'done' ? 'check' : step.state === 'failed' ? 'alert' : 'circle'} size={16} />}
          <span>{stepText(step.id) === step.id
            ? <code className="mono">{step.id}</code> : t(stepText(step.id))}</span>
          <span className="sr-only">{t(STEP_STATE_TEXT[step.state] ?? step.state)}</span>
        </li>)}
      </ol>}
      {phase === 'restarting' && <>
        <p id="update-restarting" role="status">{t("The supervisor is restarting. This page reconnects by itself.")}</p>
        {minutesDown >= 5 && <p className="field-message field-message-warning">
          <Icon name="alert" size={15} /><span>{t("The supervisor has not answered for {minutes} min. If it does not come back, check the device.",
            { minutes: minutesDown })}</span></p>}
      </>}
    </section>}

    {showResult && install && <section id="update-result" className="update-block"><ResultText install={install} /></section>}

    <section id="update-versions" className="update-block">
      <h3>{t("Versions")}</h3>
      {data.releases.length === 0 ? <p className="muted">{t("No versions are listed yet. Check for updates to load them.")}</p>
        : <div className="table-wrap metrics-table-wrap"><table className="metrics-table update-table">
          <thead><tr><th>{t("Version")}</th><th>{t("Date")}</th><th>{t("Channel")}</th><th>{t("Status")}</th><th>{t("Action")}</th></tr></thead>
          {data.releases.map(release => <ReleaseRows key={release.version} release={release} unitsDown={unitsDown}
            allowDowngrade={data.settings.allow_downgrade === true} onAction={setDialog} />)}
        </table></div>}
    </section>

    {showRollback && <section id="update-rollback" className="update-block">
      <p>{t("Version {version} is kept so it can be restored.", { version: data.rollback.version! })}</p>
      <div className="update-actions">
        <button type="button" id="update-restore" className="button button-secondary" disabled={unitsDown}
          onClick={() => setDialog({ mode: 'rollback', version: data.rollback.version! })}>
          {t("Restore {version}", { version: data.rollback.version! })}</button>
      </div>
    </section>}

    <section id="update-history" className="update-block">
      <h3>{t("History")}</h3>
      {data.history.length === 0 ? <p className="muted">{t("No update has run yet.")}</p>
        : <div className="table-wrap metrics-table-wrap"><table className="metrics-table update-table">
          <thead><tr><th>{t("When")}</th><th>{t("Action")}</th><th>{t("Change")}</th><th>{t("Result")}</th></tr></thead>
          <tbody>{data.history.map((entry, index) => <HistoryRow key={index} entry={entry} />)}</tbody>
        </table></div>}
    </section>

    {dialog && <UpdateDialog request={dialog} data={data} onClose={() => setDialog(null)} />}
  </div>;
}

/** The System dot: a new failure is bad, an available or running update is info. */
export function UpdateNavDot({ className }: { className?: string }) {
  usePreferences();
  const { data, seen } = useUpdates();
  const tone = indicatorTone(data, seen);
  if (!tone) return null;
  return <>
    <span className={'nav-update-dot tone-' + tone + (className ? ' ' + className : '')} aria-hidden="true" />
    <span className="sr-only">{t(tone === 'bad' ? "Update problem" : "Update available")}</span>
  </>;
}

/** The one line in the account footer; it opens the section in the System page. */
export function UpdateSidebarLine({ onNavigate }: { onNavigate?: (event: MouseEvent<HTMLAnchorElement>) => void }) {
  usePreferences();
  const { data, pending } = useUpdates();
  const available = data ? availableUpdate(data) : null;
  const install = data?.install;
  const installing = Boolean(pending) || Boolean(install?.running);
  if (!installing && !available) return null;
  const version = install?.running ? install.version : pending?.target ?? available?.version ?? '';
  if (!version) return null;
  return <Link id="update-available" className="account-update" href="/configuration/web/?section=updates" prefetch={false}
    onClick={event => { requestConfigSection('web', 'updates'); onNavigate?.(event); }}>
    {installing ? t("Updating to {version}…", { version }) : t("Update available: {version}", { version })}
  </Link>;
}
