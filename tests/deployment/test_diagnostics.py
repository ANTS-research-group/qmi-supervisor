from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from modules.deployment.diagnostics import diagnose, print_report
from modules.runtime.settings import Settings


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "dev").mkdir()
        self.calls = []
        self.available = {"ip", "systemctl", "udevadm", "resolvectl"}
        self.responses = {}
        self.settings = Settings()

    def modem(self, index=0, serial="test", driver="qmi_wwan"):
        usb = self.root / f"sys/devices/usb1/1-{index+1}"
        function = usb / f"1-{index+1}:1.4"
        (function / "net" / f"wwan{index}").mkdir(parents=True)
        (usb / "idVendor").write_text("2cb7")
        (usb / "serial").write_text(serial)
        (usb / "product").write_text("USB modem")
        (function / "driver").symlink_to(self.root / "sys/bus/usb/drivers" / driver)
        entry = self.root / f"sys/class/usbmisc/cdc-wdm{index}"
        entry.mkdir(parents=True)
        (entry / "device").symlink_to(function)
        (self.root / f"dev/cdc-wdm{index}").touch()

    def runner(self, args, **kwargs):
        self.calls.append((args, kwargs))
        key = tuple(args)
        if key in self.responses:
            response = self.responses[key]
            if isinstance(response, BaseException):
                raise response
            return subprocess.CompletedProcess(args, *response)
        if args[0] == "ip":
            return subprocess.CompletedProcess(args, 0, json.dumps([{"addr_info": []}]), "")
        if args[0] == "systemctl":
            return subprocess.CompletedProcess(args, 3, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def diagnose(self, settings=None):
        return {check["name"]: check for check in diagnose(
            settings or self.settings, sys_root=self.root / "sys", dev_root=self.root / "dev",
            runner=self.runner, which=lambda name: f"/usr/bin/{name}" if name in self.available else None)}

    def test_absent_usb_is_only_warning_and_commands_are_read_only(self):
        checks = self.diagnose()
        self.assertEqual(checks["modem"]["status"], "warning")
        self.assertFalse(any(check["status"] == "error" for check in checks.values()))
        allowed = {"systemctl", "resolvectl", sys.executable}
        for args, kwargs in self.calls:
            self.assertIn(args[0], allowed)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["timeout"], 15)
            if args[0] == "systemctl":
                self.assertEqual(args[1:3], ["is-active", "--quiet"])
            if args[0] == "resolvectl":
                self.assertEqual(args[1:], ["status"])
        self.assertEqual(list((self.root / "dev").iterdir()), [])

    def test_qmi_detection_and_existing_ipv4_warns_about_owner(self):
        self.modem()
        key = ("ip", "-j", "-4", "addr", "show", "dev", "wwan0")
        self.responses[key] = (0, json.dumps([{"addr_info": [{"scope": "global", "local": "10.0.0.2"}]}]), "")
        checks = self.diagnose()
        self.assertEqual(checks["modem"]["status"], "ok")
        self.assertEqual(checks["interface:wwan0"]["status"], "warning")
        self.assertIn("owner", checks["interface:wwan0"]["detail"])
        self.assertFalse(any(args[0] == "qmicli" for args, _ in self.calls))

    def test_multiple_modems_are_supported_and_an_explicit_serial_limits_diagnostics(self):
        self.modem(0, "first")
        self.modem(1, "second")
        checks = self.diagnose()
        self.assertEqual(checks['modem:wwan0']['status'], 'ok')
        self.assertEqual(checks['modem:wwan1']['status'], 'ok')
        configured = replace(self.settings, modem=replace(self.settings.modem, serial="second"))
        self.calls.clear()
        self.assertEqual(self.diagnose(configured)["modem"]["status"], "ok")
        self.assertEqual([args[-1] for args, _ in self.calls if args[0] == "ip"], ["wwan1"])

    def test_missing_selected_modem_is_warning_and_explicit_path_is_respected(self):
        self.modem()
        configured = replace(self.settings, modem=replace(self.settings.modem, device=str(self.root / "dev/cdc-wdm8")))
        checks = self.diagnose(configured)
        self.assertEqual(checks["modem"]["status"], "warning")
        self.assertNotIn("interface:wwan0", checks)

    def test_mbim_is_explained_and_is_never_selected(self):
        self.modem(driver="cdc_mbim")
        checks = self.diagnose()
        self.assertEqual(checks["modem"]["status"], "warning")
        self.assertIn("MBIM", checks["port:cdc-wdm0"]["detail"])
        self.assertFalse(any(args[0] == "ip" for args, _ in self.calls))

    def test_active_managers_warn_without_stopping_services(self):
        self.responses[("systemctl", "is-active", "--quiet", "ModemManager.service")] = (0, "", "")
        checks = self.diagnose()
        self.assertEqual(checks["manager:ModemManager"]["status"], "warning")
        self.assertEqual(checks["manager:NetworkManager"]["status"], "ok")

    def test_broken_resolved_uses_resolvconf_but_explicit_backend_does_not(self):
        self.responses[("resolvectl", "status")] = (1, "", "Failed to connect to bus")
        self.available.add("resolvconf")
        checks = self.diagnose()
        self.assertEqual(checks["dns"]["status"], "ok")
        self.assertIn("resolvconf", checks["dns"]["detail"])
        configured = replace(self.settings, network=replace(self.settings.network, dns="resolvectl"))
        self.assertEqual(self.diagnose(configured)["dns"]["status"], "error")

    def test_dns_none_and_monitor_do_not_require_or_inspect_dns(self):
        for configured in (
            replace(self.settings, network=replace(self.settings.network, dns="none")),
            replace(self.settings, modem=replace(self.settings.modem, mode="monitor")),
        ):
            with self.subTest(configured=configured):
                self.calls.clear()
                self.available.discard("resolvectl")
                self.assertEqual(self.diagnose(configured)["dns"]["status"], "ok")
                self.assertFalse(any(args[0] in {"resolvectl", "resolvconf"} for args, _ in self.calls))

    def test_missing_dependencies_gi_failure_and_missing_dns_are_errors(self):
        self.available = {"udevadm"}
        self.responses[(sys.executable, "-c", "import sys; assert sys.version_info >= (3, 10), 'Python >= 3.10 required'; import gi; gi.require_version('Qmi', '1.0'); from gi.repository import Qmi")] = (1, "", "No module named gi")
        checks = self.diagnose()
        for name in ("dependency:ip", "dependency:systemctl", "python:qmi", "dns"):
            self.assertEqual(checks[name]["status"], "error")

    def test_failed_queries_do_not_crash_report(self):
        self.modem()
        self.responses[("ip", "-j", "-4", "addr", "show", "dev", "wwan0")] = (0, "invalid JSON", "")
        self.responses[("resolvectl", "status")] = subprocess.TimeoutExpired("resolvectl", 15)
        self.assertEqual(self.diagnose()["interface:wwan0"]["status"], "warning")
        self.assertEqual(self.diagnose()["dns"]["status"], "error")

    def test_print_report_indicates_errors(self):
        output = StringIO()
        with redirect_stdout(output):
            result = print_report([{"name": "modem", "status": "warning", "detail": "No USB"}])
        self.assertTrue(result)
        self.assertIn("[WARNING] modem: No USB", output.getvalue())
        with redirect_stdout(StringIO()):
            self.assertFalse(print_report([{"name": "dns", "status": "error", "detail": "missing"}]))


if __name__ == "__main__":
    unittest.main()
