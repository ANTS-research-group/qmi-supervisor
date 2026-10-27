'use client';

import { t } from '@/lib/messages';
import { usePreferences, PreferenceControls } from './preferences';
import { intlLocale } from '@/lib/messages';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { MouseEvent, ReactNode } from 'react';
import { Icon } from './icon';
import { useSupervisor } from './supervisor-provider';
import { LoginScreen } from './login-screen';
import { ToastRegion } from './toast-region';
import { AccountMenu } from './account-menu';
import { PageActionsProvider } from './page-actions';
import { ErrorCount, PendingDot } from './form-controls';
import { FleetHeading, ModemHeader, ModemList, useFleet } from './fleet';
import { requestConfigSection } from './configuration';
import { countChanges, fieldDefinition, scopedChanges, scopedErrors } from '@/lib/config';
import type { SaveScope } from '@/lib/config';
import { displayValue, saveEffects, sectionOf, sectionsFor, valueAt } from '@/lib/config-layout';
import { modemName, modemTab, pageKind } from '@/lib/fleet';

const metricTabs = ['charts', 'data'];
const configTabs = ['modem', 'telemetry', 'web'];
const normalized = (pathname: string) => pathname.endsWith('/') ? pathname : pathname + '/';

/** Heading shared by the pages that do not belong to a single modem. */
function PageHeading({ title, subtitle }: { title: string; subtitle: string }) {
  usePreferences();
  const { refresh } = useSupervisor();
  return <div className="page-heading"><div><h1>{title}</h1><p>{subtitle}</p></div>
    <div className="page-heading-actions"><button className="icon-button refresh-button" title={t("Refresh data")} aria-label={t("Refresh data")}
      onClick={() => void refresh()}><Icon name="refresh" size={17} /></button></div></div>;
}

function SystemHeading() {
  usePreferences();
  return <PageHeading title={t("System")} subtitle={t("Shared by all modems.")} />;
}

function ProfileHeading() {
  usePreferences();
  return <PageHeading title={t("Profile")} subtitle={t("Your account and sign-in details.")} />;
}

function ChangeItem({ path, scope }: { path: string; scope: SaveScope }) {
  usePreferences();
  const { config, draft } = useSupervisor();
  const section = sectionOf(path, sectionsFor(scope));
  const [group, key] = path.split('.');
  const show = (value: ReturnType<typeof displayValue>) => value.translate ? t(value.text) : value.text;
  const secret = path === 'web.password';
  const label = secret ? t("New password") : t(fieldDefinition(group, key, valueAt(draft, path) ?? '').label);
  return <li><button type="button" className="save-diff-link" onClick={() => section && requestConfigSection(scope, section.id)}>
    {section ? t(section.label) + ' › ' : ''}{label}</button>
    <span>{secret ? t("Will be changed") : <>{show(displayValue(path, valueAt(config, path)))} → {show(displayValue(path, valueAt(draft, path)))}</>}</span></li>;
}

/** Pending changes of the open scope: one modem profile, or the shared web access. */
function SaveBar({ scope }: { scope: SaveScope }) {
  usePreferences();
  const { config, draft, password, errors, saving, savingScope, editable, saveConfig, discardConfig, selectedModem } = useSupervisor();
  const entries = useFleet();
  const [details, setDetails] = useState(false);
  const changes = scopedChanges(config, draft, password, scope), count = countChanges(changes);
  const errorCount = Object.keys(scopedErrors(errors, scope)).length;
  const applying = saving && savingScope === scope;
  const unconfigured = scope === 'profile' && entries.find(entry => entry.id === selectedModem)?.configured === false;
  const selectedEntry = entries.find(entry => entry.id === selectedModem);
  const modemLabel = selectedEntry ? modemName(selectedEntry) : t("Modem");
  const target = scope === 'web' ? t("the System settings") : scope === 'account' ? t("your account") : modemLabel;
  const detail = errorCount ? t(errorCount === 1 ? "{count} value to correct before saving." : "{count} values to correct before saving.", { count: errorCount })
    : [unconfigured ? t("Saving creates the profile of {modem}.", { modem: modemLabel }) : '', ...saveEffects(changes, config, scope).map(effect => t(effect))].filter(Boolean).join(' ');
  const paths = Object.entries(changes).flatMap(([section, values]) => Object.keys(values).map(key => section + '.' + key));
  return <section id="save-bar" className="save-bar" aria-label={t("Pending changes")}>
    <div className="save-bar-main">
      <div className="save-status">
        <strong id="draft-summary">{t(count === 1 ? "{count} change in {target}" : "{count} changes in {target}", { count, target })}</strong>
        <span id="draft-detail">{detail}</span>
        <button type="button" className="save-diff-toggle" aria-expanded={details} aria-controls="save-diff"
          onClick={() => setDetails(value => !value)}>{t(details ? "Hide changes" : "View changes")}</button>
      </div>
      <div className="button-row">
        <button id="global-discard" type="button" className="button button-ghost" disabled={saving} onClick={() => discardConfig(scope)}>{t("Discard")}</button>
        <button id="global-save" type="button" className="button" disabled={!editable || !count || saving || errorCount > 0} onClick={() => void saveConfig(scope)}>
          {applying ? <span className="spinner" /> : <Icon name="check" size={16} />}{applying ? t("Applying…") : t("Save and apply")}</button>
      </div>
    </div>
    {details && <ul id="save-diff" className="save-diff">{paths.map(path => <ChangeItem key={path} path={path} scope={scope} />)}</ul>}
  </section>;
}

export function AppShell({ children }: { children: ReactNode }) {
  usePreferences();
  const pathname = usePathname(), path = normalized(pathname);
  const router = useRouter();
  const { auth, connected, lastUpdated, config, draft, password, dirtyCount, saving, savingScope, errors, selectedModem, webDirtyCount } = useSupervisor();
  const entries = useFleet(), fleet = entries.length >= 2;
  const kind = pageKind(path, fleet);
  const scope: SaveScope | null = kind === 'system' ? 'web' : kind === 'account' ? 'account' : kind === 'modem' && modemTab(path) === 'configuration' ? 'profile' : null;
  const [menuOpen, setMenuOpen] = useState(false);
  const [mobileLayout, setMobileLayout] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const restoreMenuFocus = useRef(false);
  const active = auth === 'authenticated';
  const drawerOpen = active && mobileLayout && menuOpen;
  const scopeCount = scope ? countChanges(scopedChanges(config, draft, password, scope)) : 0;
  const showSaveBar = active && Boolean(scope) && (scopeCount > 0 || (saving && savingScope === scope));
  const webErrors = Object.keys(scopedErrors(errors, 'web')).length;
  const configuredPoll = Number(config?.web?.poll_interval);
  const pollInterval = Number.isFinite(configuredPoll) ? Math.round(Math.min(30, Math.max(1, configuredPoll)) * 10) / 10 : 2;

  useEffect(() => {
    const migrateHash = () => {
      const raw = window.location.hash.replace(/^#\/?/, '').split('/');
      if (!['dashboard', 'metrics', 'configuration'].includes(raw[0])) return;
      const [section, sub] = raw;
      const allowed = section === 'metrics' ? metricTabs : configTabs;
      const legacyChart = section === 'metrics' && ['signal', 'performance'].includes(sub);
      const next = section === 'dashboard' ? '/dashboard/' : legacyChart ? '/metrics/charts/?view=' + sub : '/' + section + '/' + (allowed.includes(sub) ? sub : allowed[0]) + '/';
      router.replace(next, { scroll: false });
    };
    migrateHash(); window.addEventListener('hashchange', migrateHash);
    return () => window.removeEventListener('hashchange', migrateHash);
  }, [router]);
  const closeMenu = useCallback((restoreFocus = true) => {
    restoreMenuFocus.current = restoreFocus;
    setMenuOpen(false);
  }, []);
  function navigateMenu(event: MouseEvent<HTMLAnchorElement>) {
    const destination = new URL(event.currentTarget.href);
    const samePage = destination.pathname === window.location.pathname && destination.search === window.location.search;
    closeMenu(samePage || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
  }
  useEffect(() => { closeMenu(false); }, [pathname, closeMenu]);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 1023px)');
    const syncLayout = () => {
      setMobileLayout(media.matches);
      if (!media.matches) closeMenu(false);
    };
    syncLayout();
    media.addEventListener('change', syncLayout);
    return () => media.removeEventListener('change', syncLayout);
  }, [closeMenu]);
  useEffect(() => {
    if (!drawerOpen) return;
    const sidebar = sidebarRef.current, opener = menuButtonRef.current;
    if (!sidebar) return;
    const focusable = () => Array.from(sidebar.querySelectorAll<HTMLElement>(
      'a[href], button, input, select, textarea, [tabindex]')).filter(element =>
      element.tabIndex >= 0 && !element.matches(':disabled') && element.getClientRects().length > 0
      && window.getComputedStyle(element).visibility !== 'hidden');
    sidebar.scrollTop = 0;
    focusable()[0]?.focus({ preventScroll: true });
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeMenu();
        return;
      }
      if (event.key !== 'Tab') return;
      const elements = focusable(), first = elements[0], last = elements.at(-1);
      if (!first || !last) { event.preventDefault(); return; }
      if (!sidebar.contains(document.activeElement) || event.shiftKey && document.activeElement === first
          || !event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    };
    document.addEventListener('keydown', keydown, true);
    return () => {
      document.removeEventListener('keydown', keydown, true);
      if (restoreMenuFocus.current && opener?.isConnected && window.matchMedia('(max-width: 1023px)').matches) {
        opener.focus({ preventScroll: true });
      }
      restoreMenuFocus.current = false;
    };
  }, [drawerOpen, closeMenu]);
  useEffect(() => {
    const block = (event: BeforeUnloadEvent) => { if (dirtyCount) event.preventDefault(); };
    window.addEventListener('beforeunload', block);
    return () => window.removeEventListener('beforeunload', block);
  }, [dirtyCount]);

  if (!active) return <div className="auth-layout">
    <div className="login-preferences"><PreferenceControls /></div><main className="auth-main" id="main-content"><LoginScreen /></main>
    <ToastRegion />
  </div>;

  const selectedEntry = entries.find(entry => entry.id === selectedModem);
  const crumb = kind === 'fleet' ? t("Fleet") : kind === 'system' ? t("System") : kind === 'account' ? t("Profile")
    : selectedEntry ? modemName(selectedEntry) : t("Modem");
  return <PageActionsProvider><div className="app-layout">
    <a href="#main-content" className="skip-link">{t("Skip to content")}</a>
    {drawerOpen && <button className="mobile-backdrop" aria-label={t("Close navigation")} tabIndex={-1} onClick={() => closeMenu()} />}
    <aside id="main-sidebar" ref={sidebarRef} className={'sidebar ' + (drawerOpen ? 'is-open' : '')}
      role={drawerOpen ? 'dialog' : undefined} aria-modal={drawerOpen ? true : undefined} aria-label={drawerOpen ? t("Main navigation") : undefined}>
      {drawerOpen && <button type="button" className="icon-button sidebar-close" aria-label={t("Close navigation")} title={t("Close navigation")} onClick={() => closeMenu()}><Icon name="close" size={20} /></button>}
      <Link href={fleet ? '/dashboard/' : '/'} className="brand" prefetch={false} onClick={navigateMenu}><span className="brand-symbol"><Icon name="radio" size={20} /></span><span className="brand-copy"><strong>QMI<span className="brand-light"> Supervisor</span></strong></span></Link>
      <nav id="main-nav" aria-label={t("Main navigation")}>
        {fleet && <Link prefetch={false} href="/dashboard/" onClick={navigateMenu} className={'nav-link' + (kind === 'fleet' ? ' active' : '')}
          aria-current={kind === 'fleet' ? 'page' : undefined}><Icon name="dashboard" size={19} /><span>{t("Fleet")}</span></Link>}
        <Link prefetch={false} href="/configuration/web/" onClick={navigateMenu} className={'nav-link' + (kind === 'system' ? ' active' : '')}
          aria-current={kind === 'system' ? 'page' : undefined}><Icon name="settings" size={19} /><span>{t("System")}</span>
          {webErrors > 0 ? <ErrorCount count={webErrors} /> : webDirtyCount > 0 && <PendingDot />}</Link>
      </nav>
      <ModemList onNavigate={navigateMenu} />
      <AccountMenu />
    </aside>
    <div className="main-workspace" inert={drawerOpen}>
      <header className="topbar">
        <div className="header-breadcrumb"><button ref={menuButtonRef} className="icon-button mobile-menu" aria-label={t("Open navigation")} aria-controls="main-sidebar" aria-expanded={drawerOpen} onClick={() => setMenuOpen(value => !value)}><Icon name="menu" /></button>
          {kind === 'modem' && fleet ? <Link href="/dashboard/" prefetch={false} className="breadcrumb-parent">{t("Fleet")}</Link> : <span className="breadcrumb-parent">QMI Supervisor</span>}
          <Icon name="chevron-right" size={14} /><strong title={crumb}>{crumb}</strong></div>
      </header>
      <main id="main-content" className={'main-content page-' + kind}>
        {kind === 'fleet' ? <FleetHeading /> : kind === 'system' ? <SystemHeading /> : kind === 'account' ? <ProfileHeading /> : <ModemHeader />}
        <div id="view" className="view-content">{children}</div>
        {showSaveBar && scope && <SaveBar scope={scope} />}
        <footer className="page-footer"><span><span className={'status-dot ' + (connected ? 'status-good' : 'status-warning')} />{lastUpdated ? t("Last update: ") + new Date(lastUpdated).toLocaleTimeString(intlLocale()) : t("Waiting for the first reading")}</span><span id="poll-status">{t("Updates every {seconds} s", { seconds: pollInterval })}</span></footer>
      </main>
    </div>
    <ToastRegion />
  </div></PageActionsProvider>;
}
