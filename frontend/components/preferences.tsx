'use client';

import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { setLocale as activateLocale, t } from '@/lib/messages';
import type { Locale } from '@/lib/messages';
import { Icon } from './icon';

export type Theme = 'light' | 'dark';
const Context = createContext({ theme: 'light' as Theme, locale: 'en' as Locale, advanced: false,
  setTheme: (_value: Theme) => {}, setLanguage: (_value: Locale) => {}, setAdvanced: (_value: boolean) => {} });
const themeKey = 'qmi-supervisor.theme', languageKey = 'qmi-supervisor.language', advancedKey = 'qmi-supervisor.advanced';

/** Runs before paint; storage access is optional and never contains credentials. */
export const preferenceBootstrap = `(function(){try{var r=document.documentElement,t=localStorage.getItem('${themeKey}'),l=localStorage.getItem('${languageKey}');r.dataset.theme=t==='dark'?'dark':'light';r.lang=l==='es'?'es':'en';}catch(e){}})();`;

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [theme, updateTheme] = useState<Theme>('light');
  const [locale, updateLocale] = useState<Locale>('en');
  const [advanced, updateAdvanced] = useState(false);
  const setTheme = (value: Theme) => {
    document.documentElement.dataset.theme = value;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', value === 'dark' ? '#142431' : '#f7f8fa');
    updateTheme(value);
    try { localStorage.setItem(themeKey, value); } catch { /* Memory-only preferences remain usable. */ }
  };
  const setLanguage = (value: Locale) => {
    activateLocale(value);
    document.documentElement.lang = value;
    updateLocale(value);
    try { localStorage.setItem(languageKey, value); } catch { /* Storage may be disabled. */ }
  };
  /** Advanced options only change what is listed; they never change values. */
  const setAdvanced = (value: boolean) => {
    updateAdvanced(value);
    try { localStorage.setItem(advancedKey, value ? 'true' : 'false'); } catch { /* Storage may be disabled. */ }
  };
  useEffect(() => {
    const restore = () => {
      try {
        setTheme(localStorage.getItem(themeKey) === 'dark' ? 'dark' : 'light');
        setLanguage(localStorage.getItem(languageKey) === 'es' ? 'es' : 'en');
        updateAdvanced(localStorage.getItem(advancedKey) === 'true');
      } catch { setTheme('light'); setLanguage('en'); updateAdvanced(false); }
    };
    restore();
    window.addEventListener('storage', restore);
    return () => window.removeEventListener('storage', restore);
  }, []);
  useEffect(() => { document.title = 'QMI Supervisor'; }, [locale]);
  const value = useMemo(() => ({ theme, locale, advanced, setTheme, setLanguage, setAdvanced }), [theme, locale, advanced]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export const usePreferences = () => useContext(Context);

export function PreferenceControls() {
  const { theme, locale, setTheme, setLanguage } = usePreferences();
  return <div className="preference-controls">
    <button type="button" className="icon-button" id="theme-toggle" aria-pressed={theme === 'dark'}
      aria-label={t('Dark mode')} title={t(theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode')}
      onClick={() => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark')}><Icon name={theme === 'dark' ? 'sun' : 'moon'} size={19} /></button>
    <label className="sr-only" htmlFor="language-select">{t('Language')}</label>
    <select id="language-select" className="language-select" value={locale}
      onChange={event => setLanguage(event.currentTarget.value as Locale)}>
      <option value="en">English</option><option value="es">{t('Spanish')}</option>
    </select>
  </div>;
}
