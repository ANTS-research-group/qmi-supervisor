'use client';

import { intlLocale, t } from '@/lib/messages';
import { usePreferences } from './preferences';

import Link from 'next/link';
import { createPortal } from 'react-dom';
import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { useSupervisor } from './supervisor-provider';
import { Icon } from './icon';
import { age, apnPresentation, gpsPresentation, object, trafficPresentation } from '@/lib/metrics';
import { attentionTone, collectIssues, issueText } from '@/lib/diagnosis';
import type { Issue, IssueLink } from '@/lib/diagnosis';
import { modemName } from '@/lib/fleet';
import type { DataMap, ModemRecord } from '@/lib/types';

/** Attach fallback status -> English message ID, translated for the locale of the render. */
const ATTACH_FALLBACK_TEXT: Record<string, string> = {
  fallback_unsupported: "The modem does not support initial attach profile configuration.",
  fallback_information_unavailable: "The modem does not provide the information needed to configure its initial attach profile.",
  fallback_no_current_profile: "The modem does not report a current initial attach profile.",
  fallback_pending_only: "Only a pending initial attach profile is reported.",
  fallback_unsafe_profile: "This initial attach profile is not changed automatically.",
  fallback_profile_type_unavailable: "The type needed for initial attach profile configuration is not reported.",
};

/**
 * Already-translated attach fallback messages, indexed by the status the modem reports.
 * Each value is a getter so reading it during a render follows the active language.
 */
export const fallbackMessages: Record<string, string> = {};
for (const [status, id] of Object.entries(ATTACH_FALLBACK_TEXT)) {
  Object.defineProperty(fallbackMessages, status, { enumerable: true, get: () => t(id) });
}

/** The translated reason for an initial-attach fallback status, or null when there is none. */
export function attachFallbackMessage(attach: DataMap | null | undefined): string | null {
  const status = object(attach).status;
  if (typeof status !== 'string' || !status.startsWith('fallback_')) return null;
  return fallbackMessages[status] || t("Initial attach profile configuration is unavailable.");
}

/** The issues of one modem, from the live status and configuration of the selected one. */
export function useModemIssues(entry: ModemRecord | null | undefined): Issue[] {
  usePreferences();
  const { snapshot, config, freshness, selectedModem } = useSupervisor();
  const sample = snapshot?.sample;
  const connection = object(sample?.collection?.connection || snapshot?.connection);
  const apn = apnPresentation(connection);
  const traffic = trafficPresentation(sample, config?.telemetry?.enabled !== false && config?.telemetry?.throughput === true);
  const gps = gpsPresentation(sample, config?.telemetry?.enabled !== false && config?.telemetry?.gps === true);
  const attachFallback = attachFallbackMessage(object(sample?.collection?.connection?.attach));
  const connectivity = snapshot?.network?.connectivity;
  return collectIssues({
    snapshot: snapshot ?? null,
    entry: entry ?? null,
    config,
    freshness,
    telemetryFeature: snapshot?.features?.telemetry ?? entry?.features?.telemetry ?? null,
    apn: config?.telemetry?.enabled === false ? null : apn,
    routes: connectivity
      ? { conflict: connectivity.routes_conflict ?? null, error: connectivity.routes_error ?? null }
      : null,
    traffic: { error: traffic.error, enabled: traffic.enabled },
    gps: { error: gps.error, enabled: gps.enabled },
    attachFallback,
    modemId: entry?.id ?? selectedModem,
    // D3 log label published by the modem worker (M13/M22 contract field).
    label: typeof snapshot?.log_label === 'string' ? snapshot.log_label : null,
    nextSeconds: snapshot?.retry_in_seconds ?? entry?.features?.connection?.retry_in_seconds ?? null,
  });
}

/** The moment an issue has been failing since, in the language's short date and medium time. */
function failingLabel(since: string, attempts?: number): string {
  const parsed = new Date(since);
  const time = Number.isNaN(parsed.getTime())
    ? since
    : new Intl.DateTimeFormat(intlLocale(), { dateStyle: 'short', timeStyle: 'medium' }).format(parsed);
  const count = typeof attempts === 'number' && attempts > 0 ? attempts : 1;
  return count === 1
    ? t("Failing since {time} (1 attempt)", { time })
    : t("Failing since {time} ({attempts} attempts)", { time, attempts: count });
}

/** A link already pointing at the journal keeps its own label; no other log link is added. */
const isLogLink = (link?: IssueLink) => Boolean(link && link.href.includes('section=log'));

/** Code, step and operation of a diagnosis, kept inside the collapsed technical detail. */
function technicalTags(technical: NonNullable<Issue['technical']>) {
  const parts = [technical.code, technical.step, technical.operation]
    .filter((value): value is string => typeof value === 'string' && value !== '');
  if (!parts.length) return null;
  return <p className="attention-technical-tags">{parts.map((value, index) => <code className="mono" key={index}>{value}</code>)}</p>;
}

/**
 * A compact trigger after the state pill. It opens the attention panel, which is rendered
 * through a portal so it overlays the page without ever moving the dashboard content.
 */
export function AttentionIndicator({ entry }: { entry?: ModemRecord | null }) {
  usePreferences();
  const { connected, freshness, selectedModem } = useSupervisor();
  const issues = useModemIssues(entry);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number; width: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  // A dead or hung worker never looks current: its last known issues stay muted.
  const stale = !connected || freshness.state_fresh !== true;
  const tone = attentionTone(issues);

  const close = () => { setOpen(false); trigger.current?.focus(); };

  function toggle() {
    if (open) { close(); return; }
    const box = trigger.current?.getBoundingClientRect();
    if (box) {
      const width = Math.min(420, window.innerWidth - 24);
      const left = Math.max(12, Math.min(box.left, window.innerWidth - width - 12));
      setPosition({ top: box.bottom, left, width });
    }
    setOpen(true);
  }

  useEffect(() => {
    if (!open) return;
    closeButton.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setOpen(false);
      trigger.current?.focus();
    };
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target && (trigger.current?.contains(target) || panel.current?.contains(target))) return;
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onPointer);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onPointer);
    };
  }, [open]);

  const logHref = '/configuration/web/?section=log&modem=' + encodeURIComponent(entry?.id ?? selectedModem);
  const overlay = open && position ? createPortal(
    <div ref={panel} id="attention-panel" role="dialog"
      aria-label={t("Issues of {modem}", { modem: entry ? modemName(entry) : t("Modem") })}
      className={'attention-panel' + (stale ? ' attention-panel-stale' : '')}
      style={{ '--attention-top': position.top + 'px', '--attention-left': position.left + 'px',
        '--attention-width': position.width + 'px' } as CSSProperties}>
      <div className="attention-panel-head">
        <button type="button" ref={closeButton} className="attention-close" aria-label={t("Close")} onClick={close}>
          <Icon name="close" size={18} />
        </button>
      </div>
      {stale && <p className="attention-stale-note">{t("Observed {age} ago", { age: age(freshness.snapshot_age_ms) })}</p>}
      <ul className="attention-issues">
        {issues.map(issue => <li key={issue.id} className={'attention-issue' + (issue.tone === 'info' ? ' attention-issue-info' : '')}>
          <h3>{issueText(issue.title, issue.vars)}</h3>
          {issue.explanation && <p>{issueText(issue.explanation, issue.vars)}</p>}
          {issue.action && <div className="attention-action">
            <span className="attention-action-label">{t("What to do")}</span>
            <p>{issueText(issue.action, issue.vars)}</p>
          </div>}
          {issue.lines?.map((line, index) => <p key={index} className="attention-line">{line}</p>)}
          {issue.link && <Link className="attention-link" href={issue.link.href} prefetch={false}>
            {t(issue.link.label)} <Icon name="arrow-up-right" size={14} /></Link>}
          {issue.since && <p className="attention-meta">{failingLabel(issue.since, issue.attempts)}</p>}
          {typeof issue.nextSeconds === 'number' && issue.nextSeconds > 0 && <p className="attention-meta">
            {t("Next attempt in {seconds} s", { seconds: Math.ceil(issue.nextSeconds) })}</p>}
          {issue.technical && <details className="attention-technical">
            <summary>{t("Technical detail")}</summary>
            <div className="attention-technical-body">
              {technicalTags(issue.technical)}
              {issue.technical.label && <p className="attention-meta">{t("Log label: {label}", { label: issue.technical.label })}</p>}
              {issue.technical.detail && <code className="mono attention-raw">{issue.technical.detail}</code>}
              {!isLogLink(issue.link) && <Link className="attention-link" href={logHref} prefetch={false}>
                {t("Open the system log")} <Icon name="arrow-up-right" size={14} /></Link>}
            </div>
          </details>}
        </li>)}
      </ul>
    </div>, document.body) : null;

  if (stale) {
    if (!issues.length) return null;
    return <>
      <button type="button" ref={trigger} className="attention-indicator attention-indicator-stale"
        aria-haspopup="dialog" aria-expanded={open} aria-controls="attention-panel" aria-label={t("Show issues")}
        onClick={toggle}>
        <Icon name="info" size={15} />
        <span className="attention-indicator-label">{t("Unconfirmed state")}</span>
      </button>
      {overlay}
    </>;
  }

  if (!tone) return null;
  return <>
    <button type="button" ref={trigger} className={'attention-indicator tone-' + tone}
      aria-haspopup="dialog" aria-expanded={open} aria-controls="attention-panel" aria-label={t("Show issues")}
      onClick={toggle}>
      <Icon name={tone === 'bad' ? 'alert' : 'info'} size={15} />
      <span className="attention-indicator-label">{t("Needs attention")}</span>
      {issues.length > 1 && <span className="attention-indicator-count">{t("{count} issues", { count: issues.length })}</span>}
    </button>
    {overlay}
  </>;
}
