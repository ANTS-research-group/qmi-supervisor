/** System log requests (M14): pure helpers, no React and no I/O. */

/** The authenticated API path for the journal, optionally filtered by one modem. */
export function logsQuery(lines: number, modem?: string | null): string {
  const count = Math.max(1, Math.min(500, Math.trunc(lines)));
  const base = `/api/logs?lines=${count}`;
  return modem ? base + '&modem=' + encodeURIComponent(modem) : base;
}

/** The modem selected in the page URL, or an empty string for all modems. */
export function modemFromSearch(search: string): string {
  return new URLSearchParams(search).get('modem') ?? '';
}
