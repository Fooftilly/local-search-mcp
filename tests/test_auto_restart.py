"""Regression tests for opt-in offline Git-driven systemd restart checks."""
from __future__ import annotations

from pathlib import Path
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import restart_on_update as update
from scripts import install_user_services as installer
from scripts.install_user_services import unit_files


class AutoRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "checkout"
        self.root.mkdir()
        self.state = Path(self.tmp.name) / "state" / "last-service-commit"
        self.git("init", "-q")
        self.git("config", "user.name", "Unit Test")
        self.git("config", "user.email", "unit-test@example.invalid")
        self.change("bridge.py", "print('initial')\n")
        self.change("requirements.txt", "fastmcp>=4,<5\n")
        self.commit("initial")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True,
                              capture_output=True, check=True).stdout.strip()

    def change(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)

    def check(self, *, active=(True, True), dependencies_ready=False):
        calls = []
        with patch.object(update, "active", side_effect=active) as act, \
             patch.object(update, "restart", side_effect=calls.append):
            result = update.apply(self.root, path=self.state, settle_seconds=0,
                                  dependencies_ready=dependencies_ready)
        return result, calls, act.call_count

    def test_opt_in_units_and_literal_safe_paths(self):
        root = Path("/tmp/checkout % has spaces")
        normal = unit_files("/usr/bin/python3", root)
        self.assertEqual(len(normal), 4)
        extra = unit_files("/usr/bin/python3", root, auto_restart=True,
                           reflog_path=Path("/tmp/checkout % has spaces/.git/logs/HEAD"))
        self.assertEqual(len(extra), 7)
        self.assertIn("PathChanged=", extra["local-search-mcp-update.path"])
        self.assertIn("%%", extra["local-search-mcp-update.path"])
        self.assertIn("OnUnitActiveSec=1min", extra["local-search-mcp-update.timer"])
        self.assertIn("NoNewPrivileges=true", extra["local-search-mcp-update.service"])
        self.assertIn("--state-file", extra["local-search-mcp-update.service"])
        self.assertNotIn("git pull", "".join(extra.values()))

    def test_systemd_escaped_path_requires_journal_manual_argument(self):
        # Raw systemd ExecStart text requires three distinct escaping rules:
        # literal %, backslash, and double quote. Never copy it as a CLI path.
        state = (Path(self.tmp.name) /
                 'xdg%state "quoted" \\ with spaces' / "last-service-commit")
        units = unit_files(
            "/usr/bin/python3", self.root, auto_restart=True,
            reflog_path=self.root / ".git/logs/HEAD", state_path=state,
        )
        raw = next(line.split("=", 1)[1] for line in
                   units["local-search-mcp-update.service"].splitlines()
                   if line.startswith("ExecStart="))
        self.assertIn("%%", raw)
        self.assertIn(r"\\", raw)
        self.assertIn(r'\"', raw)
        # The journal's explicit state_argument() returns a shell-ready token
        # that round-trips without systemd decoding or guessing the path.
        manual = update.state_argument(state)
        self.assertEqual(shlex.split(manual), ["--state-file", str(state)])
        self.assertEqual(manual, "--state-file " + shlex.quote(str(state)))
        self.assertNotEqual(shlex.split(raw)[-1], str(state))

    def test_install_pins_xdg_state_file_even_without_manager_environment(self):
        config_home = Path(self.tmp.name) / "xdg-config"
        custom_state_home = Path(self.tmp.name) / "xdg-state % custom"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home),
                                     "XDG_STATE_HOME": str(custom_state_home)}), \
             patch.object(installer, "ROOT", self.root), \
             patch.object(installer.os, "geteuid", return_value=1000):
            self.assertEqual(installer.main(["--install", "--auto-restart"]), 0)
            expected_state = update.state_file().absolute()
            self.assertTrue(expected_state.is_file())
            self.assertEqual(expected_state.read_text().strip(), update.head(self.root))
            unit = (config_home / "systemd/user/local-search-mcp-update.service").read_text()
            self.assertIn("--state-file", unit)
            self.assertIn(str(expected_state).replace("%", "%%"), unit)
        # No manager import of XDG_STATE_HOME: the installed unit remains pinned.
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "unrelated")}):
            self.assertNotEqual(update.state_file(), expected_state)
            with patch.object(update, "active", return_value=True), \
                 patch.object(update, "restart") as restart:
                result = update.apply(self.root, path=expected_state, settle_seconds=0)
            self.assertIn("No local commit change", result)
            restart.assert_not_called()

    def test_reset_baseline_rejects_missing_or_mistyped_state_file(self):
        # Do not falsely report recovery while the service still uses its
        # original baseline: a mistyped --state-file must never be created.
        update.write_state(self.state, "f" * 40)
        wrong_state = Path(self.tmp.name) / "wrong state" / "last-service-commit"
        with self.assertRaisesRegex(ValueError, "systemctl --user cat"):
            update.reset_baseline(update.head(self.root), self.root, path=wrong_state)
        self.assertFalse(wrong_state.exists())
        self.assertEqual(self.state.read_text().strip(), "f" * 40)
        result = update.apply(self.root, path=self.state, settle_seconds=0)
        self.assertIn("Previous commit unavailable", result)
        with self.assertRaisesRegex(ValueError, "No update baseline"):
            update.reset_baseline(update.head(self.root), self.root,
                                  path=Path(self.tmp.name) / "new-state")
        self.assertIn(
            "no services were restarted",
            update.reset_baseline(update.head(self.root), self.root, path=self.state),
        )

    def test_reset_baseline_needs_exact_reviewed_head_and_is_offline(self):
        update.write_state(self.state, "f" * 40)
        result, restarted, _ = self.check()
        self.assertIn("Previous commit unavailable", result)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text().strip(), "f" * 40)
        with self.assertRaisesRegex(ValueError, "exact current Git HEAD"):
            update.reset_baseline("a" * 40, self.root, path=self.state)
        with patch.object(update, "restart") as restart:
            response = update.reset_baseline(update.head(self.root), self.root, path=self.state)
            restart.assert_not_called()
        self.assertIn("no services were restarted", response)
        self.assertEqual(self.state.read_text().strip(), update.head(self.root))
        self.assertIn("No local commit change", self.check()[0])

    def test_reset_baseline_refuses_dirty_git_checkout(self):
        update.write_state(self.state, "f" * 40)
        self.change("bridge.py", "print('unreviewed')\\n")
        with self.assertRaisesRegex(ValueError, "Git operations or tracked runtime edits"):
            update.reset_baseline(update.head(self.root), self.root, path=self.state)
        self.assertEqual(self.state.read_text().strip(), "f" * 40)

    def test_baseline_is_idempotent_and_no_service_restart(self):
        update.initialize(self.root, path=self.state)
        before = self.state.read_text()
        update.initialize(self.root, path=self.state)
        self.assertEqual(before, self.state.read_text())
        result, restarted, _ = self.check()
        self.assertEqual(restarted, [])
        self.assertIn("No local commit change", result)

    def test_missing_baseline_never_restarts_current_commit(self):
        result, restarted, checked = self.check()
        self.assertIn("Initialized", result)
        self.assertEqual(restarted, [])
        self.assertEqual(checked, 0)

    def test_code_update_restarts_previously_active_services_in_order(self):
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('updated')\n")
        self.commit("fix runtime")
        result, restarted, checked = self.check()
        self.assertIn("Restart requested", result)
        self.assertEqual(restarted, list(update.SERVICES))
        self.assertEqual(checked, 2)
        self.assertEqual(self.state.read_text().strip(), update.head(self.root))
        self.assertEqual(self.check()[1], [])

    def test_docs_and_ci_only_do_not_restart(self):
        update.initialize(self.root, path=self.state)
        self.change("README.md", "New instructions\n")
        self.change(".github/workflows/test.yml", "name: Test\n")
        self.change("tests/test_misc.py", "print('test')\n")
        self.commit("docs tests CI")
        result, restarted, checked = self.check()
        self.assertIn("restart not required", result)
        self.assertEqual(restarted, [])
        self.assertEqual(checked, 0)
        self.assertEqual(self.state.read_text().strip(), update.head(self.root))

    def test_dependency_changes_require_explicit_manual_acknowledgement(self):
        update.initialize(self.root, path=self.state)
        original = self.state.read_text()
        self.change("requirements.txt", "fastmcp>=4.1,<5\n")
        self.commit("bump minimum version")
        result, restarted, checked = self.check()
        self.assertIn("Python requirements changed", result)
        self.assertEqual(restarted, [])
        self.assertEqual(checked, 0)
        self.assertEqual(self.state.read_text(), original)
        with patch.object(update, "requirements_ready", return_value=True):
            result, restarted, _ = self.check(dependencies_ready=True)
        self.assertIn("Restart requested", result)
        self.assertEqual(restarted, list(update.SERVICES))



    def test_deferred_dependency_message_contains_exact_pinned_state_path(self):
        # A custom install-time state path may not be the systemd user's default.
        special_state = Path(self.tmp.name) / "custom state 'saved'" / "last-service-commit"
        update.initialize(self.root, path=special_state)
        self.change("requirements.txt", "fastmcp>=4.1,<5\n")
        self.commit("change dependencies")
        response = update.apply(self.root, path=special_state, settle_seconds=0)
        self.assertIn("--state-file " + shlex.quote(str(special_state)), response)
        self.assertIn("--dependencies-ready", response)
        self.assertEqual(special_state.read_text().strip() != update.head(self.root), True)

    def test_manual_acknowledgement_rejects_wrong_empty_state_file(self):
        # Avoid silently initializing an unrelated baseline and returning 0.
        with self.assertRaisesRegex(ValueError, "No update baseline"):
            update.apply(self.root, path=self.state, dependencies_ready=True,
                         settle_seconds=0)
        self.assertFalse(self.state.exists())

    def test_unavailable_baseline_explains_exact_recovery_state_path(self):
        special_state = Path(self.tmp.name) / "custom state 'saved'" / "last-service-commit"
        update.write_state(special_state, "f" * 40)
        message = update.apply(self.root, path=special_state, settle_seconds=0)
        self.assertIn("Previous commit unavailable", message)
        self.assertIn("--state-file " + shlex.quote(str(special_state)), message)
        self.assertIn("--reset-baseline", message)

    def test_direct_url_needs_ack_and_accepts_installed_package_offline(self):
        update.initialize(self.root, path=self.state)
        old_state = self.state.read_text()
        self.change("requirements.txt",
                    "pip @ https://example.invalid/pip-1.0-py3-none-any.whl\\n")
        self.commit("use manually installed wheel URL")
        deferred, restarted, _ = self.check()
        self.assertIn("--dependencies-ready", deferred)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text(), old_state)

        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()  # requirements_ready checks existence only.
        actual_run = subprocess.run
        commands = []

        def offline_runner(command, **kwargs):
            commands.append(command)
            if "-c" in command:
                return actual_run([sys.executable, "-I", "-c", command[-1]],
                                  **kwargs)
            self.assertEqual(command[-3:], ["-m", "pip", "check"])
            return subprocess.CompletedProcess(command, 0)

        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(self.root))
        self.assertEqual(len(commands), 2)
        self.assertTrue(any("pip" in cmd and "check" in cmd for cmd in commands))

        # A URL entry cannot pass if the named distribution is absent locally.
        self.change("requirements.txt",
                    "definitely-absent-local-mcp-98371 @ "
                    "https://example.invalid/missing.whl\\n")
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(self.root))

    def test_dependency_acknowledgement_still_checks_local_versions(self):
        update.initialize(self.root, path=self.state)
        original = self.state.read_text()
        self.change("requirements.txt", "fastmcp>=4.1,<5\n")
        self.commit("bump minimum version")
        with patch.object(update, "requirements_ready", return_value=False):
            result, restarted, _ = self.check(dependencies_ready=True)
        self.assertIn("failed offline validation", result)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text(), original)

    def test_removed_requirements_manifest_needs_explicit_acknowledgement(self):
        update.initialize(self.root, path=self.state)
        original = self.state.read_text()
        (self.root / "requirements.txt").unlink()
        self.commit("remove requirements manifest")
        result, restarted, _ = self.check()
        self.assertIn("Python requirements changed", result)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text(), original)
        with patch.object(update, "requirements_ready", return_value=True):
            result, restarted, _ = self.check(dependencies_ready=True)
        self.assertIn("Restart requested", result)
        self.assertEqual(restarted, list(update.SERVICES))

    def test_renamed_requirements_manifest_needs_explicit_acknowledgement(self):
        update.initialize(self.root, path=self.state)
        (self.root / "config").mkdir()
        self.git("mv", "requirements.txt", "config/dependencies.txt")
        self.commit("move requirements manifest")
        result, restarted, _ = self.check()
        self.assertIn("Python requirements changed", result)
        self.assertEqual(restarted, [])
        with patch.object(update, "requirements_ready", return_value=True):
            result, restarted, _ = self.check(dependencies_ready=True)
        self.assertIn("Restart requested", result)
        self.assertEqual(restarted, list(update.SERVICES))

    def test_removed_manifest_offline_check_requires_valid_environment(self):
        # An explicit acknowledgment does not bypass virtualenv/pip integrity.
        (self.root / "requirements.txt").unlink()
        self.assertFalse(update.requirements_ready(self.root))
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/usr/bin/env python3\\n")
        success = subprocess.CompletedProcess([], 0)
        failure = subprocess.CompletedProcess([], 1)
        with patch.object(update.subprocess, "run", return_value=success) as probe:
            self.assertTrue(update.requirements_ready(self.root))
        self.assertIn("pip", probe.call_args.args[0])
        self.assertIn("check", probe.call_args.args[0])
        with patch.object(update.subprocess, "run", return_value=failure):
            self.assertFalse(update.requirements_ready(self.root))
        with patch.object(update.subprocess, "run", side_effect=OSError("no pip")):
            self.assertFalse(update.requirements_ready(self.root))

    def test_inactive_services_are_not_started(self):
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('new')\n")
        self.commit("change")
        result, restarted, _ = self.check(active=(False, False))
        self.assertIn("inactive", result)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text().strip(), update.head(self.root))

    def test_tunnel_only_does_not_start_inactive_bridge_as_dependency(self):
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('new')\n")
        self.commit("change")
        result, restarted, _ = self.check(active=(False, True))
        self.assertIn("restart deferred", result)
        self.assertEqual(restarted, [])
        self.assertNotEqual(self.state.read_text().strip(), update.head(self.root))
        # Starting the bridge later allows both services to be reconciled
        # without requiring a new commit or activating a stopped unit.
        result, restarted, _ = self.check(active=(True, True))
        self.assertIn("Restart requested", result)
        self.assertEqual(restarted, list(update.SERVICES))
        self.assertEqual(self.state.read_text().strip(), update.head(self.root))

    def test_committed_rename_from_runtime_into_docs_remains_relevant(self):
        update.initialize(self.root, path=self.state)
        (self.root / "docs").mkdir()
        self.git("mv", "bridge.py", "docs/bridge.md")
        self.commit("move runtime file to documentation")
        changed = update.changed_files(
            self.root, self.state.read_text().strip(), update.head(self.root))
        self.assertIn("bridge.py", changed)
        self.assertIn("docs/bridge.md", changed)
        self.assertTrue(any(update.affects_runtime(name) for name in changed))

    def test_staged_rename_does_not_bypass_dirty_runtime_guard(self):
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('updated')\\n")
        self.commit("new runtime")
        old_baseline = self.state.read_text()
        (self.root / "docs").mkdir()
        self.git("mv", "bridge.py", "docs/bridge.md")
        self.assertTrue(update.tracked_source_dirty(self.root))
        result, restarted, _ = self.check()
        self.assertIn("will retry", result)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text(), old_baseline)

    def test_dirty_runtime_source_defers_without_marking_commit(self):
        update.initialize(self.root, path=self.state)
        original = self.state.read_text()
        self.change("bridge.py", "print('new')\n")
        self.commit("change")
        self.change("bridge.py", "print('uncommitted')\n")
        result, restarted, _ = self.check()
        self.assertIn("will retry", result)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text(), original)


    def test_new_commit_during_preflight_defers_without_marking(self):
        update.initialize(self.root, path=self.state)
        old_baseline = self.state.read_text()
        self.change("bridge.py", "print('updated')\n")
        self.commit("first change")
        def advance(*_args):
            self.change("bridge.py", "print('newer')\n")
            self.commit("second change")
        with patch.object(update, "validate_python", side_effect=advance), \
             patch.object(update, "active", return_value=True), \
             patch.object(update, "restart") as restart:
            response = update.apply(self.root, path=self.state, settle_seconds=0)
        self.assertIn("changed after preflight", response)
        restart.assert_not_called()
        self.assertEqual(self.state.read_text(), old_baseline)

    def test_dirty_runtime_during_service_check_defers(self):
        update.initialize(self.root, path=self.state)
        old_baseline = self.state.read_text()
        self.change("bridge.py", "print('updated')\n")
        self.commit("change runtime")
        def make_dirty(_service):
            self.change("bridge.py", "print('incomplete checkout')\n")
            return True
        with patch.object(update, "active", side_effect=make_dirty), \
             patch.object(update, "restart") as restart:
            response = update.apply(self.root, path=self.state, settle_seconds=0)
        self.assertIn("changed after preflight", response)
        restart.assert_not_called()
        self.assertEqual(self.state.read_text(), old_baseline)

    def test_docs_only_commit_moving_before_record_is_deferred(self):
        update.initialize(self.root, path=self.state)
        old_baseline = self.state.read_text()
        self.change("README.md", "docs v2\n")
        self.commit("docs only")
        changed_files = update.changed_files
        def race(root, old, new):
            result = changed_files(root, old, new)
            self.change("bridge.py", "print('new runtime')\n")
            self.commit("racing runtime change")
            return result
        with patch.object(update, "changed_files", side_effect=race), \
             patch.object(update, "restart") as restart:
            response = update.apply(self.root, path=self.state, settle_seconds=0)
        self.assertIn("changed before recording", response)
        restart.assert_not_called()
        self.assertEqual(self.state.read_text(), old_baseline)

    def test_syntax_error_never_interrupts_running_service(self):
        update.initialize(self.root, path=self.state)
        original = self.state.read_text()
        self.change("bridge.py", "if nope\n")
        self.commit("broken code")
        result, restarted, _ = self.check()
        self.assertIn("failed preflight", result)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text(), original)

    def test_restart_failure_is_recorded_to_prevent_restart_loops(self):
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('new')\n")
        self.commit("update")
        with patch.object(update, "active", return_value=True), \
             patch.object(update, "restart", side_effect=subprocess.CalledProcessError(1, "systemctl")):
            with self.assertRaises(subprocess.CalledProcessError):
                update.apply(self.root, path=self.state, settle_seconds=0)
        self.assertEqual(self.state.read_text().strip(), update.head(self.root))
        self.assertEqual(self.check()[1], [])

    def test_reflog_detects_local_commits_without_network(self):
        self.assertEqual(update.reflog(self.root), self.root / ".git" / "logs" / "HEAD")
        update.initialize(self.root, path=self.state)
        old = self.state.read_text()
        self.change("bridge.py", "print('changed')\n")
        self.commit("local commit")
        self.assertNotEqual(old.strip(), update.head(self.root))

    def test_symlinked_state_is_rejected(self):
        self.state.parent.mkdir(parents=True)
        target = self.state.parent / "target"
        target.write_text("not a state")
        self.state.symlink_to(target)
        with self.assertRaises(ValueError):
            update.initialize(self.root, path=self.state)


if __name__ == "__main__":
    unittest.main()
