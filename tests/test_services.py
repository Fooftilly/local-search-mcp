"""Test user-service generation, unattended execution and health without live services."""
from pathlib import Path
from http.client import BadStatusLine, IncompleteRead, LineTooLong
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import bootstrap
import health_cli
from scripts import install_user_services as installer
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

    def test_units_are_rendered_from_version_controlled_templates(self):
        expected = set(installer.NAMES) | {installer.GUARD_PROBE}
        self.assertEqual({p.name for p in installer.TEMPLATE_DIR.iterdir()
                          if p.is_file()}, expected)
        units = unit_files("/usr/bin/python3", Path("/tmp/demo"),
                           auto_restart=True, guard_probe=True,
                           reflog_path=Path("/tmp/demo/.git/logs/HEAD"),
                           state_path=Path("/tmp/service-state"))
        self.assertEqual(set(units), expected)
        for name in expected:
            template = (installer.TEMPLATE_DIR / name).read_text()
            self.assertTrue(template.startswith(installer.MARKER))
            self.assertNotIn("CRAWL4AI_API_TOKEN=", template)
            self.assertNotIn("CONTROL_PLANE_API_KEY=", template)
            self.assertTrue(units[name].startswith(installer.MARKER))
            self.assertNotIn("$EXE", units[name])
            self.assertNotIn("$LAUNCH", units[name])
            self.assertNotIn("$WATCH_PATH", units[name])

    def test_template_symlink_or_missing_source_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as dirname:
            folder = Path(dirname)
            reference = folder / installer.NAMES[0]
            target = folder / "elsewhere"
            target.write_text(installer.MARKER + "[Unit]\n")
            reference.symlink_to(target)
            with patch.object(installer, "TEMPLATE_DIR", folder):
                with self.assertRaisesRegex(ValueError, "linked"):
                    unit_files("/usr/bin/python3", Path("/tmp/project"))
            reference.unlink()
            with patch.object(installer, "TEMPLATE_DIR", folder):
                with self.assertRaises(FileNotFoundError):
                    unit_files("/usr/bin/python3", Path("/tmp/project"))

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

    def test_dashboard_probes_compact_health_endpoint(self):
        """A healthy dashboard must not be marked down by its large HTML page."""
        def dashboard_reply(port, path, max_bytes):
            self.assertEqual((port, path, max_bytes), (8791, "/healthz", 1024))
            return 200, b'{"status": "ok"}'

        with patch.object(health_cli, "local_get", side_effect=dashboard_reply) as request:
            self.assertTrue(health_cli.dashboard_ok())
            request.assert_called_once()

    def test_dashboard_health_rejects_invalid_content(self):
        """An unrelated 200 page, malformed JSON or redirect is not healthy."""
        for status, body in (
            (200, b"<html>not a health response</html>"),
            (200, b'{"status": "error"}'),
            (200, b'{"status": "ok", "ignored": true}'),
            (302, b'{"status": "ok"}'),
        ):
            with self.subTest(status=status, body=body), \
                 patch.object(health_cli, "local_get", return_value=(status, body)):
                self.assertFalse(health_cli.dashboard_ok())

    def test_redirects_are_refused_without_off_host_fetching(self):
        """HTTP redirects never trigger outbound follow-up requests."""
        with patch.object(health_cli, "local_get", return_value=(302, b"moved")) as call:
            self.assertFalse(health_cli.dashboard_ok())
            self.assertEqual(call.call_count, 1)
        with self.assertRaises(ValueError):
            health_cli.local_get(443, "/health")

    def test_units_limit_restart_bursts_and_no_invalid_working_directory(self):
        """Systemd units must never use ExecStart quoting for WorkingDirectory."""
        units = unit_files("/usr/bin/python3", Path("/tmp/local mcp"))
        for name in ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service"):
            self.assertIn("StartLimitIntervalSec=300", units[name])
            self.assertIn("StartLimitBurst=5", units[name])
        self.assertTrue(all("WorkingDirectory=" not in unit for unit in units.values()))

    def test_malformed_or_truncated_http_marks_local_services_unavailable(self):
        """Protocol parsing failures must not abort periodic monitoring."""
        failures = (
            BadStatusLine("unexpected response"),
            LineTooLong("malformed response header"),
            IncompleteRead(b"partial", 15),
        )
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                with patch.object(health_cli, "local_get", side_effect=failure):
                    self.assertFalse(health_cli.dashboard_ok())
                    self.assertFalse(health_cli.searxng_ok())
                with patch.object(health_cli, "searxng_ok", return_value=True), \
                     patch.object(health_cli, "get_json", side_effect=failure), \
                     patch.object(health_cli, "listening", side_effect=[True, True]), \
                     patch.object(health_cli, "dashboard_ok", return_value=True):
                    status = health_cli.snapshot()
                self.assertEqual(status["status"], "degraded")
                self.assertFalse(status["checks"]["crawl4ai"])
                with patch.object(health_cli, "snapshot", return_value=status):
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        exit_code = health_cli.main(["--json"])
                self.assertEqual(exit_code, 1)
                self.assertIs(json.loads(stdout.getvalue())["checks"]["crawl4ai"], False)

    def test_unhealthy_health_report_has_nonzero_exit(self):
        with patch.object(health_cli, "snapshot", return_value={
            "status": "unavailable", "checks": {}, "note": "offline"
        }):
            self.assertEqual(health_cli.main(["--json"]), 1)


if __name__ == "__main__":
    unittest.main()
