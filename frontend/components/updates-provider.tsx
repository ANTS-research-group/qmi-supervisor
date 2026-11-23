'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { t } from '@/lib/messages';
import { BUILD_VERSION } from '@/lib/version';
import { availableUpdate, failureKeys, reconnectAction, updateErrorText } from '@/lib/updates';
import type { UpdatePending, UpdateResponse } from '@/lib/updates';
import { useSupervisor } from './supervisor-provider';

export type UpdatePhase = 'idle' | 'checking' | 'starting' | 'running' | 'restarting' | 'reload-required';

export class UpdateRequestError extends Error {
  constructor(message: string, public status = 0, public code: string | null = null, public retryAfter = 0) {
    super(message);
    this.name = 'UpdateRequestError';
  }
}

interface UpdatesContext {
  data: UpdateResponse | null;
  phase: UpdatePhase;
  pending: UpdatePending | null;
  pageVersion: string | null;
  downSince: number | null;
  seen: string[];
  retryAfter: number;
  refresh: () => void;
  watch: () => () => void;
  startCheck: () => Promise<void>;
  startInstall: (version: string, allowDowngrade: boolean, password: string) => Promise<void>;
  startRollback: (password: string) => Promise<void>;
  markSeen: () => void;
}

const Context = createContext<UpdatesContext | null>(null);
const PENDING_KEY = 'qmi-supervisor.update-pending';
const SEEN_KEY = 'qmi-supervisor.update-seen';
const RESTARTING = 'restarting';

function readPending(): UpdatePending | null {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (!value || typeof value !== 'object') return null;
    if (value.action !== 'install' && value.action !== 'rollback') return null;
    if (typeof value.target !== 'string' || typeof value.started_ms !== 'number') return null;
    return value as UpdatePending;
  } catch { return null; }
}
function writePending(value: UpdatePending | null) {
  try {
    if (value) sessionStorage.setItem(PENDING_KEY, JSON.stringify(value));
    else sessionStorage.removeItem(PENDING_KEY);
  } catch { /* Storage may be disabled; the marker is only a convenience. */ }
}
function readSeen(): string[] {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    const value = raw ? JSON.parse(raw) : [];
    return Array.isArray(value) ? value.filter(item => typeof item === 'string') : [];
  } catch { return []; }
}
function writeSeen(values: string[]) {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(values)); } catch { /* Storage may be disabled. */ }
}

/**
 * One update state for the whole page: the section, the System dot and the
 * sidebar line read the same GET. It polls only while signed in and visible,
 * fast only while a check, install or restart is in progress.
 */
export function UpdatesProvider({ children }: { children: ReactNode }) {
  const { auth, session, dirtyCount, notify, refresh: refreshSession } = useSupervisor();
  const [data, setData] = useState<UpdateResponse | null>(null);
  const [phase, setPhaseState] = useState<UpdatePhase>('idle');
  const [pending, setPendingState] = useState<UpdatePending | null>(() => readPending());
  const [pageVersion, setPageVersionState] = useState<string | null>(BUILD_VERSION === 'dev' ? null : BUILD_VERSION);
  const [downSince, setDownSince] = useState<number | null>(null);
  const [seen, setSeenState] = useState<string[]>(() => readSeen());
  const [retryAfter, setRetryAfterState] = useState(0);

  const authRef = useRef(auth);
  authRef.current = auth;
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const dirtyRef = useRef(dirtyCount);
  dirtyRef.current = dirtyCount;
  const dataRef = useRef<UpdateResponse | null>(null);
  const phaseRef = useRef<UpdatePhase>('idle');
  const pendingRef = useRef<UpdatePending | null>(pending);
  const pageRef = useRef<string | null>(pageVersion);
  const seenRef = useRef<string[]>(seen);
  const watchers = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const running = useRef(false);
  const busy = useRef(false);
  const checkReady = useRef(0);
  const mounted = useRef(false);
  const kickRef = useRef<() => void>(() => {});
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const refreshSessionRef = useRef(refreshSession);
  refreshSessionRef.current = refreshSession;

  const setPhase = useCallback((next: UpdatePhase) => { phaseRef.current = next; setPhaseState(next); }, []);
  const rememberPending = useCallback((value: UpdatePending | null) => {
    pendingRef.current = value; setPendingState(value); writePending(value);
  }, []);

  const request = useCallback(async <T,>(path: string, body?: unknown, timeout = 8000): Promise<T> => {
    const controller = new AbortController();
    const limit = setTimeout(() => controller.abort(), timeout);
    const headers: Record<string, string> = {};
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      const csrf = sessionRef.current?.csrf_token;
      if (csrf) headers['X-CSRF-Token'] = csrf;
    }
    try {
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers,
        body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store',
        signal: controller.signal });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new UpdateRequestError(String(payload.error || "The request could not be completed."), response.status,
          typeof payload.code === 'string' ? payload.code : null, Number(payload.retry_after) || 0);
      }
      return payload as T;
    } catch (error) {
      if (error instanceof UpdateRequestError) throw error;
      throw new UpdateRequestError("Cannot reach the supervisor. Retrying automatically.", 0);
    } finally { clearTimeout(limit); }
  }, []);

  const finishCheck = useCallback((next: UpdateResponse) => {
    const available = availableUpdate(next);
    if (next.check.state === 'failed') {
      notifyRef.current(t("The last check failed: {reason}", {
        reason: t(updateErrorText(next.check.error_code), { code: next.check.error_code ?? '' }),
      }), 'error');
    } else if (available) {
      notifyRef.current(t("Update available: {version}", { version: available.version }), 'info');
    } else {
      notifyRef.current(t("Up to date"), 'success');
    }
    setPhase('idle');
  }, [setPhase]);

  const apply = useCallback((next: UpdateResponse) => {
    dataRef.current = next; setData(next); setDownSince(null);
    if (BUILD_VERSION === 'dev' && pageRef.current === null && next.installed?.version) {
      pageRef.current = next.installed.version; setPageVersionState(next.installed.version);
    }
    setRetryAfterState(next.check.retry_after);
    // A check this tab started ends when the unit is no longer running and at least 2 s passed.
    if (phaseRef.current === 'checking' && !pendingRef.current && !next.check.running && Date.now() >= checkReady.current) {
      finishCheck(next);
    }
    const marker = pendingRef.current;
    const action = reconnectAction({ pending: marker, installed: next.installed.version, pageVersion: pageRef.current,
      install: next.install, unsaved: dirtyRef.current > 0, now: Date.now() });
    switch (action.kind) {
      case 'reload':
        // Keep the marker: it is consumed after the reload, when the new build reports itself.
        window.location.reload();
        return;
      case 'reload-prompt':
        setPhase('reload-required');
        return;
      case 'expired':
        rememberPending(null); setPhase('idle');
        return;
      case 'succeeded':
        rememberPending(null); setPhase('idle');
        notifyRef.current(t(action.action === 'rollback' ? "Version {version} was restored." : "Updated to version {version}.",
          { version: action.version }), 'success');
        return;
      case 'failed':
        rememberPending(null); setPhase('idle');
        notifyRef.current(action.rolledBack && action.previous
          ? t("The update to {version} failed. Version {previous} was restored.", { version: action.version, previous: action.previous })
          : t("The update to {version} failed.", { version: action.version }), 'error');
        return;
      case 'wait':
        if (next.install?.running) setPhase('running');
        else if (phaseRef.current !== RESTARTING && phaseRef.current !== 'reload-required') setPhase('starting');
        return;
      default:
        // No marker and nothing to prompt: recover from a restart back to idle.
        if (!marker && phaseRef.current === RESTARTING) setPhase('idle');
    }
  }, [finishCheck, rememberPending, setPhase]);
  const applyRef = useRef(apply);
  applyRef.current = apply;

  const load = useCallback(async () => {
    try {
      const next = await request<UpdateResponse>('/api/update');
      applyRef.current(next);
    } catch (error) {
      if (!(error instanceof UpdateRequestError)) return;
      if (error.status === 401) { refreshSessionRef.current(); return; }
      if (error.status === 0 && (pendingRef.current || ['starting', 'running'].includes(phaseRef.current))) {
        setDownSince(current => current ?? Date.now());
        setPhase(RESTARTING);
      }
    }
  }, [request, setPhase]);
  const loadRef = useRef(load);
  loadRef.current = load;

  const cadence = useCallback((): number => {
    const current = dataRef.current;
    if (['checking', 'starting', 'running', RESTARTING].includes(phaseRef.current) || pendingRef.current
        || current?.check?.running || current?.install?.running) return 2000;
    return watchers.current > 0 ? 10000 : 600000;
  }, []);

  // One timer chain: it survives re-renders and is rescheduled on demand.
  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    const plan = (delay: number) => {
      if (timer.current) clearTimeout(timer.current);
      if (!cancelled) timer.current = setTimeout(() => { void tick(); }, delay);
    };
    const tick = async () => {
      if (cancelled || running.current) return;
      if (document.hidden || authRef.current !== 'authenticated') { plan(1000); return; }
      running.current = true;
      try {
        if (phaseRef.current === RESTARTING) {
          const info = await request<{ authenticated?: boolean }>('/api/session').catch(() => null);
          if (info && info.authenticated === false) { refreshSessionRef.current(); return; }
          if (info) await loadRef.current();
        } else {
          await loadRef.current();
        }
      } finally {
        running.current = false;
        if (!cancelled) plan(cadence());
      }
    };
    kickRef.current = () => { if (timer.current) clearTimeout(timer.current); void tick(); };
    plan(0);
    const visibility = () => { if (!document.hidden) kickRef.current(); };
    document.addEventListener('visibilitychange', visibility);
    return () => {
      cancelled = true; mounted.current = false;
      if (timer.current) clearTimeout(timer.current);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [cadence, request]);

  // Signing in refreshes at once; signing out clears the data but keeps the marker.
  useEffect(() => {
    if (auth === 'authenticated') { kickRef.current(); return; }
    dataRef.current = null; setData(null); setDownSince(null); setPhase('idle');
  }, [auth, setPhase]);

  // The check limit counts down on its own, so the button re-enables when it ends.
  useEffect(() => {
    if (retryAfter <= 0) return;
    const timer = setTimeout(() => setRetryAfterState(value => Math.max(0, value - 1)), 1000);
    return () => clearTimeout(timer);
  }, [retryAfter]);

  const refresh = useCallback(() => { kickRef.current(); }, []);

  const watch = useCallback(() => {
    watchers.current += 1;
    kickRef.current();
    return () => { watchers.current = Math.max(0, watchers.current - 1); };
  }, []);

  const startCheck = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    checkReady.current = Date.now() + 2000;
    setPhase('checking');
    try {
      await request('/api/update/check', {}, 15000);
      kickRef.current();
    } catch (error) {
      setPhase('idle');
      if (error instanceof UpdateRequestError) {
        if (error.status === 401) { refreshSessionRef.current(); return; }
        if (error.status === 429) {
          const seconds = error.retryAfter || 60;
          setRetryAfterState(seconds);
          notifyRef.current(t("Wait a minute before checking again."), 'warning');
          return;
        }
        notifyRef.current(error.message, 'error');
      }
    } finally { busy.current = false; }
  }, [request, setPhase]);

  const begin = useCallback(async (action: 'install' | 'rollback', body: Record<string, unknown>, version: string) => {
    const result = await request<{ version?: string; from_version?: string }>(
      action === 'install' ? '/api/update/install' : '/api/update/rollback', body, 15000);
    const target = action === 'install' ? version : (result.version || version);
    rememberPending({ action, target, from: result.from_version ?? null,
      baseline: dataRef.current?.install?.started_at_utc ?? null, started_ms: Date.now() });
    setPhase('starting');
    kickRef.current();
  }, [rememberPending, request, setPhase]);

  const startInstall = useCallback(async (version: string, allowDowngrade: boolean, password: string) => {
    try {
      await begin('install', { version, allow_downgrade: allowDowngrade, password }, version);
    } catch (error) {
      if (error instanceof UpdateRequestError && error.status === 401) { refreshSessionRef.current(); return; }
      throw error;
    }
  }, [begin]);

  const startRollback = useCallback(async (password: string) => {
    const version = dataRef.current?.rollback?.version ?? '';
    try {
      await begin('rollback', { password }, version);
    } catch (error) {
      if (error instanceof UpdateRequestError && error.status === 401) { refreshSessionRef.current(); return; }
      throw error;
    }
  }, [begin]);

  const markSeen = useCallback(() => {
    const keys = failureKeys(dataRef.current);
    seenRef.current = keys; setSeenState(keys); writeSeen(keys);
  }, []);

  const value = useMemo<UpdatesContext>(() => ({ data, phase, pending, pageVersion, downSince, seen, retryAfter,
    refresh, watch, startCheck, startInstall, startRollback, markSeen }),
    [data, phase, pending, pageVersion, downSince, seen, retryAfter, refresh, watch, startCheck, startInstall, startRollback, markSeen]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useUpdates() {
  const context = useContext(Context);
  if (!context) throw new Error('useUpdates requires UpdatesProvider');
  return context;
}
