"""Offline rollback preflight regression tests using private temporary Git state."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import backup_state, rollback_preflight as rollback
from scripts import restart_on_update as update


class RollbackPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "checkout"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Preflight Test")
        self.git("config", "user.email", "preflight@example.invalid")
        (self.root / "bridge.py").write_text("print('old')\n")
        (self.root / "requirements.txt").write_text("httpx>=0.28,<1\n")
        self.commit("old runtime")
        self.target = self.git("rev-parse", "HEAD")
        (self.root / "bridge.py").write_text("print('new')\n")
        self.commit("new runtime")
        self.current = self.git("rev-parse", "HEAD")
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir(mode=0o700)
        with closing(sqlite3.connect(self.state / "domains.sqlite3")) as db:
            db.execute("CREATE TABLE domains (domain TEXT, status TEXT)")
            db.commit()
        (self.state / "domains.sqlite3").chmod(0o600)
        self.archive = Path(self.tmp.name) / "state.zip"
        backup_state.backup(self.archive, state_dir=self.state)
        self.initial_archive = self.archive.read_bytes()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, title):
        self.git("add", "--all")
        self.git("commit", "-qm", title)

    def test_positive_offline_read_only_preflight(self):
        before = self.current
        with patch.object(update, "restart", side_effect=AssertionError("restart")), \
             patch.object(update, "write_state", side_effect=AssertionError("write")), \
             patch.object(update, "active", side_effect=AssertionError("systemctl")):
            result = rollback.preflight(self.target, self.archive, root=self.root)
        self.assertIn("Offline rollback preflight passed", result)
        self.assertIn("1 runtime file(s)", result)
        self.assertIn("does NOT prove target dependencies", result)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.archive.read_bytes(), self.initial_archive)

    def test_rejects_invalid_and_ambiguous_commit_inputs(self):
        for target in (self.target[:8], "--help", "A" * 40, "g" * 40, "", self.current):
            with self.subTest(target=target), self.assertRaises(ValueError):
                rollback.preflight(target, self.archive, root=self.root)

    def test_refuses_non_ancestor_or_unavailable_commits(self):
        with self.assertRaisesRegex(ValueError, "unavailable locally"):
            rollback.preflight("f" * 40, self.archive, root=self.root)
        self.git("checkout", "-qb", "side", self.target)
        (self.root / "bridge.py").write_text("print('side')\n")
        self.commit("unrelated branch tip")
        side = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-")
        with self.assertRaisesRegex(ValueError, "not an ancestor"):
            rollback.preflight(side, self.archive, root=self.root)

    def test_rejects_staged_unstaged_and_untracked_content(self):
        cases = ("unstaged", "staged", "untracked")
        for case in cases:
            with self.subTest(case=case):
                if case == "untracked":
                    item = self.root / "untracked.txt"
                else:
                    item = self.root / "bridge.py"
                item.write_text(case + "\n")
                if case == "staged":
                    self.git("add", "bridge.py")
                with self.assertRaisesRegex(ValueError, "not clean"):
                    rollback.preflight(self.target, self.archive, root=self.root)
                self.git("reset", "--hard", "-q", "HEAD")
                if case == "untracked":
                    item.unlink()

    def test_corrupt_and_permissive_backup_fail_closed(self):
        self.archive.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            rollback.preflight(self.target, self.archive, root=self.root)
        self.archive.write_bytes(self.initial_archive)
        self.archive.chmod(0o644)
        with self.assertRaises(ValueError):
            rollback.preflight(self.target, self.archive, root=self.root)
        self.archive.chmod(0o600)
        with self.assertRaises(ValueError):
            rollback.preflight(self.target, self.archive.with_name("absent.zip"),
                               root=self.root)

    def test_every_git_subprocess_disables_lazy_fetch_and_index_writes(self):
        real_run = subprocess.run
        commands = []
        def spy(args, **kwargs):
            if args and args[0] == "git" and kwargs.get("cwd") == self.root:
                env = kwargs.get("env")
                self.assertIsNotNone(env, args)
                self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")
                self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
                self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
                commands.append(tuple(args))
            return real_run(args, **kwargs)
        with patch.object(rollback.subprocess, "run", side_effect=spy):
            rollback.preflight(self.target, self.archive, root=self.root)
        self.assertTrue(any("merge-base" in x for x in commands))
        self.assertTrue(any("cat-file" in x for x in commands))
        self.assertTrue(any("status" in x for x in commands))
        self.assertTrue(any("diff" in x for x in commands))
        self.assertTrue(any("ls-files" in x for x in commands))

    def test_changes_during_archive_validation_fail_closed(self):
        original = backup_state.verified_payload
        def move_checkout(archive):
            (self.root / "bridge.py").write_text("print('race')\n")
            return original(archive)
        with patch.object(backup_state, "verified_payload", side_effect=move_checkout):
            with self.assertRaisesRegex(ValueError, "changed during preflight"):
                rollback.preflight(self.target, self.archive, root=self.root)

    def test_cli_failure_leaves_checkout_unchanged(self):
        with patch.object(rollback, "ROOT", self.root):
            # Explicit root for direct function tests; the installed CLI resolves
            # its own checkout and cannot take a remote worktree path.
            self.assertEqual(rollback.main(["--target", "invalid",
                                            "--archive", str(self.archive)]), 1)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)


if __name__ == "__main__":
    unittest.main()
