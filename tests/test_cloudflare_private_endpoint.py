"""Static guard contracts and mocked privilege lifecycle: no root operations."""
import tempfile
import unittest
import stat
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from scripts import cloudflare_private_endpoint as endpoint


class PrivateEndpointTests(unittest.TestCase):
    def test_rule_blocks_all_nonloopback_input(self):
        self.assertIn('ip daddr 10.253.91.1 iifname != "lo" drop',
                      endpoint.RULES)
        self.assertIn("hook input priority -150", endpoint.RULES)

    def test_prepare_places_guard_before_private_ip(self):
        operations = []

        def run(*args, input=None):
            operations.append((args, input))
            return type("Result", (), {"returncode": 0,
                                       "stdout": "", "stderr": ""})()

        with (tempfile.TemporaryDirectory() as tmp,
              patch.object(endpoint, "MARKER", Path(tmp) / "owned"),
              patch.object(endpoint, "invocation_id", return_value="a" * 32),
              patch.object(endpoint, "command", side_effect=run),
              patch.object(endpoint, "exists_ip", return_value=False),
              patch.object(endpoint, "exists_table", return_value=False)):
            endpoint.prepare()
            self.assertTrue(endpoint.MARKER.is_file())
        self.assertEqual(operations[0][0][:3], ("/usr/bin/nft", "-f", "-"))
        self.assertEqual(operations[1][0][:4],
                         ("/usr/bin/ip", "-4", "addr", "add"))

    def test_existing_resources_abort_without_mutation(self):
        with (patch.object(endpoint, "exists_ip", return_value=True),
              patch.object(endpoint, "command") as run,
              patch.object(endpoint, "MARKER",
                           Path("/nonexistent-cloudflare-test-marker"))):
            with self.assertRaisesRegex(RuntimeError, "already exist"):
                endpoint.prepare()
            run.assert_not_called()

    def test_cleanup_removes_address_before_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "owned"
            marker.write_text("a" * 32 + "\n")
            marker.chmod(0o600)
            calls = []

            def run(*args, input=None):
                calls.append(args)
                return type("Result", (), {"returncode": 0,
                                           "stdout": "", "stderr": ""})()

            with (patch.object(endpoint, "MARKER", marker),
                  patch.object(endpoint, "invocation_id", return_value="a" * 32),
                  patch.object(Path, "lstat",
                               return_value=SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)),
                  patch.object(endpoint, "exists_ip", return_value=True),
                  patch.object(endpoint, "exists_table", return_value=True),
                  patch.object(endpoint, "command", side_effect=run)):
                endpoint.remove()
            self.assertEqual(calls[0][:4], ("/usr/bin/ip", "-4", "addr", "del"))
            self.assertEqual(calls[1][:3], ("/usr/bin/nft", "delete", "table"))
            self.assertFalse(marker.exists())

    def test_other_invocation_cannot_cleanup_active_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "owned"
            marker.write_text("a" * 32 + "\n")
            marker.chmod(0o600)
            with (patch.object(endpoint, "MARKER", marker),
                  patch.object(endpoint, "invocation_id", return_value="b" * 32),
                  patch.object(Path, "lstat",
                               return_value=SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)),
                  patch.object(endpoint, "command") as run):
                endpoint.remove()
                run.assert_not_called()
                self.assertTrue(marker.exists())

    def test_remove_without_marker_does_not_touch_foreign_state(self):
        with (patch.object(endpoint, "MARKER",
                           Path("/nonexistent-cloudflare-test-marker")),
              patch.object(endpoint, "command") as run):
            endpoint.remove()
            run.assert_not_called()

    def test_templates_keep_forwarder_unprivileged(self):
        root = Path(__file__).resolve().parents[1]
        unit = (root / "config/systemd-system" /
                "local-search-mcp-cf-private@.service").read_text()
        self.assertIn("User=%i", unit)
        self.assertIn("ExecStartPre=+", unit)
        self.assertIn("ExecStopPost=+", unit)
        self.assertIn("ExecStart=/usr/bin/socat", unit)
        self.assertNotIn("0.0.0.0", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        dns = (root / "config/systemd-system" /
               "local-search-mcp-cf-dns.service").read_text()
        self.assertIn("--keep-in-foreground", dns)
        config = (root / "config/dns" / "cloudflare-portal.conf").read_text()
        self.assertIn("listen-address=127.0.0.1", config)
        self.assertIn("server=100.100.100.100", config)


if __name__ == "__main__":
    unittest.main()
