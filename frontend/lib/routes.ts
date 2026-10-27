/** Networks a modem profile reaches through its modem; the same rules as modules/network/routes.py. */
export const MAX_ROUTE_NETWORKS = 8;

/** Why one network is rejected, or null when it can be routed. */
export function routeNetworkError(network: string): string | null {
  const match = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(network.trim());
  if (!match) return "Use a network with its prefix, such as 192.168.4.0/24.";
  const octets = match.slice(1, 5).map(Number), prefix = Number(match[5]);
  if (octets.some((octet, index) => octet > 255 || String(octet) !== match[index + 1])) return "Not a valid IPv4 address.";
  if (prefix < 1 || prefix > 32) return "The prefix length must be between 1 and 32.";
  const address = octets.reduce((value, octet) => value * 256 + octet, 0);
  const host = prefix === 32 ? 0 : address % 2 ** (32 - prefix);
  if (host !== 0) return "Use the network address, with the host bits at zero.";
  if (octets[0] === 127) return "Loopback is not allowed.";
  if (octets[0] === 0) return "Addresses in 0.0.0.0/8 are not allowed.";
  if (octets[0] >= 224 && octets[0] <= 239) return "Multicast is not allowed.";
  if (address === 2 ** 32 - 1) return "Broadcast is not allowed.";
  return null;
}

/** Per-network reasons, including duplicates, in the order they were listed. */
export function routeNetworkErrors(networks: readonly string[]): (string | null)[] {
  const seen = new Set<string>();
  return networks.map(network => {
    const error = routeNetworkError(network);
    if (error) return error;
    if (seen.has(network.trim())) return "Duplicate network.";
    seen.add(network.trim());
    return null;
  });
}

/** The networks of a valid setting (empty means none), or null when the backend would refuse it. */
export function parseRouteNetworks(value: unknown): readonly string[] | null {
  if (typeof value !== 'string' || /[\r\n\0]/.test(value)) return null;
  if (!value.trim()) return [];
  const parts = value.split(',');
  if (parts.length > MAX_ROUTE_NETWORKS || routeNetworkErrors(parts).some(Boolean)) return null;
  return parts.map(part => part.trim());
}
