"""Networks a profile reaches through its modem, beside the optional default route."""
import ipaddress

MAX_ROUTES = 8


def parse_routes(value):
    """Comma-separated IPv4 networks in CIDR form, such as the other UEs of a private core; empty means none."""
    if not isinstance(value, str):
        raise ValueError('network.routes must be a comma-separated list of IPv4 networks')
    parts = [part.strip() for part in value.split(',')] if value.strip() else []
    if len(parts) > MAX_ROUTES:
        raise ValueError(f'network.routes accepts at most {MAX_ROUTES} networks')
    networks = []
    for part in parts:
        try:
            if '/' not in part:
                raise ValueError(part)
            # Strict: 192.168.4.10/24 is refused, so the route is exactly what was written.
            network = ipaddress.IPv4Network(part)
        except ValueError:
            raise ValueError('network.routes needs IPv4 networks with their prefix length, such as '
                             f'192.168.4.0/24: {part or "(empty item)"}') from None
        address = network.network_address
        if network.prefixlen == 0:
            raise ValueError('network.routes cannot hold 0.0.0.0/0; network.default_route sets the default route')
        if address.is_loopback or address.is_multicast or int(address) < (1 << 24) or address == ipaddress.IPv4Address('255.255.255.255'):
            raise ValueError(f'network.routes cannot route {network}: no loopback, multicast, 0.0.0.0/8 or broadcast networks')
        if str(network) in networks:
            raise ValueError(f'network.routes lists {network} twice')
        networks.append(str(network))
    return tuple(networks)
