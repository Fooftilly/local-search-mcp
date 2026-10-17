"""Test user-service generation, unattended execution and health without live services."""
from pathlib import Path
import unittest
from unittest.mock import patch

import bootstrap
import health_cli
from scripts.install_user_services import unit_files


class ServiceTests(unittest.TestCase):
    def test_units_are_secrets_free_and_restart_with_delay(self):
        units = unit_files("/usr/bin/python3", Path("/tmp/project with % space"))
        self.assertEqual(len(units), 4)
        for content in units.values():
            self.assertIn("Managed by local-search-mcp", content)
            self.assertNotIn("CONTROL_PLANE_API_KEY=", content)
            self.assertNotIn("CRAWL4AI_API_TOKEN=", content)
        for name in ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service", "local-search-mcp-health.service"):
            self.assertIn("%%", units[name])
        for name in ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service"):
            self.assertIn("Restart=on-failure", units[name])
            self.assertIn("RestartSec=30s", units[name])
        self.assertIn("After=local-search-mcp-bridge.service", units["local-search-mcp-tunnel.service"])
        self.assertIn("OnUnitActiveSec=2min", units["local-search-mcp-health.timer"])

    def test_bridge_exec_uses_prepared_private_state(self):
        config = {
            "CRAWL4AI_API_TOKEN": "crawl-secret",
            "CONTROL_PLANE_API_KEY": "tunnel-secret",
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        }
        with patch.object(bootstrap, "read_config", return_value=config), \
             patch.object(bootstrap.os, "execve") as execve, \
             patch.object(Path, "is_file", return_value=True):
            bootstrap.run_unattended_service("bridge")
        argv = execve.call_args.args
        self.assertEqual(argv[1][-1], str(bootstrap.ROOT / "bridge.py"))
        self.assertEqual(argv[2]["CRAWL4AI_API_TOKEN"], "crawl-secret")
        self.assertNotEqual(argv[2].get("CONTROL_PLANE_API_KEY"), "tunnel-secret")
        self.assertNotIn("crawl-secret", argv[1])

    def test_missing_config_fails_closed_without_prompting(self):
        with patch.object(bootstrap, "read_config", return_value={}):
            with self.assertRaises(bootstrap.SetupError):
                bootstrap.run_unattended_service("bridge")

    def test_health_reports_degraded_upstream_independently(self):
        with patch.object(health_cli, "get_json", return_value={"status": "ok", "version": "0.9.5"}), \
             patch.object(health_cli, "searxng_ok", return_value=True), \
             patch.object(health_cli, "listening", side_effect=[True, False]), \
             patch.object(health_cli, "dashboard_ok", return_value=True):
            result = health_cli.snapshot()
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["checks"]["crawl4ai"])
        self.assertFalse(result["checks"]["tunnel_listener"])

    def test_redirects_are_refused_without_off_host_fetching(self):
        """Local health requests never follow redirects to another host."""
        redirector = health_cli.NoRedirect()
        self.assertIsNone(redirector.redirect_request(None, None, 302, "Moved", {}, "https://elsewhere.test/"))
        with self.assertRaises(ValueError):
            health_cli.local_request("https://elsewhere.test/health")

    def test_units_limit_restart_bursts_and_no_invalid_working_directory(self):
        """Systemd units must never use ExecStart quoting for WorkingDirectory."""
        units = unit_files("/usr/bin/python3", Path("/tmp/local mcp"))
        for name in ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service"):
            self.assertIn("StartLimitIntervalSec=300", units[name])
            self.assertIn("StartLimitBurst=5", units[name])
        self.assertTrue(all("WorkingDirectory=" not in unit for unit in units.values()))

    def test_unhealthy_health_report_has_nonzero_exit(self):
        with patch.object(health_cli, "snapshot", return_value={
            "status": "unavailable", "checks": {}, "note": "offline"
        }):
            self.assertEqual(health_cli.main(["--json"]), 1)


if __name__ == "__main__":
    unittest.main()
