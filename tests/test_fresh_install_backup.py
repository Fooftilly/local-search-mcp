"""Fresh-install recovery smoke tests without touching real application state."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import domain_policy
import cache_settings
import history_index
import safe_settings
import upstream_settings
from history_log import append_history
from scripts import backup_state, restore_smoke


ROOT = Path(__file__).resolve().parents[1]


class FreshRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.original_home = self.root / "original-xdg"
        self.original_home.mkdir(mode=0o700)
        self.archive = self.root / "private-backup.zip"
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.original_home),
            "ALLOWED_CRAWL_DOMAINS": "",
        })
        env.start()
        self.addCleanup(env.stop)
        # Drop inherited custom-history settings so this test cannot look at
        # an operator's unrelated log path.
        for key in ("LOCAL_SEARCH_MCP_HISTORY_FILE", "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS"):
            old = os.environ.pop(key, None)
            self.addCleanup(self._restore_env, key, old)
        domain_policy.initialize()
        domain_policy.decide("research.example.org", "allow", scope="exact")
        domain_policy.decide("blocked.example.net", "deny")
        moment = datetime.now(timezone.utc).isoformat()
        append_history({
            "time_utc": moment, "kind": "fetch", "status": "success",
            "url": "https://research.example.org/private?token=SECRET_TOKEN",
            "duration_ms": 12.0,
        })
        append_history({
            "time_utc": moment, "kind": "search", "status": "blocked",
            "query": "SECRET_SEARCH_QUERY", "duration_ms": 1.5,
        })
        history_index.sync_history()
        self.expected_policy_count = len(domain_policy.entries())
        self.assertEqual(len(history_index.search_history(limit=25)), 2)
        # Source is intentionally private and may contain unrelated
        # credentials; neither the archive nor recovery may include them.
        config = domain_policy.storage_dir() / "config.json"
        config.write_text('{"token":"DO_NOT_COPY"}')
        config.chmod(0o600)
        backup_state.backup(self.archive, state_dir=domain_policy.storage_dir())

    @staticmethod
    def _restore_env(key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def test_application_reopens_permissions_and_rebuilds_history(self):
        state = domain_policy.storage_dir()
        source_db = state / "domains.sqlite3"
        before = source_db.read_bytes()
        with zipfile.ZipFile(self.archive) as z:
            self.assertNotIn("config.json", z.namelist())
            self.assertFalse(any(n.endswith(".sqlite3") and n != "domains.sqlite3"
                                 for n in z.namelist()))
        result = restore_smoke.smoke(self.archive)
        self.assertEqual(result, (self.expected_policy_count, 2))
        self.assertEqual(source_db.read_bytes(), before)
        self.assertEqual(len(history_index.search_history(limit=25)), 2)
        self.assertTrue((state / "config.json").is_file())
        self.assertEqual(os.environ["XDG_STATE_HOME"], str(self.original_home))

    def test_isolation_ignores_configured_live_history_path(self):
        # Even a custom path outside the state directory must never be
        # accessed by the smoke test.
        nonexistent = self.root / "source-secrets" / "never-open.jsonl"
        with patch.dict(os.environ, {
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(nonexistent),
            "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "1",
            "ALLOWED_CRAWL_DOMAINS": "unsafe.example.com",
        }):
            self.assertEqual(
                restore_smoke.smoke(self.archive),
                (self.expected_policy_count, 2),
            )
            self.assertEqual(os.environ["LOCAL_SEARCH_MCP_HISTORY_FILE"],
                             str(nonexistent))
            self.assertEqual(os.environ["LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS"], "1")
        self.assertFalse(nonexistent.exists())

    def test_recovery_reads_archived_settings_only_from_isolated_config(self):
        config = self.root / "source-config"
        config.mkdir(mode=0o700)
        documents = {
            "history-settings.json": {
                "schema": safe_settings.SCHEMA,
                "settings": {"retention_days": 45, "max_bytes": 65536},
            },
            "upstream-timeouts.json": {
                "schema": upstream_settings.SCHEMA,
                "settings": {"searxng_seconds": 10, "crawl_seconds": 90},
            },
        }
        for name, document in documents.items():
            path = config / name
            path.write_text(json.dumps(document))
            path.chmod(0o600)
        secret = config / "config.json"
        secret.write_text('{"token":"NEVER_RESTORE_THIS"}')
        secret.chmod(0o600)
        archive = self.root / "with-settings.zip"
        backup_state.backup(archive, state_dir=domain_policy.storage_dir(),
                            config_dir=config)
        self.assertNotIn("config.json", backup_state.verified_payload(archive))

        live = self.root / "live-config"
        live.mkdir(mode=0o700)
        (live / "local-search-mcp").mkdir(mode=0o700)
        # Malformed live configuration and overrides must not leak into
        # fresh-install recovery or mask the archived values.
        (live / "local-search-mcp" / "history-settings.json").write_text("invalid")
        original_history = safe_settings.dashboard_settings
        original_upstream = upstream_settings.dashboard_settings
        def check_history():
            home = Path(os.environ["XDG_CONFIG_HOME"])
            self.assertNotEqual(home, live)
            self.assertIn("mcp-fresh-restore-", str(home))
            self.assertEqual((home / "local-search-mcp" / "history-settings.json").stat().st_mode & 0o777, 0o600)
            self.assertFalse((home / "local-search-mcp" / "config.json").exists())
            result = original_history()
            self.assertEqual(result["values"], documents["history-settings.json"]["settings"])
            return result
        def check_upstream():
            result = original_upstream()
            self.assertEqual(result["values"], documents["upstream-timeouts.json"]["settings"])
            return result
        with patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(live),
            "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "1",
            "LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES": "64000",
            "LOCAL_SEARCH_MCP_SEARXNG_TIMEOUT": "120",
            "LOCAL_SEARCH_MCP_CRAWL_TIMEOUT": "300",
        }), patch.object(safe_settings, "dashboard_settings", side_effect=check_history) as history_reader, patch.object(
            upstream_settings, "dashboard_settings", side_effect=check_upstream
        ) as upstream_reader:
            self.assertEqual(restore_smoke.smoke(archive),
                             (self.expected_policy_count, 2))
            history_reader.assert_called_once()
            upstream_reader.assert_called_once()
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], str(live))
            self.assertEqual(os.environ["LOCAL_SEARCH_MCP_SEARXNG_TIMEOUT"], "120")
        self.assertEqual(secret.read_text(), '{"token":"NEVER_RESTORE_THIS"}')

    def test_fresh_recovery_validates_optional_cache_ttl_runtime_reader(self):
        # Verified archive settings must be read from the isolated recovery
        # config, not silently shadowed by live environment overrides.
        config = self.root / "cache-source-config"
        config.mkdir(mode=0o700)
        values = {"search_seconds": 80, "fetch_seconds": 240}
        file = config / "cache-ttl.json"
        file.write_text(json.dumps({
            "schema": cache_settings.SCHEMA, "settings": values,
        }))
        file.chmod(0o600)
        secret = config / "config.json"
        secret.write_text('{"token":"UNRELATED_PRIVATE_CREDENTIAL"}')
        secret.chmod(0o600)
        archive = self.root / "cache-backup.zip"
        backup_state.backup(archive, state_dir=domain_policy.storage_dir(),
                            config_dir=config)
        payload = backup_state.verified_payload(archive)
        self.assertIn("cache-ttl.json", payload)
        self.assertNotIn("config.json", payload)
        self.assertNotIn(b"UNRELATED_PRIVATE_CREDENTIAL",
                         b"".join(payload.values()))

        live_config = self.root / "live-cache-config"
        live_config.mkdir(mode=0o700)
        original_reader = cache_settings.dashboard_settings

        def read_restored_cache():
            current = Path(os.environ["XDG_CONFIG_HOME"])
            self.assertNotEqual(current, live_config)
            self.assertIn("mcp-fresh-restore-", str(current))
            restored = current / "local-search-mcp" / "cache-ttl.json"
            self.assertEqual(restored.stat().st_mode & 0o777, 0o600)
            self.assertFalse((restored.parent / "config.json").exists())
            report = original_reader()
            self.assertEqual(report["values"], values)
            self.assertTrue(report["editable"])
            return report

        with (patch.dict(os.environ, {
                  "XDG_CONFIG_HOME": str(live_config),
                  "LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL": "5",
                  "LOCAL_SEARCH_MCP_CACHE_FETCH_TTL": "5",
              }),
              patch.object(cache_settings, "dashboard_settings",
                           side_effect=read_restored_cache) as reader):
            self.assertEqual(restore_smoke.smoke(archive),
                             (self.expected_policy_count, 2))
            reader.assert_called_once_with()
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], str(live_config))
            self.assertEqual(os.environ["LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL"], "5")
        self.assertEqual(secret.read_text(),
                         '{"token":"UNRELATED_PRIVATE_CREDENTIAL"}')

    def test_on_demand_restore_smoke_reads_only_isolated_opt_in_settings(self):
        from scripts import on_demand_settings
        config = self.root / "saved-on-demand-config"
        config.mkdir(mode=0o700)
        raw = {"schema": on_demand_settings.SCHEMA,
               "settings": {"enabled": True, "idle_minutes": 60}}
        file = config / on_demand_settings.NAME
        file.write_text(json.dumps(raw))
        file.chmod(0o600)
        archive = self.root / "on-demand-backup.zip"
        backup_state.backup(archive, state_dir=domain_policy.storage_dir(),
                            config_dir=config)
        self.assertIn(on_demand_settings.NAME, backup_state.verified_payload(archive))

        live_config = self.root / "live-on-demand-config"
        live_config.mkdir(mode=0o700)
        (live_config / "local-search-mcp").mkdir(mode=0o700)
        (live_config / "local-search-mcp" / on_demand_settings.NAME).write_text("{bad")
        original_read = on_demand_settings.read

        def read_staged():
            folder = Path(os.environ["XDG_CONFIG_HOME"])
            self.assertIn("mcp-fresh-restore-", str(folder))
            self.assertNotEqual(folder, live_config)
            target = folder / "local-search-mcp" / on_demand_settings.NAME
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(original_read(), raw["settings"])
            self.assertFalse((target.parent / "config.json").exists())
            return raw["settings"]

        with (patch.dict(os.environ, {"XDG_CONFIG_HOME": str(live_config)}),
              patch.object(on_demand_settings, "read",
                           side_effect=read_staged) as reader):
            self.assertEqual(restore_smoke.smoke(archive),
                             (self.expected_policy_count, 2))
            reader.assert_called_once_with()
        self.assertEqual((live_config / "local-search-mcp" /
                          on_demand_settings.NAME).read_text(), "{bad")

    def test_missing_cache_file_never_loads_cache_runtime_reader(self):
        with patch.object(cache_settings, "dashboard_settings",
                          side_effect=AssertionError("No cache settings archived")):
            self.assertEqual(restore_smoke.smoke(self.archive),
                             (self.expected_policy_count, 2))

    def test_without_settings_never_inspects_live_config(self):
        live = self.root / "live-invalid-config"
        live.mkdir(mode=0o700)
        (live / "local-search-mcp").mkdir(mode=0o700)
        (live / "local-search-mcp" / "upstream-timeouts.json").write_text("{bad")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(live),
                                     "LOCAL_SEARCH_MCP_CRAWL_TIMEOUT": "300"}):
            self.assertEqual(restore_smoke.smoke(self.archive),
                             (self.expected_policy_count, 2))
            self.assertEqual(os.environ["XDG_CONFIG_HOME"], str(live))

    def test_subprocess_cli_smokes_archive_offline(self):
        result = subprocess.run(
            [sys.executable, "scripts/restore_smoke.py", str(self.archive)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fresh-install smoke check passed", result.stdout)
        self.assertIn("No live state changed", result.stdout)
        self.assertNotIn("SECRET_SEARCH_QUERY", result.stdout)
        self.assertNotIn("SECRET_TOKEN", result.stdout)

    def test_smoke_audits_every_staged_file_before_application_reads(self):
        original_check = restore_smoke._verify_staged_copy
        inspected = []

        def audit(path, expected):
            original_check(path, expected)
            inspected.append(path.name)

        with patch.object(restore_smoke, "_verify_staged_copy", side_effect=audit):
            self.assertEqual(restore_smoke.smoke(self.archive),
                             (self.expected_policy_count, 2))
        self.assertIn("domains.sqlite3", inspected)
        self.assertIn("history.jsonl", inspected)
        self.assertEqual(set(inspected),
                         set(backup_state.verified_payload(self.archive)))

    def test_staged_copy_rejects_changed_bytes_modes_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            expected = b'{"query":"private"}\\n'
            path.write_bytes(expected)
            path.chmod(0o600)
            restore_smoke._verify_staged_copy(path, expected)

            path.write_bytes(b'{"query":"altered"}\\n')
            with self.assertRaisesRegex(ValueError, "bytes differ"):
                restore_smoke._verify_staged_copy(path, expected)
            path.write_bytes(expected)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "permissions"):
                restore_smoke._verify_staged_copy(path, expected)
            path.unlink()
            outside = Path(tmp) / "outside"
            outside.write_bytes(expected)
            path.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "type"):
                restore_smoke._verify_staged_copy(path, expected)

    def test_corrupt_archive_is_rejected_before_initialization(self):
        broken = self.root / "not-a-zip"
        broken.write_bytes(b"damaged")
        broken.chmod(0o600)
        self.assertEqual(restore_smoke.main([str(broken)]), 1)
        self.assertFalse((self.original_home / "other-state").exists())


if __name__ == "__main__":
    unittest.main()
