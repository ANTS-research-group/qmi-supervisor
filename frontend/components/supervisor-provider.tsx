'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { AuthState, Config, ConfigResponse, ConfigValue, Freshness, MetricPoint, ModemRecord, Notice, Session, Snapshot, StatusResponse, Tone } from '@/lib/types';
import { countChanges, diffConfig, discardScopedDraft, mergeSavedConfig, sanitizeConfig, scopedChanges,
  scopedDraft, validateConfig } from '@/lib/config';
import type { SaveScope } from '@/lib/config';
import { detectedModems, pruneObjectiveOverrides, validModemId } from '@/lib/fleet';
import type { ObjectiveOverride } from '@/lib/fleet';
import { t } from '@/lib/messages';

class ApiError extends Error {
  constructor(message: string, public status = 0, public stale = false) { super(message); }
}
export type Objective = 'connection' | 'telemetry';
interface SupervisorContext {
  modems: ModemRecord[]; selectedModem: string; selectModem: (identifier: string) => void;
  /** Load another modem's configuration and status before the route changes. */
  prepareModem: (identifier: string) => Promise<void>;
  auth: AuthState; session: Session | null; connected: boolean;
  snapshot: Snapshot | null; freshness: Freshness; resourceHistory: MetricPoint[];
  config: Config | null; draft: Config | null; password: string;
  edit: (section: string, key: string, value: ConfigValue) => void;
  setPassword: (value: string) => void;
  saveConfig: (scope?: SaveScope) => Promise<void>; discardConfig: (scope?: SaveScope) => void;
  /** Without a modem it acts on the selected one; any inventory modem can be changed from the fleet. */
  setObjective: (name: Objective, enabled: boolean, modem?: string) => Promise<void>;
  objectiveBusy: string | null; objectiveOverrides: Record<string, ObjectiveOverride>;
  errors: Record<string, string>; dirtyCount: number; pendingByModem: Record<string, number>; webDirtyCount: number;
  saving: boolean; savingScope: SaveScope | null;
  editable: boolean; loggingOut: boolean;
  lastUpdated: number | null; listenUrls: string[]; notice: Notice | null;
  notify: (message: string, tone?: Tone) => void;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>; dismissNotice: () => void;
}
const Context = createContext<SupervisorContext | null>(null);
const modemKey = 'qmi-supervisor.modem';

/** The web section is shared by every modem; its account fields are saved from the profile page. */
const accountKeys = new Set(['username', 'password', 'session_timeout']);
const webPart = (web: Record<string, ConfigValue> | null | undefined, account: boolean) =>
  Object.fromEntries(Object.entries(web ?? {}).filter(([key]) => accountKeys.has(key) === account));

function requestedModem(): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get('modem');
  if (validModemId(fromUrl)) return fromUrl;
  try {
    const stored = sessionStorage.getItem(modemKey);
    return validModemId(stored) ? stored : null;
  } catch { return null; }
}

export function SupervisorProvider({ children }: { children: ReactNode }) {
  const [modems, setModems] = useState<ModemRecord[]>([]);
  const [selectedModem, setSelectedModem] = useState('default');
  const selectedModemRef = useRef('default');
  const modemGeneration = useRef(0);
  const modemDrafts = useRef(new Map<string, { config: Config | null; draft: Config | null; resources: MetricPoint[] }>());
  const prepared = useRef<{ id: string; config: Config; snapshot: Snapshot | null;
    freshness: Freshness; resources: MetricPoint[] } | null>(null);
  const sharedWebDraft = useRef<Config['web'] | null>(null);
  const pendingSelection = useRef<string | null>(null);
  const [selectionRequest, setSelectionRequest] = useState(0);
  const apiPath = useCallback((path: string, modem = selectedModemRef.current) => path + '?modem=' + encodeURIComponent(modem), []);
  const [auth, setAuth] = useState<AuthState>('checking');
  const [session, setSession] = useState<Session | null>(null);
  const [connected, setConnected] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [freshness, setFreshness] = useState<Freshness>({});
  const [resourceHistory, setResourceHistory] = useState<MetricPoint[]>([]);
  const [config, setConfig] = useState<Config | null>(null);
  const [draft, setDraft] = useState<Config | null>(null);
  const [password, setPassword] = useState('');
  const passwordRef = useRef(password);
  passwordRef.current = password;
  const [saving, setSaving] = useState(false);
  const [savingScope, setSavingScope] = useState<SaveScope | null>(null);
  const [objectiveBusy, setObjectiveBusy] = useState<string | null>(null);
  const [objectiveOverrides, setObjectiveOverrides] = useState<Record<string, ObjectiveOverride>>({});
  const saveBusy = useRef(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const logoutBusy = useRef(false);
  const [editable, setEditable] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [listenUrls, setListenUrls] = useState<string[]>([]);
  const [notice, setNotice] = useState<Notice | null>(null);
  const nextNoticeId = useRef(0);
  const generation = useRef(0);
  const sessionRef = useRef<Session | null>(null);
  const authRef = useRef<AuthState>('checking');
  const configRef = useRef<Config | null>(null);
  const configRevision = useRef<string | null>(null);
  const snapshotRevision = useRef<string | null>(null);
  const pending = useRef(new Set<AbortController>());
  const pollBusy = useRef(false);
  const bootstrapBusy = useRef(false);
  const mounted = useRef(false);
  const expiry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bootstrap = useRef<() => Promise<void>>(async () => {});
  const poll = useRef<() => Promise<void>>(async () => {});
  const notify = useCallback((message: string, tone: Tone = 'success') => setNotice(message ? { id: ++nextNoticeId.current, message, tone } : null), []);
  const dismissNotice = useCallback(() => setNotice(null), []);
  const invalidate = useCallback(() => {
    generation.current += 1;
    for (const controller of pending.current) controller.abort();
    pending.current.clear();
    pollBusy.current = false;
    if (expiry.current) clearTimeout(expiry.current);
  }, []);
  const expire = useCallback(() => {
    invalidate();
    authRef.current = 'required';
    setAuth('required'); setEditable(false); setSnapshot(null); setFreshness({});
    setResourceHistory([]); setLastUpdated(null); setListenUrls([]);
    sessionRef.current = sessionRef.current ? { ...sessionRef.current, authenticated: false, csrf_token: null } : null;
    setSession(sessionRef.current);
    window.getSelection()?.removeAllRanges();
  }, [invalidate]);

  const request = useCallback(async <T,>(path: string, body?: unknown, csrfOverride?: string | null): Promise<T> => {
    const epoch = generation.current;
    const selection = selectedModemRef.current;
    const scopeGeneration = modemGeneration.current;
    const scoped = path.startsWith('/api/config') || path.startsWith('/api/status');
    const controller = new AbortController();
    pending.current.add(controller);
    const timeout = setTimeout(() => controller.abort(), 6000);
    const headers: Record<string, string> = {};
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      const csrf = csrfOverride ?? sessionRef.current?.csrf_token;
      if (csrf) headers['X-CSRF-Token'] = csrf;
    }
    try {
      if (path.split('?')[0] === '/api/config' && body !== undefined && configRevision.current) headers['If-Match'] = configRevision.current;
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers,
        body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      const data = await response.json();
      if (epoch !== generation.current || !mounted.current || scoped &&
          (selection !== selectedModemRef.current || scopeGeneration !== modemGeneration.current)) throw new ApiError("Previous request discarded.", 0, true);
      setConnected(true);
      if (!response.ok) {
        if (response.status === 401 && !['/api/login', '/api/logout', '/api/session'].includes(path)) {
          expire();
          void bootstrap.current();
        }
        throw new ApiError(response.status === 401 ? (path === '/api/login' ? "Incorrect username or password." : "Your session expired. Sign in again.") : data.error || "The request could not be completed.", response.status);
      }
      return data as T;
    } catch (error) {
      if (epoch !== generation.current || !mounted.current || scoped &&
          (selection !== selectedModemRef.current || scopeGeneration !== modemGeneration.current)) throw new ApiError("Previous request discarded.", 0, true);
      if (!(error instanceof ApiError)) {
        setConnected(false);
        throw new ApiError("Cannot reach the supervisor. Retrying automatically.");
      }
      throw error;
    } finally {
      clearTimeout(timeout); pending.current.delete(controller);
    }
  }, [expire]);

  const applySession = useCallback((info: Session) => {
    sessionRef.current = info; setSession(info);
    authRef.current = info.authenticated ? 'authenticated' : 'required';
    setAuth(authRef.current); setEditable(info.authenticated);
    if (expiry.current) clearTimeout(expiry.current);
    if (info.authenticated && Number.isFinite(info.expires_in) && (info.expires_in ?? 0) > 0) {
      expiry.current = setTimeout(() => { expire(); void bootstrap.current(); }, info.expires_in! * 1000 + 50);
    }
  }, [expire]);

  const loadConfig = useCallback(async () => {
    const epoch = generation.current;
    const data = await request<ConfigResponse>(apiPath('/api/config'));
    if (epoch !== generation.current) return;
    const clean = sanitizeConfig(data.config);
    const previous = configRef.current;
    configRef.current = clean; setConfig(clean); configRevision.current = data.revision ?? null;
    setDraft(current => {
      const merged = previous ? mergeSavedConfig(previous, current, clean, {}) : structuredClone(clean);
      if (sharedWebDraft.current) merged.web = { ...clean.web, ...sharedWebDraft.current };
      return merged;
    });
    setEditable(Boolean(data.editable)); setListenUrls(data.listen_urls || []);
  }, [apiPath, request]);

  poll.current = async () => {
    if (document.hidden || pollBusy.current || authRef.current !== 'authenticated') return;
    const epoch = generation.current; pollBusy.current = true;
    try {
      const data = await request<StatusResponse>(apiPath('/api/status'));
      if (epoch !== generation.current) return;
      setSnapshot(data.snapshot); setFreshness(data.freshness || {}); setLastUpdated(Date.now());
      if (Array.isArray(data.snapshot?.modems)) setModems(data.snapshot.modems);
      snapshotRevision.current = typeof data.snapshot?.config_revision === 'string' ? data.snapshot.config_revision : null;
      setObjectiveOverrides(current => Object.keys(current).length ? pruneObjectiveOverrides(current, snapshotRevision.current) : current);
      const resources = data.snapshot?.resources;
      if (resources) setResourceHistory(current => {
        if (current.at(-1)?.time === resources.sampled_at_utc) return current;
        const point: MetricPoint = { time: resources.sampled_at_utc, monotonic: resources.sampled_monotonic };
        for (const name of ['supervisor', 'manager', 'modem', 'telemetry', 'web']) {
          const value = resources[name] || {};
          point[name + '_cpu'] = value.state === 'ok' ? value.cpu_percent : null;
          point[name + '_rss'] = typeof value.rss_bytes === 'number' ? value.rss_bytes / 1048576 : null;
          point[name + '_cpu_run_id'] = point[name + '_rss_run_id'] = String(value.pid) + ':' + String(value.starttime_ticks);
        }
        return [...current, point].slice(-Math.min(1000, Number(configRef.current?.web?.history_size) || 120));
      });
      if (!configRef.current || (data.snapshot?.config_revision && data.snapshot.config_revision !== configRevision.current)) await loadConfig();
    } catch (error) {
      if (error instanceof ApiError && error.stale) return;
    } finally { if (epoch === generation.current) pollBusy.current = false; }
  };
  bootstrap.current = async () => {
    if (bootstrapBusy.current || document.hidden) return;
    bootstrapBusy.current = true;
    try {
      const info = await request<Session>('/api/session');
      applySession(info);
      if (authRef.current === 'authenticated') { await loadConfig(); await poll.current(); }
    } catch (error) {
      if (error instanceof ApiError && !error.stale) setConnected(false);
    } finally { bootstrapBusy.current = false; }
  };

  useEffect(() => {
    mounted.current = true;
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;
    let running = false;
    // The system configuration owns the cadence; the browser never goes below 1 s.
    const pollDelay = () => {
      const seconds = Number(configRef.current?.web?.poll_interval);
      return (Number.isFinite(seconds) ? Math.min(30, Math.max(1, seconds)) : 2) * 1000;
    };
    const tick = async () => {
      if (running || cancelled || document.hidden) return;
      running = true;
      try {
        if (authRef.current === 'checking' || !sessionRef.current) await bootstrap.current();
        else await poll.current();
      } finally {
        running = false;
        clearTimeout(timer);
        if (!cancelled && !document.hidden) timer = setTimeout(tick, pollDelay());
      }
    };
    void tick();
    const visibility = () => { clearTimeout(timer); if (!document.hidden) void tick(); };
    document.addEventListener('visibilitychange', visibility);
    return () => {
      cancelled = true; mounted.current = false; invalidate(); bootstrapBusy.current = false;
      clearTimeout(timer); document.removeEventListener('visibilitychange', visibility);
    };
  }, [invalidate]);

  // A link, reload or history entry names its modem; the inventory confirms it exists.
  useEffect(() => {
    pendingSelection.current = requestedModem();
    setSelectionRequest(value => value + 1);
    const history = () => { pendingSelection.current = requestedModem(); setSelectionRequest(value => value + 1); };
    window.addEventListener('popstate', history);
    return () => window.removeEventListener('popstate', history);
  }, []);

  const login = useCallback(async (username: string, secret: string) => {
    if (logoutBusy.current) throw new ApiError("Signing out of the previous session. Please wait.");
    invalidate();
    const epoch = generation.current;
    const info = await request<Session>('/api/login', { username, password: secret });
    if (epoch !== generation.current) return;
    applySession(info); await loadConfig(); await poll.current();
    notify("Signed in.");
  }, [applySession, invalidate, loadConfig, notify, request]);
  const logout = useCallback(async () => {
    if (logoutBusy.current) return;
    logoutBusy.current = true; setLoggingOut(true);
    const csrf = sessionRef.current?.csrf_token;
    expire();
    configRef.current = null; setConfig(null); setDraft(null); setPassword('');
    modemDrafts.current.clear(); sharedWebDraft.current = null;
    setModems([]); setObjectiveOverrides({});
    try { sessionStorage.removeItem(modemKey); } catch {}
    try {
      if (csrf) await request('/api/logout', {}, csrf);
      notify("Signed out.", 'info');
    } catch (error) {
      if (error instanceof ApiError && error.stale) return;
      notify(error instanceof ApiError && error.status === 401 ? "Signed out." : "Data has been hidden. Server sign-out could not be confirmed.", 'warning');
    } finally { logoutBusy.current = false; setLoggingOut(false); }
  }, [expire, notify, request]);

  const edit = useCallback((section: string, key: string, value: ConfigValue) => {
    setDraft(current => {
      if (!current) return current;
      const next = { ...current, [section]: { ...current[section], [key]: value } };
      if (section === 'web') sharedWebDraft.current = diffConfig(configRef.current, next, '').web || null;
      return next;
    });
  }, []);
  const discardConfig = useCallback((scope?: SaveScope) => {
    const base = configRef.current;
    setDraft(current => discardScopedDraft(base, current, scope));
    if (!scope || scope !== 'profile') {
      if (!scope || scope === 'account') setPassword('');
      // The account and the web interface share a section but not a draft.
      const account = scope === 'account';
      sharedWebDraft.current = account
        ? Object.keys(webPart(sharedWebDraft.current, false)).length ? webPart(sharedWebDraft.current, false) : null
        : null;
      if (base) for (const cached of modemDrafts.current.values()) {
        const discarded = account ? webPart(base.web, true) : base.web;
        if (cached.config) cached.config = { ...cached.config, web: { ...cached.config.web, ...discarded } };
        if (cached.draft) cached.draft = { ...cached.draft, web: { ...cached.draft.web, ...discarded } };
      }
    }
    notify("Unsaved changes discarded.", 'info');
  }, [notify]);
  const errors = useMemo(() => validateConfig(draft, password), [draft, password]);
  // Shared web edits travel with every cached draft, so per-modem counts exclude them.
  const pendingByModem: Record<string, number> = Object.fromEntries([...modemDrafts.current]
    .filter(([key]) => key !== selectedModem)
    .map(([key, value]) => [key, countChanges(scopedChanges(value.config, value.draft, '', 'profile'))]));
  pendingByModem[selectedModem] = countChanges(scopedChanges(config, draft, '', 'profile'));
  const webDirtyCount = countChanges(scopedChanges(config, draft, password, 'web'));
  const accountDirtyCount = countChanges(scopedChanges(config, draft, password, 'account'));
  const dirtyCount = Object.values(pendingByModem).reduce((total, count) => total + count, 0) + webDirtyCount + accountDirtyCount;
  const prepareModem = useCallback(async (identifier: string) => {
    if (identifier === selectedModemRef.current) return;
    const cached = modemDrafts.current.get(identifier), query = '?modem=' + encodeURIComponent(identifier);
    const [config, status] = await Promise.all([
      request<ConfigResponse>('/api/config' + query),
      request<StatusResponse>('/api/status' + query),
    ]);
    if (identifier === selectedModemRef.current) return;
    prepared.current = { id: identifier, config: sanitizeConfig(config.config),
      snapshot: status.snapshot ?? null, freshness: status.freshness || {}, resources: cached?.resources ?? [] };
  }, [request]);
  const selectModem = useCallback((identifier: string) => {
    if (identifier === selectedModemRef.current || saveBusy.current || !detectedModems(modems).some(modem => modem.id === identifier)) return;
    // A prepared switch renders the target page complete; otherwise it starts empty.
    const ready = prepared.current?.id === identifier ? prepared.current : null;
    prepared.current = null;
    modemDrafts.current.set(selectedModemRef.current, { config, draft, resources: resourceHistory });
    modemGeneration.current += 1;
    selectedModemRef.current = identifier; setSelectedModem(identifier);
    try { sessionStorage.setItem(modemKey, identifier); } catch {}
    if (ready) {
      const cached = modemDrafts.current.get(identifier), clean = ready.config;
      // Unsaved edits of the target survive the switch, exactly as a normal load rebases them.
      const nextDraft = clean ? mergeSavedConfig(cached?.config ?? clean, cached?.draft ?? clean, clean, {}) : null;
      configRef.current = clean;
      setConfig(clean); setDraft(nextDraft); setSnapshot(ready.snapshot);
      setFreshness(ready.freshness); setLastUpdated(Date.now()); setResourceHistory(ready.resources);
      void poll.current();
      return;
    }
    const cached = modemDrafts.current.get(identifier);
    configRef.current = cached?.config ?? null;
    setConfig(cached?.config ?? null);
    setDraft(cached?.draft ?? null); setResourceHistory(cached?.resources ?? []);
    setSnapshot(null); setFreshness({}); setLastUpdated(null);
    void loadConfig().then(() => poll.current()).catch(error => {
      if (!(error instanceof ApiError && error.stale)) notify(error instanceof Error ? error.message : 'Could not load modem.', 'error');
    });
  }, [config, draft, loadConfig, modems, notify, resourceHistory]);
  useEffect(() => {
    const requested = pendingSelection.current;
    if (!requested || !modems.length) return;
    pendingSelection.current = null;
    if (detectedModems(modems).some(modem => modem.id === requested)) selectModem(requested);
    else try { if (sessionStorage.getItem(modemKey) === requested) sessionStorage.removeItem(modemKey); } catch {}
  }, [modems, selectModem, selectionRequest]);
  // An unplugged modem leaves the fleet; the page moves to a modem that is still present.
  useEffect(() => {
    const detected = detectedModems(modems);
    if (!detected.length || detected.some(modem => modem.id === selectedModemRef.current)) return;
    const next = detected.find(modem => modem.id === 'default') ?? detected[0];
    selectModem(next.id);
  }, [modems, selectModem]);
  const saveConfig = useCallback(async (scope?: SaveScope) => {
    const base = configRef.current;
    const submitted = scopedChanges(base, draft, password, scope);
    const shared = scope !== 'profile', withPassword = !scope || scope === 'account';
    const candidateErrors = validateConfig(scopedDraft(base, draft, scope), withPassword ? password : '');
    if (!editable || saveBusy.current || !countChanges(submitted) || Object.keys(candidateErrors).length) return;
    const epoch = generation.current, submittedPassword = password;
    saveBusy.current = true; setSaving(true); setSavingScope(scope ?? null);
    try {
      // The web section is shared: writing it through the default profile can never create a profile.
      const target = shared ? 'default' : selectedModemRef.current;
      const data = await request<ConfigResponse>(apiPath('/api/config', target), { config: submitted });
      if (epoch !== generation.current) return;
      configRevision.current = data.revision ?? null;
      const saved = sanitizeConfig(data.config);
      const clean = shared && base ? { ...base, web: saved.web } : saved;
      configRef.current = clean; setConfig(clean);
      setDraft(current => mergeSavedConfig(base, current, clean, submitted));
      if (shared) {
        // Saving the account keeps pending web-access edits, and saving web access keeps the account draft.
        const account = scope === 'account', applied = account ? webPart(clean.web, true) : clean.web;
        sharedWebDraft.current = account && sharedWebDraft.current ? webPart(sharedWebDraft.current, false) : null;
        if (sharedWebDraft.current && !Object.keys(sharedWebDraft.current).length) sharedWebDraft.current = null;
        for (const cached of modemDrafts.current.values()) {
          if (cached.config) cached.config = { ...cached.config, web: { ...cached.config.web, ...applied } };
          if (cached.draft) cached.draft = { ...cached.draft, web: { ...cached.draft.web, ...applied } };
        }
        if (scope !== 'web' && passwordRef.current === submittedPassword) setPassword('');
      }
      if ('password' in (submitted.web || {}) || 'username' in (submitted.web || {})) {
        expire(); await bootstrap.current();
        notify("Access updated. Sign in with your new credentials.", 'info');
      } else notify("Configuration saved.");
    } catch (error) {
      if (error instanceof ApiError && error.stale) return;
      notify(error instanceof Error ? error.message : "Could not save configuration.", 'error');
    } finally { saveBusy.current = false; setSaving(false); setSavingScope(null); }
  }, [apiPath, draft, editable, expire, notify, password, request]);

  const setObjective = useCallback(async (name: Objective, enabled: boolean, modem?: string) => {
    const target = modem ?? selectedModemRef.current, selected = target === selectedModemRef.current;
    const base = configRef.current;
    if (!editable || saveBusy.current || (selected && !base)) return;
    const submitted: Config = name === 'connection' ? { modem: { mode: enabled ? 'connect' : 'monitor' } } : { telemetry: { enabled } };
    const epoch = generation.current, key = target + ':' + name, revision = snapshotRevision.current;
    saveBusy.current = true; setObjectiveBusy(key);
    try {
      const result = await request<ConfigResponse>(apiPath('/api/config', target), { config: submitted });
      if (epoch !== generation.current) return;
      configRevision.current = result.revision ?? null;
      const clean = sanitizeConfig(result.config);
      if (selected) {
        configRef.current = clean; setConfig(clean);
        setDraft(current => mergeSavedConfig(base, current, clean, {}));
      } else {
        // Another modem's untouched fields follow the server; its pending edits stay cached.
        const cached = modemDrafts.current.get(target);
        if (cached?.config) modemDrafts.current.set(target, { ...cached, config: clean, draft: mergeSavedConfig(cached.config, cached.draft, clean, {}) });
        setObjectiveOverrides(current => ({ ...current, [key]: { enabled, revision } }));
      }
      await poll.current();
    } catch (error) {
      if (error instanceof ApiError && error.stale) return;
      if (error instanceof ApiError && error.status === 412) await loadConfig();
      const message = error instanceof Error ? error.message : 'Could not save configuration.';
      notify(selected ? message : target + ': ' + t(message), 'error');
    } finally { saveBusy.current = false; setObjectiveBusy(null); }
  }, [apiPath, editable, loadConfig, notify, request]);

  return <Context.Provider value={{ modems, selectedModem, selectModem, prepareModem, auth, session, connected, snapshot, freshness, resourceHistory,
    config, draft, password, edit, setPassword,
    saveConfig, discardConfig, setObjective, objectiveBusy, objectiveOverrides, errors, dirtyCount, pendingByModem, webDirtyCount,
    saving, savingScope, editable, loggingOut, lastUpdated, listenUrls, notice,
    notify, dismissNotice, login, logout,
    refresh: () => ['checking', 'required'].includes(authRef.current) ? bootstrap.current() : poll.current() }}>{children}</Context.Provider>;
}

export function useSupervisor() {
  const context = useContext(Context);
  if (!context) throw new Error('useSupervisor requires SupervisorProvider');
  return context;
}
