"""Offline backup, staged restore and corruption rejection regressions."""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
from unittest.mock import patch
import sqlite3
import stat
import tempfile
import unittest
import warnings
import zipfile

from scripts import backup_state as backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.archive = self.base / "state-backup.zip"
        self.db = self.state / "domains.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE domains (domain TEXT, status TEXT)")
            conn.execute("INSERT INTO domains VALUES ('example.org', 'allowed')")
            conn.commit()
        self.db.chmod(0o600)
        self.history = self.state / "history.jsonl"
        self.history.write_text('{"kind":"search","query":"private"}\n')
        self.history.chmod(0o600)

    def test_backup_verify_restore_roundtrip_and_private_permissions(self):
        backup.backup(self.archive, state_dir=self.state)
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        content = backup.verified_payload(self.archive)
        self.assertEqual(set(content), {"domains.sqlite3", "history.jsonl"})
        self.assertIn(b"private", content["history.jsonl"])
        staged = self.base / "restored"
        backup.restore(self.archive, staged)
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((staged / "domains.sqlite3").stat().st_mode), 0o600)
        self.assertEqual((staged / "history.jsonl").read_bytes(), self.history.read_bytes())
        with closing(sqlite3.connect(staged / "domains.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM domains").fetchone()[0],
                             "allowed")
        self.assertTrue(self.db.exists())

    def test_secret_files_are_excluded_and_no_clobber_is_allowed(self):
        secret = self.state / "config.json"
        secret.write_text('{"api_token":"NEVER-EXPORT-ME"}')
        secret.chmod(0o600)
        backup.backup(self.archive, state_dir=self.state)
        self.assertNotIn(b"NEVER-EXPORT-ME", self.archive.read_bytes())
        with zipfile.ZipFile(self.archive) as z:
            self.assertNotIn("config.json", z.namelist())
        original = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            backup.backup(self.archive, state_dir=self.state)
        self.assertEqual(original, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        restored = self.base / "destination"
        restored.mkdir()
        with self.assertRaises(ValueError):
            backup.restore(self.archive, restored)
        self.assertEqual(list(restored.iterdir()), [])

    def test_corrupt_hash_cannot_create_restoration(self):
        backup.backup(self.archive, state_dir=self.state)
        forged = self.base / "forged.zip"
        with zipfile.ZipFile(self.archive) as original, zipfile.ZipFile(forged, "w") as dst:
            for name in original.namelist():
                data = original.read(name)
                if name == "history.jsonl":
                    data = b"altered\n"
                dst.writestr(name, data)
        forged.chmod(0o600)
        target = self.base / "should-not-exist"
        with self.assertRaisesRegex(ValueError, "Integrity mismatch"):
            backup.restore(forged, target)
        self.assertFalse(target.exists())

    def test_duplicate_and_traversal_members_fail(self):
        for entries in [
            [("manifest.json", b"{}"), ("../escape", b"malice")],
            [("manifest.json", b"{}"), ("manifest.json", b"{}")],
        ]:
            with self.subTest(entries=entries):
                forged = self.base / "bad.zip"
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Duplicate name:.*",
                                            category=UserWarning)
                    with zipfile.ZipFile(forged, "w") as dst:
                        for name, data in entries:
                            dst.writestr(name, data)
                forged.chmod(0o600)
                with self.assertRaises(ValueError):
                    backup.verified_payload(forged)
                forged.unlink()

    def test_symlink_sources_and_unsafe_permissions_fail(self):
        external = self.base / "private.txt"
        external.write_text("secret")
        self.history.unlink()
        self.history.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())
        self.history.unlink()
        self.history.write_text("{}\n")
        self.history.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private owned"):
            backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())

    def test_wal_database_snapshot_includes_committed_changes(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO domains VALUES ('another.example', 'denied')")
            conn.commit()
            backup.backup(self.archive, state_dir=self.state)
            staged = self.base / "wal-restored"
            backup.restore(self.archive, staged)
        with closing(sqlite3.connect(staged / "domains.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM domains").fetchone()[0], 2)

    def test_custom_history_path_uses_override_not_default(self):
        elsewhere = self.base / "private-history"
        elsewhere.mkdir(mode=0o700)
        custom = elsewhere / "events.private.jsonl"
        custom.write_bytes(b'{"query":"actual-custom-history"}\n')
        custom.chmod(0o600)
        rotated = elsewhere / "events.private.jsonl.1"
        rotated.write_bytes(b'{"query":"older-custom-history"}\n')
        rotated.chmod(0o600)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": str(custom)}):
            backup.backup(self.archive, state_dir=self.state)
        stored = backup.verified_payload(self.archive)
        self.assertIn(b"actual-custom-history", stored["history.jsonl"])
        self.assertIn(b"older-custom-history", stored["history.jsonl.1"])
        self.assertNotIn(b"private", stored["history.jsonl"])
        with zipfile.ZipFile(self.archive) as container:
            self.assertFalse(any("events.private.jsonl" in name for name in
                                 container.namelist()))
        staged = self.base / "custom-staged"
        backup.restore(self.archive, staged)
        self.assertEqual((staged / "history.jsonl").read_bytes(),
                         custom.read_bytes())

    def test_missing_configured_history_fails_instead_of_silent_db_only_backup(self):
        outside = self.base / "private-history"
        outside.mkdir(mode=0o700)
        missing = outside / "not-created.jsonl"
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": str(missing)}):
            with self.assertRaisesRegex(ValueError, "Configured history file is missing"):
                backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())

    def test_configured_history_must_be_private_and_absolute(self):
        outside = self.base / "history-readable"
        outside.mkdir(mode=0o755)
        current = outside / "events.jsonl"
        current.write_text("secret history")
        current.chmod(0o600)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": str(current)}):
            with self.assertRaisesRegex(ValueError, "privately owned"):
                backup.backup(self.archive, state_dir=self.state)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": "relative/events.jsonl"}):
            with self.assertRaisesRegex(ValueError, "must be absolute"):
                backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())

    def test_rotated_history_over_db_limit_is_valid_for_backup_and_verify(self):
        # Reduced test constants exercise multi-generation writer-sized data
        # without consuming hundreds of megabytes in the CI matrix.
        db_cap = 16 * 1024
        history_cap = 6000
        for name in backup.HISTORY_NAMES:
            source = self.state / name
            source.write_bytes(os.urandom(5200))
            source.chmod(0o600)
        with (patch.object(backup, "MAX_DB_FILE", db_cap),
              patch.object(backup, "MAX_HISTORY_FILE", history_cap),
              patch.object(backup, "MAX_TOTAL", db_cap + 4 * history_cap),
              patch.object(backup, "MAX_ARCHIVE", db_cap + 4 * history_cap + 4096)):
            backup.backup(self.archive, state_dir=self.state)
            self.assertGreater(self.archive.stat().st_size, db_cap)
            verified = backup.verified_payload(self.archive)
            self.assertEqual(len(verified), 5)
            self.assertTrue(all(len(verified[name]) == 5200
                                for name in backup.HISTORY_NAMES))
            restored = self.base / "large-staged"
            backup.restore(self.archive, restored)
            self.assertEqual((restored / "history.jsonl.3").stat().st_size, 5200)
        self.assertGreaterEqual(backup.MAX_HISTORY_FILE, 100_000_000)
        self.assertGreaterEqual(backup.MAX_TOTAL,
                                backup.MAX_DB_FILE + 4 * 100_000_000)

    def test_cli_backup_verify_restore_and_refusal_of_existing_target(self):
        self.assertEqual(backup.main(["backup", str(self.archive),
                                      "--state-dir", str(self.state)]), 0)
        self.assertEqual(backup.main(["verify", str(self.archive)]), 0)
        self.assertEqual(backup.main(["restore", str(self.archive), "--target-dir",
                                      str(self.base / "staged")]), 0)
        self.assertEqual(backup.main(["restore", str(self.archive), "--target-dir",
                                      str(self.base / "staged")]), 1)


if __name__ == "__main__":
    unittest.main()
