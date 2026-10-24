"""Offline rollback preflight regression tests using private temporary Git state."""
from __future__ import annotations

from contextlib import closing, redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import shlex
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
                self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
                self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.fsmonitor")
                self.assertEqual(env["GIT_CONFIG_VALUE_0"], "false")
                self.assertNotIn("GIT_CONFIG_PARAMETERS", env)
                commands.append(tuple(args))
            return real_run(args, **kwargs)
        with patch.object(rollback.subprocess, "run", side_effect=spy):
            rollback.preflight(self.target, self.archive, root=self.root)
        self.assertTrue(any("merge-base" in x for x in commands))
        self.assertTrue(any("cat-file" in x for x in commands))
        self.assertFalse(any("status" in x or "diff-files" in x for x in commands))
        self.assertTrue(any("diff-index" in x for x in commands))
        self.assertTrue(any("ls-files" in x and "--debug" in x for x in commands))
        self.assertTrue(any("ls-files" in x and "--others" in x for x in commands))
        self.assertTrue(any("diff" in x for x in commands))

    def test_fsmonitor_hook_cannot_run_during_offline_preflight(self):
        marker = Path(self.tmp.name) / "fsmonitor-invoked"
        hook = Path(self.tmp.name) / "fsmonitor-hook"
        hook.write_text(
            "#!/bin/sh\nprintf 'executed' > " + shlex.quote(str(marker)) + "\n"
        )
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        # Demonstrate that Git would execute this hook without the override.
        self.git("status", "--porcelain=v1")
        self.assertTrue(marker.exists(), "Fixture must exercise an active hook")
        marker.unlink()
        env_override = "'core.fsmonitor=" + str(hook) + "'"
        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": env_override}):
            result = rollback.preflight(self.target, self.archive, root=self.root)
        self.assertIn("Offline rollback preflight passed", result)
        self.assertFalse(marker.exists(),
                         "Read-only preflight must not invoke configured hooks")

    def test_rejects_active_git_bisect_without_changes(self):
        self.git("bisect", "start")
        try:
            self.assertTrue(update.busy(self.root, read_only=True))
            with self.assertRaisesRegex(ValueError, "Git operation in progress"):
                rollback.preflight(self.target, self.archive, root=self.root)
        finally:
            self.git("bisect", "reset")

    def test_git_index_stat_uses_unsigned_32bit_fields(self):
        from types import SimpleNamespace

        original = (self.root / "bridge.py").lstat()
        shift = 1 << 32
        fake = SimpleNamespace(
            st_ctime_ns=original.st_ctime_ns + shift * 1_000_000_000,
            st_mtime_ns=original.st_mtime_ns + shift * 1_000_000_000,
            st_dev=original.st_dev + shift,
            st_ino=original.st_ino + shift,
            st_uid=original.st_uid + shift,
            st_gid=original.st_gid + shift,
            st_size=original.st_size + shift,
        )
        # Git index stores all these fields modulo 2**32, so a wider
        # filesystem stat must compare equal to the same index entry.
        self.assertEqual(update._git_index_stat(fake),
                         update._git_index_stat(original))
        self.assertFalse(update._tracked_metadata_dirty(self.root))
        real_lstat = Path.lstat

        def large_inode(path, *args, **kwargs):
            if path == self.root / "bridge.py":
                return fake
            return real_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", autospec=True, side_effect=large_inode):
            self.assertFalse(update._tracked_metadata_dirty(self.root))
            result = rollback.preflight(self.target, self.archive, root=self.root)
        self.assertIn("Offline rollback preflight passed", result)

    def test_git_index_size_exact_multiple_of_4gib(self):
        from types import SimpleNamespace
        original = (self.root / "bridge.py").lstat()
        empty = SimpleNamespace(
            st_ctime_ns=original.st_ctime_ns,
            st_mtime_ns=original.st_mtime_ns,
            st_dev=original.st_dev,
            st_ino=original.st_ino,
            st_uid=original.st_uid,
            st_gid=original.st_gid,
            st_size=0,
        )
        sparse = SimpleNamespace(
            st_ctime_ns=original.st_ctime_ns,
            st_mtime_ns=original.st_mtime_ns,
            st_dev=original.st_dev,
            st_ino=original.st_ino,
            st_uid=original.st_uid,
            st_gid=original.st_gid,
            st_size=1 << 32,
        )
        self.assertEqual(update._git_index_stat(empty)[-1], 0)
        self.assertEqual(update._git_index_stat(sparse)[-1], 0x80000000)

    def test_rejects_submodule_gitlinks_without_running_submodule_code(self):
        remote = Path(self.tmp.name) / "submodule-source"
        remote.mkdir()
        def sub_git(*args):
            return subprocess.run(["git", *args], cwd=remote,
                                  capture_output=True, text=True, check=True).stdout
        sub_git("init", "-q")
        sub_git("config", "user.email", "fixture@example.invalid")
        sub_git("config", "user.name", "Fixture")
        (remote / "file.txt").write_text("nested repository\n")
        sub_git("add", ".")
        sub_git("commit", "-qm", "submodule fixture")
        self.git("-c", "protocol.file.allow=always", "submodule", "add",
                 "-q", str(remote), "sm")
        self.commit("track submodule")
        self.assertTrue(update._tracked_metadata_dirty(
            self.root, include_untracked=True))
        with self.assertRaisesRegex(ValueError, "not clean"):
            rollback.preflight(self.target, self.archive, root=self.root)

    def test_clean_filter_never_executes_in_preflight(self):
        import shlex
        marker = Path(self.tmp.name) / "filter-ran"
        hook = Path(self.tmp.name) / "filter-clean.sh"
        hook.write_text("#!/bin/sh\nprintf 'executed' >> " +
                        shlex.quote(str(marker)) + "\ncat\n")
        hook.chmod(0o700)
        (self.root / ".gitattributes").write_text("*.txt filter=x\n")
        (self.root / "filtered.txt").write_text("unchanged\n")
        self.git("config", "filter.x.clean", str(hook))
        self.commit("configured clean filter")
        marker.unlink(missing_ok=True)
        (self.root / "filtered.txt").touch()
        # Git status really would execute the configured clean filter.
        self.git("status", "--porcelain=v1")
        self.assertTrue(marker.exists())
        marker.unlink()
        # Git status may refresh the index stat after the clean filter runs,
        # making the touched-but-unchanged file appear clean again. Recreate
        # a guaranteed metadata mismatch *after* that status probe.
        filtered = self.root / "filtered.txt"
        stat = filtered.stat()
        os.utime(filtered, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        self.assertTrue(update._tracked_metadata_dirty(self.root),
                        "Fixture must be dirty after Git status refresh")
        with self.assertRaisesRegex(ValueError, "not clean"):
            rollback.preflight(self.target, self.archive, root=self.root)
        self.assertFalse(marker.exists(), "preflight invoked a clean filter")

    def test_changes_during_archive_validation_fail_closed(self):
        original = backup_state.verified_payload
        def move_checkout(archive):
            (self.root / "bridge.py").write_text("print('race')\n")
            return original(archive)
        with patch.object(backup_state, "verified_payload", side_effect=move_checkout):
            with self.assertRaisesRegex(ValueError, "changed during preflight"):
                rollback.preflight(self.target, self.archive, root=self.root)

    def test_cli_uses_injected_checkout_and_leaves_it_unchanged(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(rollback, "ROOT", self.root), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(
                rollback.main(["--target", self.target,
                               "--archive", str(self.archive)]), 0,
                errors.getvalue(),
            )
        self.assertIn("Offline rollback preflight passed", output.getvalue())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)
        self.assertEqual(self.archive.read_bytes(), self.initial_archive)

    def test_cli_invalid_target_is_rejected(self):
        errors = io.StringIO()
        with patch.object(rollback, "ROOT", self.root), redirect_stderr(errors):
            self.assertEqual(
                rollback.main(["--target", "invalid",
                               "--archive", str(self.archive)]), 1,
            )
        self.assertIn("exact lowercase full Git commit SHA", errors.getvalue())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.current)


if __name__ == "__main__":
    unittest.main()
