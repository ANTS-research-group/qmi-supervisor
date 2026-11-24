/** Build-time package version and the published-version comparison. */

/** Filled by next.config.ts from QMI_SUPERVISOR_VERSION; "dev" outside a package build. */
export const BUILD_VERSION: string = process.env.NEXT_PUBLIC_BUILD_VERSION || 'dev';

/** The muted line under the sidebar account: always `v` plus the build version. */
export function accountVersionLabel(version: string = BUILD_VERSION): string {
  return `v${version}`;
}

/**
 * The published version when the running supervisor differs from this page's
 * build, or null when there is nothing to reload. The caller shows
 * "Reload the page to load version {returned value}." A development build
 * ("dev") and an unknown installed version are not mismatches: a reload cannot
 * fix either.
 */
export function publishedVersionMismatch(
  buildVersion: string | null | undefined,
  publishedVersion: string | null | undefined,
): string | null {
  const build = (buildVersion || '').trim(), published = (publishedVersion || '').trim();
  if (!build || !published || build === 'dev' || published === 'unknown') return null;
  return build === published ? null : published;
}
