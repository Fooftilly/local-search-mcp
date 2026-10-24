"""Offline rollback candidate preparation; tests never change live worktrees."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import backup_state
from scripts import rollback_candidate as candidate


class RollbackCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Rollback Test")
        self.git("config", "user.email", "rollback@example.invalid")
        (self.root / "bridge.py").write_text("old behavior\n")
        (self.root / "requirements.txt").write_text("httpx>=0.28,<1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.target = self.git("rev-parse", "HEAD")
        (self.root / "bridge.py").write_text("new behavior\n")
        self.git("add", ".")
        self.git("commit", "-qm", "current")
        self.current = self.git("rev-parse", "HEAD")
        self.state = self.base / "data"
        self.state.mkdir(mode=0o700)
        with closing(sqlite3.connect(self.state / "domains.sqlite3")) as db:
            db.execute("CREATE TABLE domains (domain TEXT, status TEXT)")
            db.execute("INSERT INTO domains VALUES ('example.org','allowed')")
            db.commit()
        (self.state / "domains.sqlite3").chmod(0o600)
        self.archive = self.base / "backup.zip"
        backup_state.backup(self.archive, state_dir=self.state)
        self.dir = self.base / "rollback-staged"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_stage_only_local_source_and_verified_private_state(self):
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            report = candidate.prepare(self.target, self.archive, self.dir,
                                       root=self.root)
        self.assertEqual(report["source_commit"], self.current)
        self.assertEqual(report["target_commit"], self.target)
        self.assertFalse(report["activation_performed"])
        self.assertFalse(report["ready_for_live_use"])
        self.assertEqual((self.dir / "source" / "bridge.py").read_text(),
                         "old behavior\n")
        self.assertEqual((self.root / "bridge.py").read_text(), "new behavior\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)
        self.assertTrue((self.dir / "state" / "domains.sqlite3").exists())
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(
            (self.dir / "state" / "domains.sqlite3").stat().st_mode), 0o600)
        self.assertEqual(json.loads((self.dir / "candidate.json").read_text()), report)

    def test_failure_cleans_new_candidate_without_touching_checkout(self):
        with (patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)),
              patch.object(candidate, "_copy_git_blobs",
                           side_effect=ValueError("invalid tracked source"))):
            with self.assertRaisesRegex(ValueError, "invalid tracked source"):
                candidate.prepare(self.target, self.archive, self.dir,
                                  root=self.root)
        self.assertFalse(self.dir.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)
        self.assertEqual((self.root / "bridge.py").read_text(), "new behavior\n")

    def test_refuses_existing_destination_and_wrong_permissions(self):
        self.dir.mkdir()
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            with self.assertRaisesRegex(ValueError, "must not exist"):
                candidate.prepare(self.target, self.archive, self.dir,
                                  root=self.root)
        self.assertTrue(self.dir.exists())
        self.dir.rmdir()
        self.base.chmod(0o755)
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            with self.assertRaisesRegex(ValueError, "private"):
                candidate.prepare(self.target, self.archive, self.dir,
                                  root=self.root)
        self.assertFalse(self.dir.exists())
        self.base.chmod(0o700)

    def test_rejects_gitlink_symlink_and_unsafe_names(self):
        code = self.base / "output"
        code.mkdir()
        for name in ("../escape", "/absolute", "safe/../../escape",
                     "double//separator", "back\\slash"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                candidate._valid_member(name)
        with patch.object(candidate, "_git",
                          return_value=b"120000 blob " + (b"a" * 40) + b"\tlink\0"):
            with self.assertRaisesRegex(ValueError, "unsupported tracked file type"):
                candidate._copy_git_blobs(self.root, self.target, code)
        self.assertEqual(list(code.rglob("*")), [])

    def test_local_smudge_filter_never_executes_during_staging(self):
        marker = self.base / "filter-triggered"
        command = self.base / "smudge.sh"
        command.write_text("#!/bin/sh\nprintf 'ran' > " + str(marker) + "\ncat\n")
        command.chmod(0o700)
        attributes = self.base / "global-attributes"
        attributes.write_text("*.py filter=unsafe\n")
        self.git("config", "core.attributesFile", str(attributes))
        self.git("config", "filter.unsafe.smudge", str(command))
        with patch.object(candidate.restore_smoke, "smoke", return_value=(1, 0)):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        self.assertFalse(marker.exists(), "Raw blob staging executed a smudge command")
        self.assertEqual((self.dir / "source" / "bridge.py").read_text(),
                         "old behavior\n")

    def test_cli_never_applies_unconfirmed_rollback(self):
        with patch.object(candidate, "prepare",
                          side_effect=AssertionError("Do not stage")):
            self.assertEqual(candidate.main([
                "--target", self.target, "--archive", str(self.archive),
                "--destination", str(self.dir)]), 1)
        self.assertFalse(self.dir.exists())

    def test_unavailable_bad_git_or_corrupt_backup_cannot_stage(self):
        bad_targets = (self.current, self.target[:8], "f" * 40)
        for target in bad_targets:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    candidate.prepare(target, self.archive, self.dir, root=self.root)
                self.assertFalse(self.dir.exists())
        self.archive.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            candidate.prepare(self.target, self.archive, self.dir, root=self.root)
        self.assertFalse(self.dir.exists())


if __name__ == "__main__":
    unittest.main()
