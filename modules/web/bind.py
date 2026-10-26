"""Resolve default-route and management-LAN IPv4 without traffic, commands or QMI."""
import fcntl
import ipaddress
from pathlib import Path
import re
import socket
import struct


_CELLULAR_PREFIXES = ('ww', 'rmnet', 'ppp', 'mhi', 'ccmni', 'pdp', 'qmimux', 'usb')
_CELLULAR_DRIVERS = {'qmi_wwan', 'cdc_mbim', 'mhi_net', 'mhi_wwan_mbim', 'iosm', 't7xx'}
_VIRTUAL_PREFIXES = ('docker', 'veth', 'virbr', 'lxc', 'cni', 'flannel', 'podman',
                     'tun', 'tap', 'wg', 'tailscale', 'zt', 'dummy')


def interface_ipv4(interface):
    request = struct.pack('256s', interface.encode('utf-8')[:15])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        result = fcntl.ioctl(connection.fileno(), 0x8915, request)  # SIOCGIFADDR
    return socket.inet_ntoa(result[20:24])


def default_interface_address(route_path='/proc/net/route', address_for=interface_ipv4):
    """Return (interface, address) for the lowest metric usable default route."""
    try:
        lines = Path(route_path).read_text().splitlines()
    except OSError:
        return None
    routes = []
    for order, line in enumerate(lines[1:]):
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            flags, metric = int(fields[3], 16), int(fields[6])
            default = int(fields[1], 16) == 0 and int(fields[7], 16) == 0
        except ValueError:
            continue
        if default and flags & 1 and not flags & 0x200:  # UP, not REJECT
            routes.append((metric, order, fields[0]))
    for _, _, interface in sorted(routes):
        try:
            address = str(ipaddress.IPv4Address(address_for(interface)))
        except (OSError, ValueError):
            continue
        parsed = ipaddress.ip_address(address)
        if not (parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast):
            return interface, address
    return None


def management_interface_eligible(interface, sys_root='/sys', _seen=None):
    """Recognize a physical management link or a bridge backed by one.

    A cellular interface is excluded by both name and driver: renaming a QMI
    interface must not turn it into a web listener. Container/tunnel devices
    never become management links just because they have an IPv4 address.
    """
    name = interface.lower()
    if (name == 'lo' or name.startswith(_CELLULAR_PREFIXES + _VIRTUAL_PREFIXES)
            or re.fullmatch(r'br-[0-9a-f]{12,}', name)):
        return False
    root = Path(sys_root) / 'class' / 'net'
    link = root / interface
    try:
        driver = (link / 'device' / 'driver').resolve().name
    except (OSError, RuntimeError):
        return False
    if driver in _CELLULAR_DRIVERS:
        return False
    seen = set() if _seen is None else set(_seen)
    if interface in seen:
        return False
    seen.add(interface)
    if (link / 'bridge').is_dir() or (link / 'brif').is_dir():
        try:
            members = [member.name for member in (link / 'brif').iterdir()]
        except OSError:
            return False
        # A bridge containing a modem is not a management LAN. A physical LAN
        # bridge may also contain veth/VM ports, which alone are insufficient.
        for member in members:
            member_path = root / member
            try:
                member_driver = (member_path / 'device' / 'driver').resolve().name
            except (OSError, RuntimeError):
                return False
            if member.lower().startswith(_CELLULAR_PREFIXES) or member_driver in _CELLULAR_DRIVERS:
                return False
        return any(management_interface_eligible(member, sys_root, seen) for member in members)
    return (link / 'device').exists()


def _management_link_up(interface, sys_root):
    link = Path(sys_root) / 'class' / 'net' / interface
    try:
        if (link / 'operstate').exists() and (link / 'operstate').read_text().strip() not in {'up', 'unknown'}:
            return False
        if (link / 'flags').exists() and not int((link / 'flags').read_text().strip(), 0) & 1:
            return False
        if (link / 'carrier').exists() and (link / 'carrier').read_text().strip() == '0':
            return False
    except (OSError, ValueError):
        return False
    return True


def management_interface_address(route_path='/proc/net/route', address_for=interface_ipv4, *,
                                 sys_root='/sys', interface_eligible=None):
    """Select a management LAN independently of the cellular default route.

    Prefer usable non-cellular default routes, then physical/bridged LANs with
    IPv4 but no gateway. With only cellular/container links, keep localhost.
    Classification and sysfs are injectable; no traffic or commands are used.
    """
    eligible = interface_eligible or (lambda name: management_interface_eligible(name, sys_root))
    defaults = []
    try:
        lines = Path(route_path).read_text().splitlines()
    except OSError:
        lines = []
    for order, line in enumerate(lines[1:]):
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            flags, metric = int(fields[3], 16), int(fields[6])
            default = int(fields[1], 16) == 0 and int(fields[7], 16) == 0
        except ValueError:
            continue
        if default and flags & 1 and not flags & 0x200:
            defaults.append((metric, order, fields[0]))
    try:
        interfaces = sorted(path.name for path in (Path(sys_root) / 'class' / 'net').iterdir())
    except OSError:
        interfaces = []
    candidates = list(dict.fromkeys([name for _, _, name in sorted(defaults)] + interfaces))
    for interface in candidates:
        try:
            if not eligible(interface) or not _management_link_up(interface, sys_root):
                continue
            address = ipaddress.IPv4Address(address_for(interface))
            if address.is_loopback or address.is_unspecified or address.is_multicast:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        return interface, str(address)
    return None
