"""Private config hardening, precedence, and redacted diagnostic tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import config_diagnostics
import domain_policy


class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "local-search-mcp"
        self.file = self.directory / "config.json"
        self.environment = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        directory = patch.object(bootstrap, "PRIVATE_DIR", self.directory)
        config = patch.object(bootstrap, "PRIVATE_CONFIG", self.file)
        directory.start()
        config.start()
        self.addCleanup(directory.stop)
        self.addCleanup(config.stop)

    def test_first_run_and_atomic_owner_only_round_trip(self):
        self.assertEqual(bootstrap.read_config(), {})
        initial = {"CRAWL4AI_API_TOKEN": "secret", "CONTROL_PLANE_API_KEY": "runtime"}
        bootstrap.save_config(initial)
        self.assertEqual(bootstrap.read_config(), initial)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.directory.glob(".config-*")), [])
        bootstrap.save_config({**initial, "ALLOWED_CRAWL_DOMAINS": "example.com"})
        self.assertEqual(bootstrap.read_config()["CRAWL4AI_API_TOKEN"], "secret")

    def test_directory_permissions_block_unsafe_read_and_write(self):
        """Never trust configuration under a group/world-accessible directory."""
        self.directory.mkdir(mode=0o700)
        self.file.write_text('{"CRAWL4AI_API_TOKEN": "original"}')
        self.file.chmod(0o600)
        for mode in (0o750, 0o707, 0o777):
            self.directory.chmod(mode)
            with self.subTest(mode=oct(mode)):
                with self.assertRaisesRegex(bootstrap.SetupError, "directory permissions"):
                    bootstrap.read_config()
                with self.assertRaisesRegex(bootstrap.SetupError, "directory permissions"):
                    bootstrap.save_config({"CRAWL4AI_API_TOKEN": "replacement"})
        self.directory.chmod(0o700)
        self.assertEqual(bootstrap.read_config()["CRAWL4AI_API_TOKEN"], "original")

    def test_atomic_file_and_directory_sync_and_failed_rename(self):
        """A durable update syncs its temp file before rename and its directory after."""
        bootstrap.save_config({"CRAWL4AI_API_TOKEN": "original"})
        events = []
        fsync = os.fsync
        replace = os.replace

        def check_fsync(fd):
            events.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
            return fsync(fd)

        def check_replace(source, target):
            events.append(("replace", None))
            return replace(source, target)

        with (patch.object(bootstrap.os, "fsync", side_effect=check_fsync),
              patch.object(bootstrap.os, "replace", side_effect=check_replace)):
            bootstrap.save_config({"CRAWL4AI_API_TOKEN": "updated"})
        self.assertEqual(events, [
            ("fsync", False), ("replace", None), ("fsync", True),
        ])
        with patch.object(bootstrap.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                bootstrap.save_config({"CRAWL4AI_API_TOKEN": "bad"})
        self.assertEqual(bootstrap.read_config()["CRAWL4AI_API_TOKEN"], "updated")
        self.assertEqual(list(self.directory.glob(".config-*")), [])

    def test_directory_and_dangling_file_symlinks_are_refused(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir(mode=0o700)
        self.directory.symlink_to(other, target_is_directory=True)
        for action in (bootstrap.read_config, lambda: bootstrap.save_config({"test": "value"})):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(bootstrap.SetupError, "symlink"):
                action()
        self.directory.unlink()
        self.directory.mkdir(mode=0o700)
        self.file.symlink_to(self.directory / "missing-secret-file")
        with self.assertRaisesRegex(bootstrap.SetupError, "symlink"):
            bootstrap.read_config()
        with self.assertRaisesRegex(bootstrap.SetupError, "symlink"):
            bootstrap.save_config({"test": "value"})
        self.assertFalse((self.directory / "missing-secret-file").exists())

    def test_malformed_or_world_readable_secret_file_is_rejected(self):
        self.directory.mkdir(mode=0o700)
        self.file.write_text("{not JSON")
        self.file.chmod(0o600)
        with self.assertRaisesRegex(bootstrap.SetupError, "Cannot read"):
            bootstrap.read_config()
        self.file.write_text(json.dumps({"CRAWL4AI_API_TOKEN": 7}))
        with self.assertRaisesRegex(bootstrap.SetupError, "values must be strings"):
            bootstrap.read_config()
        self.file.chmod(0o644)
        with self.assertRaisesRegex(bootstrap.SetupError, "too broad"):
            bootstrap.read_config()

    def test_symlink_or_nonregular_destination_does_not_get_overwritten(self):
        self.directory.mkdir(mode=0o700)
        self.file.mkdir()
        with self.assertRaisesRegex(bootstrap.SetupError, "regular file"):
            bootstrap.save_config({"CRAWL4AI_API_TOKEN": "safe"})
        self.assertTrue(self.file.is_dir())

    def test_credentials_sources_are_redacted_and_reports_do_not_write(self):
        bootstrap.save_config({
            "CRAWL4AI_API_TOKEN": "saved-token-secret",
            "CONTROL_PLANE_API_KEY": "saved-runtime-secret",
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        with patch.dict(os.environ, {
            "CONTROL_PLANE_API_KEY": "override-secret",
            "SEARXNG_URL": "http://user:othersecret@remote.example/secret",
        }):
            with patch.object(bootstrap, "read_legacy_env", return_value={
                "CRAWL4AI_API_TOKEN": "legacy-secret",
            }), patch.object(bootstrap, "check_services") as services, \
                 patch.object(bootstrap, "docker_token") as docker, \
                 patch.object(bootstrap, "save_config") as save, \
                 patch("sys.argv", ["bootstrap.py", "--show-config"]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = bootstrap.main()
                self.assertEqual(code, 0)
                services.assert_not_called()
                docker.assert_not_called()
                save.assert_not_called()
                data = json.loads(output.getvalue())
                self.assertEqual(data["secrets"]["CONTROL_PLANE_API_KEY"]["source"], "environment")
                self.assertEqual(data["secrets"]["CRAWL4AI_API_TOKEN"]["source"], "private JSON")
                self.assertFalse(data["endpoints"]["searxng"]["valid_loopback"])
                self.assertIn("<redacted>", output.getvalue())
                for secret in ("saved-token-secret", "saved-runtime-secret", "override-secret",
                               "legacy-secret", "othersecret"):
                    self.assertNotIn(secret, output.getvalue())

    def test_sqlite_approval_rules_survive_new_launcher_seed_value(self):
        domain_policy.initialize()
        domain_policy.decide("example.com", "deny")
        with patch.dict(os.environ, {"ALLOWED_CRAWL_DOMAINS": "python.org"}):
            domain_policy.initialize()
            approvals = {entry["domain"]: entry["status"] for entry in domain_policy.entries()}
            self.assertEqual(approvals["example.com"], "denied")
            self.assertNotIn("python.org", approvals)

    def test_settings_precedence_does_not_replace_existing_credentials(self):
        old = {
            "CRAWL4AI_API_TOKEN": "existing-crawl-secret",
            "CONTROL_PLANE_API_KEY": "existing-tunnel-secret",
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        }
        with patch.object(bootstrap, "docker_token", return_value=""), \
             patch.object(bootstrap, "read_legacy_env", return_value={
                 "CRAWL4AI_API_TOKEN": "legacy-crawl", "CONTROL_PLANE_API_KEY": "legacy-key",
             }):
            self.assertEqual(bootstrap.collect_settings(old)["CRAWL4AI_API_TOKEN"], "existing-crawl-secret")
            self.assertEqual(bootstrap.collect_settings(old)["CONTROL_PLANE_API_KEY"], "existing-tunnel-secret")
            with patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": "explicit-override"}):
                self.assertEqual(bootstrap.collect_settings(old)["CONTROL_PLANE_API_KEY"], "explicit-override")

    def test_sqlite_seed_state_is_read_only_and_detects_links(self):
        """Authority is based on the seed marker, not merely database existence."""
        database = Path(self.tmp.name) / "local-search-mcp" / "domains.sqlite3"
        self.assertIn("initial seed", config_diagnostics._database_authority(database))
        database.parent.mkdir(mode=0o700)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
        stamp = database.stat().st_mtime_ns
        self.assertIn("seed pending", config_diagnostics._database_authority(database))
        self.assertEqual(database.stat().st_mtime_ns, stamp)
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO metadata VALUES ('domain_seed_v1', '1')")
        stamp = database.stat().st_mtime_ns
        self.assertIn("persisted SQLite rules", config_diagnostics._database_authority(database))
        self.assertEqual(database.stat().st_mtime_ns, stamp)
        self.assertFalse(Path(str(database) + "-shm").exists())
        self.assertFalse(Path(str(database) + "-wal").exists())

        target = Path(self.tmp.name) / "separate"
        target.mkdir()
        (target / "domains.sqlite3").write_bytes(database.read_bytes())
        linked = Path(self.tmp.name) / "linked"
        linked.symlink_to(target, target_is_directory=True)
        self.assertIn("unsafe SQLite symlink", config_diagnostics._database_authority(linked / "domains.sqlite3"))
        with patch.dict(os.environ, {"ALLOWED_CRAWL_DOMAINS": "python.org"}):
            report = config_diagnostics.collect_report(
                {"ALLOWED_CRAWL_DOMAINS": "example.com"}, Path(self.tmp.name), self.directory, lambda p: {})
        self.assertEqual(report["domain_policy"]["interactive_setup_seed_source"], "environment")
        self.assertEqual(report["domain_policy"]["systemd_service_seed_source"], "private JSON")

    def test_diagnostic_endpoint_and_dashboard_port_validation(self):
        report = config_diagnostics.collect_report({}, Path(self.tmp.name), self.directory, lambda path: {})
        self.assertEqual(report["endpoints"]["searxng"]["value"], "127.0.0.1:8888")
        self.assertEqual(report["endpoints"]["searxng"]["transport"], "HTTP over loopback")
        self.assertEqual(report["endpoints"]["dashboard_port"]["value"], 8791)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_DASHBOARD_PORT": "not-a-port",
                                      "CRAWL4AI_URL": "http://127.0.0.1:99999/"}):
            report = config_diagnostics.collect_report({}, Path(self.tmp.name), self.directory, lambda path: {})
            self.assertEqual(report["endpoints"]["dashboard_port"]["value"], "<invalid>")
            self.assertFalse(report["endpoints"]["crawl4ai"]["valid_loopback"])
        for url in ("http://127.0.0.1:8888?", "http://127.0.0.1:8888#"):
            with self.subTest(url=url), patch.dict(os.environ, {"SEARXNG_URL": url}):
                result = config_diagnostics.collect_report({}, Path(self.tmp.name), self.directory, lambda p: {})
                self.assertFalse(result["endpoints"]["searxng"]["valid_loopback"])


if __name__ == "__main__":
    unittest.main()
