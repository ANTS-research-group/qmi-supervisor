import { t } from './messages.ts';
import type { ConnectivityStatus } from './types';

export const MAX_CONNECTIVITY_TARGETS = 4;

/** Targets as listed by the operator; empty items are ignored for editing. */
export function splitTargets(value: unknown): string[] {
  return typeof value === 'string' ? value.split(',').map(part => part.trim()).filter(Boolean) : [];
}

/** Why one IPv4:port target is rejected, or null when the target is valid. */
export function connectivityTargetError(target: string): string | null {
  const match = /^(\d+\.\d+\.\d+\.\d+):([0-9]+)$/.exec(target.trim());
  if (!match) return "Use a numeric IPv4:port target.";
  const host = match[1], octets = host.split('.').map(Number), port = Number(match[2]);
  if (octets.some((octet, index) => octet > 255 || String(octet) !== host.split('.')[index])) return "Not a valid IPv4 address.";
  if (octets[0] === 127) return "Loopback is not allowed.";
  if (octets[0] === 0) return "Addresses in 0.0.0.0/8 are not allowed.";
  if (octets[0] >= 224 && octets[0] <= 239) return "Multicast is not allowed.";
  if (host === '255.255.255.255') return "Broadcast is not allowed.";
  if (!Number.isInteger(port) || port < 1 || port > 65535) return "The port must be between 1 and 65535.";
  return null;
}

const targetIdentity = (target: string) => {
  const [host, port] = target.trim().split(':');
  return host + ':' + Number(port);
};

/** Per-target reasons, including duplicates, in the order they were listed. */
export function connectivityTargetErrors(targets: readonly string[]): (string | null)[] {
  const seen = new Set<string>();
  return targets.map(target => {
    const error = connectivityTargetError(target);
    if (error) return error;
    const identity = targetIdentity(target);
    if (seen.has(identity)) return "Duplicate target.";
    seen.add(identity);
    return null;
  });
}

/** Numeric targets avoid relying on whichever uplink currently supplies DNS. */
export function parseConnectivityTargets(value: unknown): readonly [string, number][] | null {
  if (typeof value !== 'string' || /[\r\n\0]/.test(value)) return null;
  const parts = value.split(',');
  if (parts.length < 1 || parts.length > MAX_CONNECTIVITY_TARGETS) return null;
  if (connectivityTargetErrors(parts).some(Boolean)) return null;
  return parts.map(part => {
    const [host, port] = part.trim().split(':');
    return [host, Number(port)] as [string, number];
  });
}

/** A local ONLINE session never supplies evidence of TCP reachability. */
export function connectivityPresentation(value: Partial<ConnectivityStatus> | null | undefined) {
  const status = value ?? {};
  const verified = status.state === 'up' && status.stale !== true;
  const label = status.dns_error ? "DNS error" : verified ? "Connectivity verified" : status.stale === true ? "Verification expired"
    : status.state === 'checking' ? "Checking connectivity"
    : status.state === 'down' ? "Verification failed"
    : status.state === 'disabled' ? status.enabled === false ? "Verification disabled" : "No active verification"
    : "Connectivity unverified";
  let route: string | null = null;
  if (status.route_mode === 'blocked') route = "Route blocked";
  else if (status.route_mode === 'disabled') route = "No default route";
  else if (status.route_mode === 'fallback') route = "Backup";
  else if (status.route_mode === 'requested') route = status.preferred === true ? "Primary" : "Not primary";
  return { label: t(label), route: route ? t(route) : null, verified, summary: [label, route].map(value => value ? t(value) : value).filter(Boolean).join(' · ') };
}
