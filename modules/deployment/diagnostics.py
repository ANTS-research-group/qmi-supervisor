"""Read-only host diagnostics; importing this module does not load GI or QMI."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from modules.device.device import scan_devices


def diagnose(settings, *, sys_root="/sys", dev_root="/dev", runner=None, which=shutil.which):
    """Inspect dependencies and USB/network ownership without changing the host.

    ``runner`` accepts the same arguments as ``subprocess.run``. Every invoked
    command reads state only; no QMI requests or service changes are performed.
    A modem may be absent during provisioning, so absence is only a warning.
    """
    run = runner or subprocess.run
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    def query(*args):
        try:
            result = run(list(args), capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return None, "", str(error)
        return result.returncode, result.stdout or "", result.stderr or ""

    def reason(stderr, fallback):
        return stderr.strip()[:300] or fallback

    commands = {name: bool(which(name)) for name in ("ip", "systemctl", "udevadm")}
    for name, available in commands.items():
        required = name == "systemctl" or (name == "ip" and settings.modem.mode == "connect")
        add("dependency:" + name, "ok" if available else ("error" if required else "warning"),
            f"{name} is available" if available else f"{name} is missing")

    code = ("import sys; assert sys.version_info >= (3, 10), 'Python >= 3.10 required'; "
            "import gi; gi.require_version('Qmi', '1.0'); from gi.repository import Qmi")
    rc, _, stderr = query(sys.executable, "-c", code)
    add("python:qmi", "ok" if rc == 0 else "error",
        "Python >= 3.10 and libqmi GI bindings are available" if rc == 0
        else reason(stderr, "Cannot load Python >= 3.10 and libqmi GI bindings"))

    try:
        devices = scan_devices(sys_root=sys_root, dev_root=dev_root)
    except OSError as error:
        devices = []
        add("usb:scan", "warning", f"Cannot inspect USB devices: {error}")
    explicit = Path(settings.modem.device).resolve() if settings.modem.device != "auto" else None
    selected = [device for device in devices
                if (explicit is None or Path(device.path).resolve() == explicit)
                and (not settings.modem.serial or settings.modem.serial == device.serial)]
    if len(selected) > 1 and (explicit is not None or settings.modem.serial):
        add("modem", "error", "Multiple matching QMI modems; set modem.serial or modem.device")
    elif selected:
        for device in selected:
            add("modem" if len(selected) == 1 else 'modem:'+device.interface, "ok",
                f"QMI modem {device.product or device.identity}: {device.path}, {device.interface}")
    elif devices:
        add("modem", "warning", "No QMI modem matches the configured device/serial; waiting for that modem")
    else:
        add("modem", "warning", "No usable QMI modem connected; hotplug is supported and installation may continue")

    # Explain excluded control ports instead of treating every cdc-wdm as QMI.
    for entry in sorted((Path(sys_root) / "class/usbmisc").glob("cdc-wdm*")):
        function = (entry / "device").resolve()
        driver_link = function / "driver"
        driver = driver_link.resolve().name if driver_link.is_symlink() or driver_link.exists() else "unknown"
        if driver == "qmi_wwan" and any(Path(device.path).name == entry.name for device in devices):
            continue
        if driver == "cdc_mbim":
            detail = "cdc_mbim exposes MBIM, which this QMI supervisor does not manage"
        elif driver == "qmi_wwan":
            detail = "qmi_wwan control port has no usable device/network-interface association"
        else:
            detail = f"Driver {driver} is not a usable qmi_wwan function"
        add("port:" + entry.name, "warning", detail)

    if commands["ip"]:
        for device in selected:
            rc, output, stderr = query("ip", "-j", "-4", "addr", "show", "dev", device.interface)
            if rc != 0:
                add("interface:" + device.interface, "warning", reason(stderr, "Cannot inspect IPv4 configuration"))
                continue
            try:
                addresses = json.loads(output)
                occupied = any(address.get("scope") == "global"
                               for interface in addresses for address in interface.get("addr_info", []))
            except (ValueError, TypeError, AttributeError):
                add("interface:" + device.interface, "warning", "Cannot parse interface IPv4 configuration")
                continue
            add("interface:" + device.interface, "warning" if occupied else "ok",
                "Existing IPv4 configuration; confirm its owner before connect mode takes over"
                if occupied else "No existing global IPv4 configuration")

    if commands["systemctl"]:
        for service in ("ModemManager", "NetworkManager"):
            rc, _, stderr = query("systemctl", "is-active", "--quiet", service + ".service")
            if rc == 0:
                add("manager:" + service, "warning",
                    f"{service} is active; the selected QMI modem must be excluded from competing management")
            elif rc is None or stderr.strip():
                add("manager:" + service, "warning", reason(stderr, f"Cannot determine whether {service} is active"))
            else:
                add("manager:" + service, "ok", f"{service} is not active")

    if settings.modem.mode == "monitor" or settings.network.dns == "none":
        add("dns", "ok", "DNS management is disabled")
    else:
        backend = settings.network.dns
        resolved_error = "resolvectl is missing"
        if backend in {"auto", "resolvectl"} and which("resolvectl"):
            rc, _, stderr = query("resolvectl", "status")
            if rc == 0:
                add("dns", "ok", "systemd-resolved is operational (resolvectl status succeeded)")
                return checks
            resolved_error = reason(stderr, "systemd-resolved is not operational")
        if backend in {"auto", "resolvconf"} and which("resolvconf"):
            add("dns", "ok", "resolvconf is available for per-interface DNS updates")
        else:
            detail = resolved_error if backend == "resolvectl" else "No operational DNS backend; install/enable systemd-resolved or resolvconf, or set network.dns=none"
            add("dns", "error", detail)
    return checks


def diagnose_configuration(configuration, **kwargs):
    """Include requirements and selectors from every configured modem profile."""
    if not configuration.profiles:
        return diagnose(configuration.base, **kwargs)
    checks = []
    for identifier, settings in [('default', configuration.base), *sorted(configuration.profiles.items())]:
        for check in diagnose(settings, **kwargs):
            checks.append(dict(check, name=identifier+'/'+check['name']))
    return checks


def print_report(checks):
    """Print a compact human-readable report and return its success status."""
    for check in checks:
        print(f"[{check['status'].upper()}] {check['name']}: {check['detail']}")
    return not any(check["status"] == "error" for check in checks)
