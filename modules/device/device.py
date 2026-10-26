"""Discover QMI USB functions from sysfs, without parsing lsusb output."""
from dataclasses import dataclass
from pathlib import Path


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    path: str
    interface: str
    usb_path: str
    serial: str
    product: str
    enumeration: str = ''
    manufacturer: str = ''

    @property
    def identity(self):
        return self.serial or self.usb_path


def scan_devices(*, sys_root="/sys", dev_root="/dev"):
    """List usable QMI USB functions without selecting or reserving a modem.

    The result is a fresh snapshot, so callers can scan again after a hotplug.
    MBIM functions and control ports without a network interface are excluded.
    """
    root = Path(sys_root)
    candidates = []
    for entry in sorted((root / "class/usbmisc").glob("cdc-wdm*")):
        path = Path(dev_root) / entry.name
        if not path.exists():
            continue
        function = (entry / "device").resolve()
        if (function / "driver").resolve().name != "qmi_wwan":
            continue
        interfaces = sorted((function / "net").glob("*"))
        if len(interfaces) != 1:
            continue
        usb = next((p for p in function.parents if (p / "idVendor").exists()), None)
        if usb is None:
            continue
        def read(name):
            try:
                return (usb / name).read_text().strip()
            except OSError:
                return ""
        found_serial = read("serial")
        candidates.append(Device(str(path), interfaces[0].name, str(usb), found_serial, read("product"),
                                 read('devnum'), read("manufacturer")))
    return candidates


def discover(device="auto", serial="", *, sys_root="/sys", dev_root="/dev"):
    """Require exactly one QMI function matching the configured selectors."""
    explicit = Path(device).resolve() if device != "auto" else None
    candidates = [
        candidate for candidate in scan_devices(sys_root=sys_root, dev_root=dev_root)
        if (explicit is None or Path(candidate.path).resolve() == explicit)
        and (not serial or candidate.serial == serial)
    ]
    if len(candidates) > 1:
        raise DiscoveryError("Multiple QMI modems found; set modem.serial or --device to select one")
    return candidates[0] if candidates else None
