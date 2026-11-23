/** Version and updates (plans/updates/U4-web-ui.md): pure data, labels and reconnect rules.
 *
 * No React and no I/O, so `node --experimental-strip-types` can load it directly.
 * The JSON shapes mirror the `/api/update` GET body of step 4 exactly; U2/U3
 * spellings are normalised by `modules/web/updates.py`, never here.
 */

export type Message = { id: string; values?: Record<string, string | number> };

export type UpdateStepState = 'pending' | 'running' | 'done' | 'failed' | 'skipped';
export type UpdateStep = { id: string; state: UpdateStepState };
export type UpdateRelation = 'installed' | 'newer' | 'older' | 'unknown';
export type UpdateAction = 'install' | 'automatic' | 'rollback' | 'repair';
export type UpdateState = 'running' | 'succeeded' | 'failed' | 'rolled_back' | 'interrupted';

export type UpdateRelease = {
  version: string;
  tag: string | null;
  published_at_utc: string | null;
  prerelease: boolean;
  notes: string;
  notes_truncated: boolean;
  relation: UpdateRelation;
  installable: boolean;
  reason: string | null;
};

export type UpdateInstall = {
  action: UpdateAction;
  state: UpdateState;
  version: string | null;
  from_version: string | null;
  step: string | null;
  steps: UpdateStep[];
  started_at_utc: string | null;
  finished_at_utc: string | null;
  error_code: string | null;
  error: string | null;
  running?: boolean;
};

export type UpdateHistoryEntry = {
  action: UpdateAction;
  version: string | null;
  from_version: string | null;
  result: UpdateState;
  started_at_utc: string | null;
  finished_at_utc: string | null;
  error_code: string | null;
  error: string | null;
};

export type UpdateSettings = {
  mode: 'off' | 'notify' | 'auto' | string;
  channel: 'stable' | 'prerelease' | string;
  window_start: string;
  window_end: string;
  check_interval_hours: number;
  require_signature: boolean;
  auto_rollback: boolean;
  allow_downgrade: boolean;
  source: string;
};

export type UpdateCheck = {
  state: 'never' | 'ok' | 'failed';
  checked_at_utc: string | null;
  error_code: string | null;
  error: string | null;
  running: boolean;
  retry_after: number;
};

export type UpdateResponse = {
  installed: { version: string; commit: string | null; built_utc: string | null };
  settings: UpdateSettings;
  check: UpdateCheck;
  releases: UpdateRelease[];
  install: UpdateInstall | null;
  history: UpdateHistoryEntry[];
  rollback: { available: boolean; version: string | null; running: boolean };
  state_error: string | null;
  units_error: string | null;
};

/** The request an open tab remembers across the restart and the reload. */
export type UpdatePending = {
  action: 'install' | 'rollback';
  target: string;
  from: string | null;
  /** `install.started_at_utc` when the request was accepted, so an older result is ignored. */
  baseline: string | null;
  started_ms: number;
};

export type ReleaseActionKind = 'update' | 'downgrade' | 'installed' | 'none';
export type ReleaseAction = { kind: ReleaseActionKind; reason: Message | null };

export type ReconnectResult =
  | { kind: 'none' }
  | { kind: 'reload' }
  | { kind: 'reload-prompt'; version: string }
  | { kind: 'expired' }
  | { kind: 'wait' }
  | { kind: 'succeeded'; action: 'install' | 'rollback'; version: string }
  | { kind: 'failed'; action: 'install' | 'rollback'; version: string;
      previous: string | null; code: string | null; rolledBack: boolean };

const FINAL_STATES: UpdateState[] = ['succeeded', 'failed', 'rolled_back', 'interrupted'];

/** The first release newer than the installed version that may be installed. */
export function availableUpdate(data: UpdateResponse | null | undefined): UpdateRelease | null {
  if (!data) return null;
  return data.releases.find(release => release.relation === 'newer' && release.installable) ?? null;
}

/**
 * What the action cell of one release may do: update, an advanced downgrade,
 * an installed version or nothing (with the reason, already chosen at runtime).
 */
export function releaseAction(release: UpdateRelease, options: { advanced: boolean; allowDowngrade: boolean }): ReleaseAction {
  if (release.relation === 'installed') return { kind: 'installed', reason: null };
  if (!release.installable) return { kind: 'none', reason: reasonText(release.reason) };
  if (release.relation === 'newer') return { kind: 'update', reason: null };
  if (release.relation === 'older') {
    return { kind: options.advanced && options.allowDowngrade ? 'downgrade' : 'none', reason: null };
  }
  return { kind: 'none', reason: release.reason ? reasonText(release.reason) : null };
}

/** Installed → Installed, newer → Newer, older → Older, unknown → Unknown. */
export const RELATION_TEXT: Record<string, string> = {
  installed: "Installed",
  newer: "Newer",
  older: "Older",
  unknown: "Unknown",
};

/** Why a release cannot be installed (the adapter's `reason` codes). */
export const REASON_TEXT: Record<string, string> = {
  unsigned: "Not signed",
  no_package: "No package for this device",
  no_checksums: "No checksum file",
  prerelease_channel: "Pre-releases are not enabled",
  downgrade_disabled: "Older versions are not allowed",
  installed_unknown: "The installed version is unknown",
  installed: "Already installed",
  // U2's release reasons (plans/updates/U2-release-discovery.md).
  bad_tag: "The release tag is not a version number",
  several_packages: "The release has more than one qmi-supervisor package",
  bad_package_name: "The package file name does not contain a package version",
  version_mismatch: "The package version does not match the release tag",
  asset_not_ready: "A release file is empty or still uploading",
  bad_url: "A release file has an address that is not allowed",
  package_too_large: "The package is larger than 32 MiB",
};

/** The updater's transaction steps; U3 adds resolve, backup and snapshot. */
export const STEP_TEXT: Record<string, string> = {
  resolve: "Check the request",
  download: "Download the package",
  verify: "Verify checksum and signature",
  prepare: "Keep the current package for rollback",
  backup: "Save the configuration",
  snapshot: "Record the running state",
  install: "Install the package",
  restart: "Restart the supervisor",
  health: "Check the new version",
  rollback: "Restore the previous version",
};

export const STEP_STATE_TEXT: Record<string, string> = {
  done: "Done",
  running: "In progress",
  pending: "Pending",
  failed: "Error",
  skipped: "Skipped",
};

export const HISTORY_ACTION_TEXT: Record<string, string> = {
  install: "Manual update",
  automatic: "Automatic update",
  rollback: "Rollback",
  repair: "Repair",
};

export const HISTORY_RESULT_TEXT: Record<string, string> = {
  succeeded: "Succeeded",
  failed: "Failed",
  rolled_back: "Rolled back",
  interrupted: "Interrupted",
};

/** Sentences for a failed check (U2) or a failed transaction (U3), by code. */
export const ERROR_TEXT: Record<string, string> = {
  // Check failures, U2.
  network: "The release server could not be reached.",
  tls: "The secure connection to the release server failed (certificates or clock).",
  unauthorized: "The access token was rejected.",
  forbidden: "The release server refused the request.",
  rate_limited: "The release server limited the requests; the next check retries.",
  not_found: "The release list was not found; check update.source or set a token for a private repository.",
  http: "The release server answered with an error.",
  http_error: "The release server answered with an error.",
  too_large: "The release list is larger than 2 MiB.",
  bad_response: "The list of releases could not be read.",
  invalid_response: "The list of releases could not be read.",
  bad_source: "update.source is not an allowed address.",
  bad_redirect: "The release server redirected to an address that is not allowed.",
  bad_token_file: "The token file is not private or not valid; set it again with qmi-supervisor-update set-token.",
  // Install failures.
  no_package: "The release has no package.",
  no_checksums: "The release has no checksum file.",
  unsigned: "The release is not signed.",
  bad_signature: "The release signature is not valid.",
  checksum_mismatch: "The downloaded package does not match its checksum.",
  download_failed: "The package could not be downloaded.",
  install_failed: "The package could not be installed.",
  health_failed: "The new version did not start correctly in time.",
  modems_not_back: "A modem that was online did not come back online in time.",
  rollback_failed: "The previous version could not be restored.",
  locked: "Another update operation was running.",
  no_rollback: "No previous version is kept.",
  interrupted: "The updater stopped before finishing.",
  // U3's history codes (plans/updates/U3-verified-install-and-rollback.md).
  config_unreadable: "The current configuration cannot be read; fix it first.",
  invalid_version: "The requested version is not valid.",
  not_package_install: "This installation was not made with the Debian package.",
  package_state_broken: "The installed package is not completely installed.",
  already_installed: "This version is already installed.",
  unknown_version: "This version is not in the list of releases. Check again.",
  prerelease_channel: "Pre-releases are not enabled",
  downgrade_disabled: "Installing older versions is turned off.",
  no_cached_package: "The package of that version is no longer stored on this device.",
  cache_unsafe: "The download folder is not private.",
  download_too_large: "The download is larger than allowed.",
  download_size_mismatch: "The downloaded file does not have the published size.",
  keyring_missing: "The release signing key is not installed.",
  no_keyring: "This device has no release signing key, so it cannot verify releases. Install a package that includes /opt/qmi-supervisor/keys/release-signing.gpg.",
  sums_invalid: "The checksum file is not valid.",
  checksum_missing: "The checksum file does not list this package.",
  package_mismatch: "The downloaded package is not the expected version.",
  package_incomplete: "That version does not include the updater and cannot be installed from here.",
  config_incompatible: "The current configuration is not valid for that version.",
  backup_failed: "The configuration could not be saved.",
  dpkg_busy: "Another package installation is running; try again later.",
  install_timeout: "The package installation took too long.",
  repair_failed: "The package could not be repaired automatically.",
  internal_error: "Unexpected updater error.",
  in_progress: "An update is already running.",
  request_pending: "Another update request is starting.",
  unit_start_failed: "The updater is not available on this device.",
  downgrade_not_confirmed: "Confirm the installation of an older version.",
};

/** A reason as a message and its values; an unknown code keeps its raw code in the sentence. */
export function reasonText(code: string | null | undefined): Message {
  const known = code ? REASON_TEXT[code] : undefined;
  return known ? { id: known } : { id: "Unknown reason ({reason})", values: { reason: code ?? '' } };
}

/** The sentence of a failure code; unknown codes fall back to the generic updater error. */
export function updateErrorText(code: string | null | undefined): string {
  return (code && ERROR_TEXT[code]) || "The updater reported an error ({code}).";
}

/** A step's label; an unknown step keeps the raw identifier, shown in monospace. */
export function stepText(id: string): string {
  return STEP_TEXT[id] ?? id;
}

/** Keys of the failures the user has not seen yet. */
export function failureKeys(data: UpdateResponse | null | undefined): string[] {
  if (!data) return [];
  const keys: string[] = [];
  if (data.check.state === 'failed') keys.push('check:' + (data.check.checked_at_utc ?? ''));
  const last = data.history[0];
  if (last && (last.result === 'failed' || last.result === 'rolled_back')) keys.push('history:' + (last.finished_at_utc ?? ''));
  return keys;
}

/** The tone of the System dot: a new failure is bad, an available or running update is info. */
export function indicatorTone(data: UpdateResponse | null | undefined, seen: string[]): 'bad' | 'info' | null {
  if (!data) return null;
  if (failureKeys(data).some(key => !seen.includes(key))) return 'bad';
  if (availableUpdate(data) || data.install?.running) return 'info';
  return null;
}

/**
 * What the page does after a GET, in order: a reload prompt when the page build
 * differs from the installed version (no request pending), an expired request,
 * a reload once the installed version changed, wait while the transaction runs,
 * then success or failure of the request this tab started.
 */
export function reconnectAction(input: {
  pending: UpdatePending | null;
  installed: string;
  pageVersion: string | null;
  install: UpdateInstall | null;
  unsaved: boolean;
  now: number;
}): ReconnectResult {
  const { pending, installed, pageVersion, install, unsaved, now } = input;
  if (!pending) {
    return pageVersion !== null && installed !== pageVersion
      ? { kind: 'reload-prompt', version: installed }
      : { kind: 'none' };
  }
  if (now - pending.started_ms > 30 * 60 * 1000 && !install?.running) return { kind: 'expired' };
  if (installed !== pageVersion) {
    return unsaved ? { kind: 'reload-prompt', version: installed } : { kind: 'reload' };
  }
  if (install?.running) return { kind: 'wait' };
  if (install && install.version === pending.target
      && install.started_at_utc !== pending.baseline && FINAL_STATES.includes(install.state)) {
    if (install.state === 'succeeded') return { kind: 'succeeded', action: pending.action, version: pending.target };
    return {
      kind: 'failed', action: pending.action, version: pending.target,
      previous: install.from_version,
      code: install.state === 'interrupted' ? 'interrupted' : install.error_code,
      rolledBack: install.state === 'rolled_back',
    };
  }
  if (now - pending.started_ms > 2 * 60 * 1000) {
    return { kind: 'failed', action: pending.action, version: pending.target, previous: null, code: 'interrupted', rolledBack: false };
  }
  return { kind: 'wait' };
}
