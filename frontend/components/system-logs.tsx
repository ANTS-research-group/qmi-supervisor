'use client';

import { intlLocale, t } from '@/lib/messages';
import { usePreferences } from './preferences';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Icon } from '@/components/icon';
import { Switch } from './form-controls';

const tone = (line: string) => /\b(ERROR|CRITICAL)\b/.test(line) ? ' is-error' : /\bWARNING\b/.test(line) ? ' is-warning' : '';

/**
 * Supervisor journal, read through the authenticated API. It is the content of its own System section,
 * so nothing is fetched until that section is opened; following keeps the newest line in view.
 */
export function SystemLog() {
  usePreferences();
  const [follow, setFollow] = useState(true);
  const [lines, setLines] = useState<string[] | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [updated, setUpdated] = useState<number | null>(null);
  const request = useRef(0), box = useRef<HTMLDivElement>(null), atEnd = useRef(true);

  const load = useCallback(async () => {
    const ticket = ++request.current;
    setLoading(true);
    try {
      const response = await fetch('/api/logs?lines=200', { credentials: 'same-origin', cache: 'no-store' });
      const body = await response.json().catch(() => ({}));
      if (ticket !== request.current) return;
      if (!response.ok) { setError(String(body.error || "The system log is not available.")); return; }
      setLines(Array.isArray(body.lines) ? body.lines.map(String) : []);
      setError('');
      setUpdated(Date.now());
    } catch {
      if (ticket === request.current) setError("The system log is not available.");
    } finally {
      if (ticket === request.current) setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!follow) return;
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, 5000);
    return () => window.clearInterval(timer);
  }, [follow, load]);
  // Like tail -f: new lines keep the end in view unless the reader scrolled up to read.
  useLayoutEffect(() => {
    const element = box.current;
    if (element && follow && atEnd.current) element.scrollTop = element.scrollHeight;
  }, [lines, follow]);

  return <div className="system-log">
    <div className="system-log-toolbar">
      <label className="card-switch" htmlFor="system-log-follow"><span>{t("Follow new lines")}</span>
        <Switch id="system-log-follow" checked={follow} onChange={event => { atEnd.current = true; setFollow(event.currentTarget.checked); }} /></label>
      <span className="system-log-meta muted">{t("Last 200 lines of the supervisor unit.")}
        {updated !== null && ' ' + t("Updated at {time}.", { time: new Date(updated).toLocaleTimeString(intlLocale()) })}</span>
      <button type="button" id="system-log-refresh" className="button button-secondary" disabled={loading} onClick={() => { atEnd.current = true; void load(); }}>
        {loading ? <span className="spinner" /> : <Icon name="refresh" size={15} />}{t("Refresh log")}</button>
    </div>
    {error && <p className="field-message field-message-error" role="alert"><Icon name="alert-circle" size={15} /><span>{t(error)}</span></p>}
    {lines === null ? !error && <p className="muted system-log-status" role="status">{t("Loading…")}</p>
      : lines.length ? <div ref={box} className="table-wrap metrics-table-wrap output-log" tabIndex={0} aria-label={t("System log lines")}
        onScroll={event => { const element = event.currentTarget; atEnd.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24; }}>
        <pre className="metric-json">{lines.map((line, index) => <span key={index} className={'log-line' + tone(line)}>{line + '\n'}</span>)}</pre>
      </div>
      : <div className="empty-state metric-empty"><Icon name="logs" size={24} /><p>{t("No log lines are available.")}</p></div>}
  </div>;
}
