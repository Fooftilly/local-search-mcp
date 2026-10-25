"""Read-only local Git status independent of the retired update watcher."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_preflight


class LocalUpdatePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        def run(*args):
            return subprocess.run(["git", *args], cwd=self.root,
                                  capture_output=True, check=True,
                                  text=True).stdout.strip()
        self.git = run
        run("init", "-q")
        run("config", "user.name", "Test")
        run("config", "user.email", "test@example.invalid")
        (self.root / "note.txt").write_text("test\n")
        run("add", "note.txt")
        run("commit", "-qm", "initial")
        self.sha = run("rev-parse", "HEAD")

    def test_revision_is_read_only_and_never_uses_watcher_baseline(self):
        self.assertEqual(update_preflight.local_revision(self.root), self.sha)
        with patch.object(update_preflight, "ROOT", self.root):
            result = update_preflight.report()
        self.assertEqual(result["state"], "inspected")
        self.assertEqual(result["local_revision"], self.sha[:12])
        self.assertFalse(result["remote_checked"])
        self.assertFalse(result["updates_applied"])
        self.assertIn("./setup.sh --update", result["next_step"])
        self.assertFalse((self.root / ".local-search-update-state").exists())

    def test_unavailable_git_is_redacted(self):
        with patch.object(update_preflight, "ROOT", self.root / "missing"):
            result = update_preflight.report()
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn(str(self.root), str(result))

    def test_no_commands_other_than_read_only_git_revision(self):
        calls = []
        original = update_preflight.subprocess.run
        def inspected(args, **kwargs):
            calls.append(args)
            self.assertEqual(args[0], "git")
            self.assertNotIn("fetch", args)
            self.assertNotIn("status", args)
            self.assertNotIn("reset", args)
            self.assertNotIn("checkout", args)
            self.assertNotIn("merge", args)
            self.assertEqual(kwargs.get("timeout"), 10)
            self.assertTrue(kwargs["capture_output"])
            self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
            return original(args, **kwargs)
        with patch.object(update_preflight.subprocess, "run", side_effect=inspected):
            self.assertEqual(update_preflight.local_revision(self.root), self.sha)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
