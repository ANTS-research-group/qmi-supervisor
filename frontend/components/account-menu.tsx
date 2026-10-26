'use client';

import { t } from '@/lib/messages';
import type { Locale } from '@/lib/messages';
import { usePreferences } from './preferences';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { Icon } from './icon';
import { useSupervisor } from './supervisor-provider';

/** Account and personal preferences, at the bottom of the main navigation. */
export function AccountMenu() {
  const { theme, locale, setTheme, setLanguage } = usePreferences();
  const { session, editable, logout, loggingOut } = useSupervisor();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const container = useRef<HTMLDivElement>(null), button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!container.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);
  const name = session?.username || t("Username");
  // The menu stays mounted while closed so its controls keep their identity.
  return <div className="account" ref={container} onKeyDown={event => {
    if (event.key === 'Escape' && open) { event.preventDefault(); setOpen(false); button.current?.focus(); }
  }}>
    <div id="account-menu" className="account-menu" hidden={!open}>
      <button type="button" id="theme-toggle" className="account-item" aria-pressed={theme === 'dark'}
        onClick={() => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark')}>
        <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={18} /><span>{t("Dark mode")}</span><span className="account-toggle" aria-hidden="true" />
      </button>
      <label className="account-item account-language" htmlFor="language-select"><Icon name="globe" size={18} /><span>{t("Language")}</span>
        <select id="language-select" className="language-select" value={locale} onChange={event => setLanguage(event.currentTarget.value as Locale)}>
          <option value="en">English</option><option value="es">{t("Spanish")}</option>
        </select></label>
      <Link href="/profile/" prefetch={false} className="account-item"
        onClick={() => setOpen(false)}><Icon name="user" size={18} /><span>{t("Profile")}</span></Link>
      <button type="button" id="session-open" className="account-item account-signout" disabled={busy || loggingOut}
        onClick={async () => { setBusy(true); try { await logout(); } finally { setBusy(false); } }}>
        <Icon name="logout" size={18} /><span>{t("Sign out")}</span></button>
    </div>
    <button ref={button} type="button" id="account-button" className="account-button" aria-expanded={open} aria-controls="account-menu"
      onClick={() => setOpen(value => !value)}>
      <span className="avatar" aria-hidden="true">{name.slice(0, 2).toUpperCase()}</span>
      <span className="account-copy"><strong>{name}</strong><span>{t(editable ? "Full access" : "Read only")}</span></span>
      <Icon name="chevrons-up-down" size={16} />
    </button>
  </div>;
}
