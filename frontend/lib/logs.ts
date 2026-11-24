/** System log requests: pure helpers, no React and no I/O. */

/**
 * The authenticated API path for the journal, optionally filtered by one modem or by one plugin.
 * The API accepts only one of the two filters; a plugin wins because it is the narrower choice.
 */
export function logsQuery(lines: number, modem?: string | null, plugin?: string | null): string {
  const count = Math.max(1, Math.min(500, Math.trunc(lines)));
  const base = `/api/logs?lines=${count}`;
  if (plugin) return base + '&plugin=' + encodeURIComponent(plugin);
  return modem ? base + '&modem=' + encodeURIComponent(modem) : base;
}

/** The modem selected in the page URL, or an empty string for all modems. */
export function modemFromSearch(search: string): string {
  return new URLSearchParams(search).get('modem') ?? '';
}

/** The plugin selected in the page URL (System → Plugins links here), or an empty string. */
export function pluginFromSearch(search: string): string {
  const value = new URLSearchParams(search).get('plugin') ?? '';
  return /^[a-z0-9-]{1,32}$/.test(value) ? value : '';
}
