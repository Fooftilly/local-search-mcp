"""Offline backup, staged restore and corruption rejection regressions."""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
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
