"""No-root tests of the opt-in egress-before-tunnel systemd design."""
from pathlib import Path
import unittest

from scripts import install_user_services


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "config/systemd-system/local-search-mcp-crawler-egress.service"
DROPIN = ROOT / "config/systemd/guards/10-crawler-egress.conf"
GUARD = "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py"


class GuardSystemdTests(unittest.TestCase):
    def test_system_service_uses_only_root_owned_install_location(self):
        service = SERVICE.read_text()
        self.assertIn("After=docker.service", service)
        self.assertIn("RemainAfterExit=yes", service)
        self.assertIn("Restart=on-failure", service)
        self.assertIn(GUARD, service)
        self.assertIn(" --apply", service)
        self.assertIn(" --probe", service)
        self.assertNotIn("/home/", service)
        self.assertNotIn("$HOME", service)

    def test_user_tunnel_dropin_probe_fails_closed(self):
        rendered = install_user_services.unit_files("/usr/bin/python3", Path("/tmp/project"))
        self.assertNotIn("ExecStartPre", rendered["local-search-mcp-tunnel.service"])
        dropin = DROPIN.read_text()
        self.assertIn("ExecStartPre=+/usr/bin/sudo -n /usr/bin/python3 " + GUARD, dropin)
        self.assertIn("--container crawl4ai --probe", dropin)
        self.assertNotIn("ExecStartPre=-", dropin)
        self.assertIn("NoNewPrivileges=true", rendered["local-search-mcp-tunnel.service"])
        self.assertEqual(dropin.count("ExecStartPre=+"), 1)
        self.assertNotIn("NoNewPrivileges=false", dropin)
        self.assertNotIn("ExecStart=", dropin)
        self.assertIn("sudo -n", dropin)
        self.assertNotIn(" --apply", dropin)
        self.assertNotIn("git pull", dropin)


if __name__ == "__main__":
    unittest.main()
