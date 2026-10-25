"""Focused offline tests for one operator-attended update, not host mutation."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import attended_update as app
from scripts import setup_wizard


class AttendedUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        python = self.root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("test")
        self.root_patch = patch.object(app, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.old = "a" * 40
        self.new = "b" * 40
        self.units = {name: False for name in app.UNITS}
        self.units[app.BRIDGE] = True
        self.units[app.TUNNEL] = True
        self.units[app.HEALTH] = True
        self.stopped = {name: False for name in app.UNITS}
        self.target = self.root / "backup.zip"
        self.privileged = patch.object(app.os, "geteuid", return_value=1000)
        self.privileged.start()
        self.addCleanup(self.privileged.stop)
        tty = Mock()
        tty.isatty.return_value = True
        self.stdin = patch.object(app.sys, "stdin", tty)
        self.stdin.start()
        self.addCleanup(self.stdin.stop)

    def git_result(self, *args, **kwargs):
        if args[0] == "fetch":
            return ""
        if args[:2] == ("rev-parse", "--verify"):
            return self.new
        if args[:2] == ("rev-parse", "FETCH_HEAD"):
            return self.new
        if args[:2] == ("rev-parse", "HEAD"):
            return self.new
        if args[0] == "merge-base":
            return ""
        if args[0] == "rev-list":
            return "2"
        if args[0] == "merge":
            return ""
        raise AssertionError(args)

    def run_case(self, *, answers=(True, True), failure=None):
        calls = []
        def local_command(args, **kwargs):
            calls.append(tuple(args))
            if failure and failure(args):
                raise app.UpdateError("Simulated failure")
            return ""
        with (patch.object(app, "checkout", return_value=self.old),
              patch.object(app, "approved_units",
                           side_effect=[self.units, self.units, self.stopped]),
              patch.object(app, "git", side_effect=self.git_result) as git,
              patch.object(app, "confirmation", side_effect=answers),
              patch.object(app, "command", side_effect=local_command),
              patch.object(app, "backup_path", return_value=self.target),
              patch.object(app.backup_state, "backup") as backup,
              patch.object(app.backup_state, "verified_payload") as verified,
              patch.object(app, "listener_ready", return_value=True),
              redirect_stdout(io.StringIO()),
              redirect_stderr(io.StringIO())):
            status = app.main()
        return status, calls, git, backup, verified

    def test_attended_update_stops_owned_services_backs_up_and_restarts(self):
        status, calls, git, backup, verified = self.run_case()
        self.assertEqual(status, 0)
        backup.assert_called_once_with(self.target)
        verified.assert_called_once_with(self.target)
        def step(needle):
            return next(i for i, args in enumerate(calls) if needle in args)
        self.assertLess(step("disable"), step("stop"))
        self.assertLess(step("stop"), step("install"))
        self.assertLess(step("install"), step("start"))
        self.assertTrue(any(args[-1] == app.BRIDGE and "start" in args for args in calls))
        self.assertTrue(any(args[-1] == app.TUNNEL and "start" in args for args in calls))
        self.assertFalse(any("docker" in args for args in calls))
        self.assertFalse(any("sudo" in args for args in calls))
        self.assertFalse(any("local-search-mcp-crawler-egress.service" in args
                             for args in calls))
        self.assertEqual(sum(x.args[0] == "merge" for x in git.call_args_list), 1)

    def test_abort_before_any_network_access_without_fetch_confirmation(self):
        with (patch.object(app, "checkout", return_value=self.old),
              patch.object(app, "approved_units", return_value=self.units),
              patch.object(app, "confirmation", return_value=False),
              patch.object(app, "git", side_effect=AssertionError("network must not run")),
              patch.object(app, "command", side_effect=AssertionError("no mutations")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(app.update(), 0)

    def test_cancel_after_fetch_never_stops_services_or_backs_up(self):
        status, calls, git, backup, verified = self.run_case(answers=(True, False))
        self.assertEqual(status, 0)
        self.assertEqual(calls, [])
        backup.assert_not_called()
        verified.assert_not_called()
        self.assertTrue(any(x.args[0] == "fetch" for x in git.call_args_list))
        self.assertFalse(any(x.args[0] == "merge" for x in git.call_args_list))

    def test_failure_after_merge_never_auto_starts_partial_update(self):
        status, calls, _, _, _ = self.run_case(
            failure=lambda args: "pip" in args)
        self.assertEqual(status, 1)
        self.assertFalse(any("start" in args for args in calls))
        self.assertTrue(any("stop" in args for args in calls))

    def test_pre_merge_failure_can_restore_original_services(self):
        with patch.object(app.backup_state, "backup",
                          side_effect=ValueError("private policy secret")):
            # Need the actual backup function exception (rather than the
            # standard happy-path mock in run_case).
            with (patch.object(app, "checkout", return_value=self.old),
                  patch.object(app, "approved_units",
                               side_effect=[self.units, self.units, self.stopped]),
                  patch.object(app, "git", side_effect=self.git_result),
                  patch.object(app, "confirmation", side_effect=[True, True]),
                  patch.object(app, "backup_path", return_value=self.target),
                  patch.object(app, "command", return_value="") as command,
                  patch.object(app, "listener_ready", return_value=True),
                  redirect_stdout(io.StringIO()),
                  redirect_stderr(io.StringIO()) as errors):
                status = app.main()
        self.assertEqual(status, 1)
        self.assertNotIn("private policy secret", errors.getvalue())
        self.assertTrue(any("start" in x.args[0] for x in command.call_args_list))

    def test_no_noninteractive_or_root_updates(self):
        with patch.object(app, "checkout", side_effect=AssertionError("do not read Git")):
            app.sys.stdin.isatty.return_value = False
            with redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(), 1)
        app.sys.stdin.isatty.return_value = True
        with patch.object(app.os, "geteuid", return_value=0):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(), 1)

    def test_foreign_unit_fails_before_any_mutation(self):
        folder = self.root / "config" / "systemd" / "user"
        folder.mkdir(parents=True)
        unit = folder / app.WATCHERS[0]
        unit.write_text("# An unrelated user's unit\n")
        with (patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}),
              patch.object(app, "command", return_value=(
                  "LoadState=loaded\nActiveState=active\nFragmentPath=" +
                  str(unit))) as execute):
            with self.assertRaisesRegex(app.UpdateError, "Unknown or unverified"):
                app.approved_units()
        self.assertEqual(execute.call_count, 1)

    def test_git_checkout_is_real_and_refuses_untrusted_remote(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, text=True,
                                  capture_output=True, check=True).stdout.strip()
        git("init", "-q", "-b", "master")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (self.root / "note").write_text("initial\n")
        git("add", "note")
        git("commit", "-qm", "initial")
        git("remote", "add", "origin", "https://example.invalid/untrusted")
        with self.assertRaisesRegex(app.UpdateError, "Origin"):
            app.checkout()
        git("remote", "set-url", "origin",
            "https://github.com/Fooftilly/local-search-mcp.git")
        self.assertEqual(app.checkout(), git("rev-parse", "HEAD"))
        (self.root / "note").write_text("dirty")
        with self.assertRaisesRegex(app.UpdateError, "contains changes"):
            app.checkout()


class WizardEntryTests(unittest.TestCase):
    def test_cli_routes_to_one_attended_entrypoint(self):
        with (patch.object(setup_wizard.os, "geteuid", return_value=1000),
              patch.object(setup_wizard, "run_existing", return_value=0) as run):
            self.assertEqual(setup_wizard.main(["--update"]), 0)
        run.assert_called_once_with("scripts/attended_update.py")

    def test_help_and_other_status_options_are_still_read_only(self):
        with (patch.object(setup_wizard, "run_existing",
                           side_effect=AssertionError("must be explicit")),
              redirect_stdout(io.StringIO()) as output):
            with self.assertRaises(SystemExit) as result:
                setup_wizard.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--update", output.getvalue())


if __name__ == "__main__":
    unittest.main()
