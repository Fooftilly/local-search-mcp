"""Fresh-install recovery smoke tests without touching real application state."""
from __future__ import annotations

from datetime import datetime, timezone
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
import history_index
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

    def test_corrupt_archive_is_rejected_before_initialization(self):
        broken = self.root / "not-a-zip"
        broken.write_bytes(b"damaged")
        broken.chmod(0o600)
        self.assertEqual(restore_smoke.main([str(broken)]), 1)
        self.assertFalse((self.original_home / "other-state").exists())


if __name__ == "__main__":
    unittest.main()
