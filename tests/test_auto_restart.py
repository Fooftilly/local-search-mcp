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

    def test_status_never_initializes_or_mutates_or_restarts(self):
        with patch.object(update, "active", side_effect=AssertionError("probe")), \
             patch.object(update, "restart", side_effect=AssertionError("restart")), \
             patch.object(update, "write_state", side_effect=AssertionError("write")):
            self.assertIn("No local update baseline",
                          update.inspect_status(self.root, path=self.state))
        self.assertFalse(self.state.exists())
        update.initialize(self.root, path=self.state)
        baseline = self.state.read_bytes()
        with patch.object(update, "active", side_effect=AssertionError("probe")), \
             patch.object(update, "restart", side_effect=AssertionError("restart")), \
             patch.object(update, "write_state", side_effect=AssertionError("write")):
            self.assertIn("No local commit change",
                          update.inspect_status(self.root, path=self.state))
            self.change("README.md", "a docs change\n")
            self.commit("docs update")
            self.assertIn("Documentation/test-only",
                          update.inspect_status(self.root, path=self.state))
            self.change("bridge.py", "print('updated')\n")
            self.commit("runtime update")
            self.assertIn("runtime file(s) changed",
                          update.inspect_status(self.root, path=self.state))
            self.change("requirements.txt", "fastmcp>=4,<6\n")
            self.commit("requirements update")
            self.assertIn("explicit --dependencies-ready",
                          update.inspect_status(self.root, path=self.state))
        self.assertEqual(self.state.read_bytes(), baseline)

    def test_status_detects_transitive_dependency_inputs(self):
        self.change("requirements.txt", "-r locks/pins.txt\n")
        self.change("locks/pins.txt", "fastmcp>=4,<5\n")
        self.commit("initial includes")
        update.initialize(self.root, path=self.state)
        self.change("locks/pins.txt", "fastmcp>=4,<6\n")
        self.commit("update included file")
        result = update.inspect_status(self.root, path=self.state)
        self.assertIn("1 dependency input(s) changed", result)
        self.assertIn("explicit --dependencies-ready", result)

    def test_read_only_status_never_runs_configured_clean_filter(self):
        import shlex
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('new commit')\n")
        self.commit("pending update")
        outside = Path(self.tmp.name)
        marker = outside / "clean-filter-executed"
        hook = outside / "clean-filter.sh"
        hook.write_text("#!/bin/sh\nprintf 'ran' >> " +
                        shlex.quote(str(marker)) + "\ncat\n")
        hook.chmod(0o700)
        self.change(".gitattributes", "*.txt filter=x\n")
        self.change("fixture.txt", "sample\n")
        self.git("config", "filter.x.clean", str(hook))
        self.commit("enable Git clean filter")
        marker.unlink(missing_ok=True)
        (self.root / "fixture.txt").touch()
        # No Git status command may run from the read-only classification.
        self.assertIn("tracked runtime edits",
                      update.inspect_status(self.root, path=self.state))
        self.assertFalse(marker.exists())

    def test_status_missing_invalid_or_symlink_baseline(self):
        self.assertIn("No local update baseline",
                      update.inspect_status(self.root, path=self.state))
        update.write_state(self.state, "f" * 40)
        before = self.state.read_bytes()
        self.assertIn("Git objects are unavailable locally",
                      update.inspect_status(self.root, path=self.state))
        self.assertEqual(self.state.read_bytes(), before)
        self.state.unlink()
        target = self.root / "secret"
        target.write_text("unchanged")
        self.state.symlink_to(target)
        with self.assertRaises(ValueError):
            update.inspect_status(self.root, path=self.state)
        self.assertEqual(target.read_text(), "unchanged")

    def test_status_defers_dirty_runtime_tree_without_changing_baseline(self):
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('new')\n")
        self.commit("runtime commit")
        baseline = self.state.read_bytes()
        self.change("bridge.py", "print('dirty')\n")
        self.assertIn("tracked runtime edits",
                      update.inspect_status(self.root, path=self.state))
        self.assertEqual(self.state.read_bytes(), baseline)

    def test_status_reports_matching_head_even_with_dirty_runtime(self):
        update.initialize(self.root, path=self.state)
        baseline = self.state.read_bytes()
        self.change("bridge.py", "print('dirty, same commit')\n")
        self.assertIn("No local commit change",
                      update.inspect_status(self.root, path=self.state))
        self.assertEqual(self.state.read_bytes(), baseline)

    def test_status_git_subprocesses_are_offline_and_have_no_optional_locks(self):
        update.initialize(self.root, path=self.state)
        self.change("requirements.txt", "fastmcp>=4,<6\n")
        self.commit("dependency update")
        real_run = subprocess.run
        inspected = []

        def watch(args, **kwargs):
            if args and args[0] == "git" and kwargs.get("cwd") == self.root:
                environment = kwargs.get("env")
                self.assertIsNotNone(environment, args)
                for key, expected in (
                    ("GIT_NO_LAZY_FETCH", "1"),
                    ("GIT_OPTIONAL_LOCKS", "0"),
                    ("GIT_TERMINAL_PROMPT", "0"),
                ):
                    self.assertEqual(environment.get(key), expected,
                                     f"{args}: missing {key}")
                inspected.append(tuple(args))
            return real_run(args, **kwargs)

        with patch.object(update.subprocess, "run", side_effect=watch):
            status = update.inspect_status(self.root, path=self.state)
        self.assertIn("explicit --dependencies-ready", status)
        self.assertFalse(any("status" in args or "diff-files" in args
                             for args in inspected))
        self.assertTrue(any("diff-index" in args for args in inspected))
        self.assertTrue(any("--debug" in args for args in inspected))
        self.assertTrue(any("cat-file" in args for args in inspected))
        self.assertTrue(any("diff" in args for args in inspected))
        self.assertTrue(any("ls-files" in args for args in inspected))
        self.assertTrue(any("rev-parse" in args for args in inspected))

    def test_status_does_not_use_network_for_missing_objects(self):
        update.initialize(self.root, path=self.state)
        self.change("bridge.py", "print('updated')\n")
        self.commit("local change")
        original_git = update.git

        def missing_object(root, *args, **kwargs):
            if args and args[0] == "cat-file":
                self.assertTrue(kwargs.get("read_only"))
                raise subprocess.CalledProcessError(1, ["git", *args])
            return original_git(root, *args, **kwargs)

        with patch.object(update, "git", side_effect=missing_object):
            result = update.inspect_status(self.root, path=self.state)
        self.assertIn("Git objects are unavailable locally", result)
        self.assertNotEqual(self.state.read_text().strip(),
                            self.git("rev-parse", "HEAD"))

    def test_status_cli_is_read_only_and_incompatible_with_apply_flags(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        with patch.object(update, "inspect_status", return_value="local status") as status, \
             patch.object(update, "apply", side_effect=AssertionError("applied")), \
             redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(update.main(["--status", "--state-file", str(self.state)]), 0)
        status.assert_called_once_with(path=self.state)
        self.assertEqual(stream.getvalue().strip(), "local status")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            update.main(["--status", "--dependencies-ready"])


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
        self.assertIn("TimeoutStartSec=10min", extra["local-search-mcp-update.service"])
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
                    "pip @ https://example.invalid/pip-1.0-py3-none-any.whl\n")
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
                script_at = command.index("-c") + 1
                return actual_run([sys.executable, "-I", "-c",
                                   *command[script_at:]], **kwargs)
            self.assertEqual(command[-3:], ["-m", "pip", "check"])
            return subprocess.CompletedProcess(command, 0)

        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(self.root))
        self.assertEqual(len(commands), 2)
        self.assertTrue(any("pip" in cmd and "check" in cmd for cmd in commands))

        # A URL entry cannot pass if the named distribution is absent locally.
        self.change("requirements.txt",
                    "definitely-absent-local-mcp-98371 @ "
                    "https://example.invalid/missing.whl\n")
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(self.root))

    def test_replacement_manifest_changes_stay_gated_after_migration(self):
        update.initialize(self.root, path=self.state)
        (self.root / "config").mkdir()
        self.git("mv", "requirements.txt", "config/dependencies.txt")
        self.commit("migrate pip dependency manifest")
        initial, _, _ = self.check()
        self.assertIn("--dependencies-ready", initial)
        with patch.object(update, "requirements_ready", return_value=True):
            self.assertIn("Restart requested", self.check(dependencies_ready=True)[0])

        # The second commit does not mention the old requirements.txt.
        self.change("config/dependencies.txt", "fastmcp>=4.9,<5\n")
        self.commit("change replacement dependency manifest")
        before = self.state.read_text()
        message, restarted, _ = self.check()
        self.assertIn("--dependencies-ready", message)
        self.assertEqual(restarted, [])
        self.assertEqual(before, self.state.read_text())
        with patch.object(update, "requirements_ready", return_value=False):
            failed, restarted, _ = self.check(dependencies_ready=True)
        self.assertIn("failed offline validation", failed)
        self.assertEqual(restarted, [])
        with patch.object(update, "requirements_ready", return_value=True) as check:
            result, restarted, _ = self.check(dependencies_ready=True)
        self.assertIn("Restart requested", result)
        origins = {"config/dependencies.txt": {"config/dependencies.txt"}}
        roles = {"config/dependencies.txt": {("config/dependencies.txt", False)}}
        check.assert_called_once_with(
            self.root, ["config/dependencies.txt"], origins, roles,
        )
        self.assertEqual(restarted, list(update.SERVICES))

    def test_transitive_pip_includes_are_gated_independent_of_filename(self):
        update.initialize(self.root, path=self.state)
        self.change("requirements.txt", "-r pins.txt\n-c locks/prod.txt\n")
        self.change("pins.txt", "fastmcp>=4,<5\n")
        self.change("locks/prod.txt", "fastmcp<5\n")
        self.commit("convert to included pins")
        with patch.object(update, "requirements_ready", return_value=True):
            self.assertIn("Restart requested", self.check(dependencies_ready=True)[0])
        self.assertEqual(update.referenced_requirement_files(self.root),
                         {"requirements.txt", "pins.txt", "locks/prod.txt"})

        for filename in ("pins.txt", "locks/prod.txt"):
            previous = self.state.read_text()
            self.change(filename, "fastmcp>=4.1,<5\n")
            self.commit("modify included dependency input")
            message, restarted, _ = self.check()
            self.assertIn("--dependencies-ready", message)
            self.assertEqual(restarted, [])
            self.assertEqual(self.state.read_text(), previous)
            with patch.object(update, "requirements_ready", return_value=True):
                self.assertIn("Restart requested",
                              self.check(dependencies_ready=True)[0])

    def test_included_pip_file_under_docs_is_not_ignored(self):
        update.initialize(self.root, path=self.state)
        self.change("requirements.txt", "-r docs/pins.txt\n")
        self.change("docs/pins.txt", "fastmcp>=4,<5\n")
        self.commit("add included manifest under docs")
        with patch.object(update, "requirements_ready", return_value=True):
            self.check(dependencies_ready=True)
        before = self.state.read_text()
        self.change("docs/pins.txt", "fastmcp>=4.1,<5\n")
        self.commit("update included docs manifest")
        message, restarted, _ = self.check()
        self.assertIn("--dependencies-ready", message)
        self.assertEqual(restarted, [])
        self.assertEqual(self.state.read_text(), before)

    def test_deleted_transitive_manifest_remains_gated(self):
        update.initialize(self.root, path=self.state)
        self.change("requirements.txt", "-r pins.txt\n")
        self.change("pins.txt", "fastmcp>=4,<5\n")
        self.commit("add arbitrary pip manifest")
        with patch.object(update, "requirements_ready", return_value=True):
            self.check(dependencies_ready=True)
        self.git("rm", "pins.txt")
        self.commit("delete included requirements")
        message, restarted, _ = self.check()
        self.assertIn("--dependencies-ready", message)
        self.assertEqual(restarted, [])

    def test_moved_manifest_include_validates_arbitrary_changed_pin(self):
        update.initialize(self.root, path=self.state)
        self.change("requirements/base.txt", "-r ../pins.txt\n")
        self.change("pins.txt", "pip>=0\n")
        self.git("rm", "requirements.txt")
        self.commit("migrate to included root")
        with patch.object(update, "requirements_ready", return_value=True):
            self.check(dependencies_ready=True)
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        actual_run = subprocess.run

        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return actual_run([sys.executable, "-I", "-c",
                                   *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)

        self.change("pins.txt", "missing-package-xy98371>=1\n")
        self.commit("uninstalled pinned package")
        before = self.state.read_text()
        message, restarted, _ = self.check()
        self.assertIn("--dependencies-ready", message)
        self.assertEqual(restarted, [])
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(
                self.root, ["pins.txt"],
                {"pins.txt": {"requirements/base.txt"}},
            ))
        self.assertEqual(before, self.state.read_text())

    def test_standalone_constraints_do_not_require_optional_packages(self):
        self.change("constraints.txt", "uninstalled-optional-xy98371>=1\n")
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        actual_run = subprocess.run
        seen = []
        def offline_runner(command, **kwargs):
            seen.append(command)
            if "-c" in command:
                i = command.index("-c") + 1
                return actual_run([sys.executable, "-I", "-c",
                                   *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["constraints.txt"]))
        self.assertIn("C:constraints.txt", seen[0])
        # If the optional distribution happens to be installed, a violated
        # constraint must still fail the offline check.
        self.change("constraints.txt", "pip>=999999\n")
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(
                self.root, ["constraints.txt"]))

    def test_approved_editable_and_multiline_hash_inputs(self):
        self.change("requirements.txt",
                    "-e .\npip>=0 \\\n  --hash=sha256:" + "0" * 64 + "\n")
        self.change("pyproject.toml",
                    "[project]\nname = 'pip'\nversion = '1.0'\n")
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        actual_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return actual_run([sys.executable, "-I", "-c",
                                   *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements.txt"]))
        self.change("requirements.txt", "-e .[dev]\n")
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements.txt"]))
        self.change("requirements.txt",
                    "-e missing-local-project\n")
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements.txt"]))

    def test_other_dependency_formats_are_gated_for_manual_review(self):
        self.assertTrue(update.dependency_manifest("pyproject.toml"))
        self.assertTrue(update.dependency_manifest("uv.lock"))
        self.assertTrue(update.dependency_manifest("requirements/base.txt"))
        self.assertTrue(update.dependency_manifest("config/dependencies.txt"))
        self.assertTrue(update.dependency_manifest("constraints.txt"))
        self.assertFalse(update.dependency_manifest("docs/requirements.txt"))
        self.assertFalse(update.dependency_manifest("README.md"))
        update.initialize(self.root, path=self.state)
        self.change("pyproject.toml", "[project]\ndependencies = []\n")
        self.commit("add metadata")
        result, restarted, _ = self.check()
        self.assertIn("--dependencies-ready", result)
        self.assertEqual(restarted, [])

    def test_pip_requirement_includes_and_constraints_offline(self):
        # Use the running interpreter only to evaluate the isolated probe;
        # keep its pip check mocked to avoid relying on the host environment.
        self.change("requirements.txt",
                    "-r requirements/base.txt\n-c constraints.txt\n")
        self.change("requirements/base.txt", "pip>=0\n")
        self.change("constraints.txt", "pip>=0\n")
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        actual_run = subprocess.run

        def offline_runner(command, **kwargs):
            if "-c" in command:
                index = command.index("-c") + 1
                return actual_run([sys.executable, "-I", "-c",
                                   *command[index:]], **kwargs)
            self.assertEqual(command[-3:], ["-m", "pip", "check"])
            return subprocess.CompletedProcess(command, 0)

        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements.txt", "requirements/base.txt",
                            "constraints.txt"]))
            self.change("constraints.txt", "pip>=999999\n")
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements.txt", "constraints.txt"]))
            self.change("requirements/base.txt", "-r ../requirements.txt\n")
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements.txt"]))
            self.change("requirements/base.txt", "-r ../outside.txt\n")
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements.txt"]))

    def test_included_constraints_under_requirements_are_not_mandatory(self):
        # requirements/prod.txt matches the filename heuristic but its -c
        # edge makes non-installed packages optional constraints.
        self.change("requirements.txt", "-c requirements/prod.txt\n")
        self.change("requirements/prod.txt", "absent-optional-xy98371==1.0\n")
        self.commit("include optional constraint")
        origins = {}
        update.referenced_requirement_files(self.root, origins=origins)
        self.assertIn("requirements.txt", origins["requirements/prod.txt"])
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        actual_run = subprocess.run

        def offline_runner(command, **kwargs):
            if "-c" in command:
                pos = command.index("-c") + 1
                return actual_run([sys.executable, "-I", "-c",
                                   *command[pos:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements/prod.txt"], origins,
            ))
            self.change("requirements/prod.txt", "pip>=999999\n")
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements/prod.txt"], origins,
            ))

    def test_quoted_and_env_expanded_pip_includes(self):
        self.change("requirements.txt",
                    '-r "pins/base file.txt"\n-c ${REQ_DIR}/prod.txt\n')
        self.change("pins/base file.txt", "pip>=0\n")
        self.change("locks/prod.txt", "pip>=0\n")
        self.commit("quoted and interpolated include filenames")
        with patch.dict(os.environ, {"REQ_DIR": "locks"}):
            origins = {}
            found = update.referenced_requirement_files(self.root, origins=origins)
            self.assertIn("pins/base file.txt", found)
            self.assertIn("locks/prod.txt", found)
            self.assertIn("requirements.txt", origins["locks/prod.txt"])
            interpreter = self.root / ".venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            real_run = subprocess.run
            def offline_runner(command, **kwargs):
                if "-c" in command:
                    i = command.index("-c") + 1
                    return real_run([sys.executable, "-I", "-c",
                                     *command[i:]], **kwargs)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(update.subprocess, "run", side_effect=offline_runner):
                self.assertTrue(update.requirements_ready(
                    self.root, ["pins/base file.txt", "locks/prod.txt"], origins,
                ))

    def test_nested_requirement_resets_parent_constraint_role(self):
        self.change("requirements.txt", "-c constraints.txt\n")
        self.change("constraints.txt", "-r required.txt\n")
        self.change("required.txt", "absent-nested-required-xy98371>=1\n")
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c",
                                 *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements.txt", "constraints.txt"],
            ))
            self.change("constraints.txt", "absent-optional-xy98371>=1\n")
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements.txt", "constraints.txt"],
            ))

    def test_group_writable_state_directory_is_rejected(self):
        update.initialize(self.root, path=self.state)
        old = self.state.read_text()
        self.state.parent.chmod(0o770)
        with self.assertRaisesRegex(ValueError, "must be private"):
            update.write_state(self.state, update.head(self.root))
        self.assertEqual(self.state.read_text(), old)

    def test_live_requirements_not_blocked_by_development_includers(self):
        self.change("requirements.txt", "pip>=0\n")
        self.change("requirements-dev.txt",
                    "-r requirements.txt\nmissing-dev-only-xy98371>=1\n")
        self.commit("add optional development requirements")
        origins = {}
        update.referenced_requirement_files(self.root, origins=origins)
        self.assertIn("requirements-dev.txt", origins["requirements.txt"])
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c", *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements.txt"], origins))

    def test_comment_continuation_does_not_hide_pip_include(self):
        self.change("requirements.txt",
                    "pip>=0\n# pinned below \\\n-r pins.txt\n")
        self.change("pins.txt", "uninstalled-required-xy98371>=1\n")
        self.commit("comment with a trailing backslash")
        origins = {}
        files = update.referenced_requirement_files(self.root, origins=origins)
        self.assertIn("pins.txt", files)
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c", *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(
                self.root, ["pins.txt"], origins))

    def test_pip_index_option_aliases_are_accepted_offline(self):
        self.change(
            "requirements.txt",
            "--index-url=https://example.invalid/simple\n"
            "-i https://example.invalid/simple\n"
            "--extra-index-url=https://example.invalid/other\n"
            "-f https://example.invalid/packages\n"
            "pip>=0\n",
        )
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c", *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements.txt"]))

    def test_shared_pins_with_constraint_and_required_roles(self):
        self.git("rm", "requirements.txt")
        self.change("requirements/prod.txt", "pip\n-c pins.txt\n")
        self.change("requirements/worker.txt", "-r pins.txt\n")
        self.change("requirements/pins.txt",
                    "missing-runtime-pkg-xy98371==1.0\n")
        self.commit("mixed constraint and required include roles")
        origins, roles = {}, {}
        update.referenced_requirement_files(self.root, origins=origins, roles=roles)
        self.assertIn(("requirements/prod.txt", True),
                      roles["requirements/pins.txt"])
        self.assertIn(("requirements/worker.txt", False),
                      roles["requirements/pins.txt"])
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c", *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements/pins.txt"], origins, roles))
            self.change("requirements/pins.txt", "pip>=0\n")
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements/pins.txt"], origins, roles))

    def test_shared_constraint_pins_keep_their_c_role(self):
        # Under pip-tools layering a generic pins file is a constraint,
        # even when it also matches the tracked manifest-name heuristic.
        self.git("rm", "requirements.txt")
        self.change("requirements/prod.txt", "-r base.txt\n-c pins.txt\n")
        self.change("requirements/dev.txt",
                    "-r base.txt\n-c pins.txt\nmissing-dev-only-xy98371>=1\n")
        self.change("requirements/base.txt", "pip>=0\n")
        self.change("requirements/pins.txt",
                    "pip>=0\nmissing-optional-xy98371==1.0\n")
        self.commit("split prod and dev with a shared constraint lock")
        origins, roles = {}, {}
        update.referenced_requirement_files(self.root, origins=origins, roles=roles)
        self.assertIn(("requirements/prod.txt", True),
                      roles["requirements/pins.txt"])
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c", *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements/pins.txt"], origins, roles))
            self.change("requirements/pins.txt", "pip>=999999\n")
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements/pins.txt"], origins, roles))

    def test_shared_pip_base_does_not_require_dev_only_dependencies(self):
        self.change("requirements/base.txt", "pip>=0\n")
        self.change("requirements/prod.txt", "-r base.txt\n")
        self.change("requirements/dev.txt",
                    "-r base.txt\nmissing-dev-only-xy98371>=1\n")
        self.git("rm", "requirements.txt")
        self.commit("shared base with separate prod and dev roots")
        origins = {}
        update.referenced_requirement_files(self.root, origins=origins)
        self.assertIn("requirements/dev.txt", origins["requirements/base.txt"])
        self.assertIn("requirements/prod.txt", origins["requirements/base.txt"])
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c", *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["requirements/base.txt"], origins))
        self.change("requirements/base.txt", "missing-required-xy98371>=1\n")
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertFalse(update.requirements_ready(
                self.root, ["requirements/base.txt"], origins))

    def test_shared_pip_pin_with_live_root_ignores_dev_requirements(self):
        self.change("requirements.txt", "-r pins.txt\n")
        self.change("pins.txt", "pip>=0\n")
        self.change("requirements-dev.txt",
                    "-r pins.txt\nmissing-dev-only-xy98371>=1\n")
        self.commit("share pinned runtime input")
        origins = {}
        update.referenced_requirement_files(self.root, origins=origins)
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        real_run = subprocess.run
        def offline_runner(command, **kwargs):
            if "-c" in command:
                i = command.index("-c") + 1
                return real_run([sys.executable, "-I", "-c", *command[i:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["pins.txt"], origins))

    def test_pip_replacement_manifest_checks_local_versions(self):
        (self.root / "config").mkdir()
        self.git("mv", "requirements.txt", "config/dependencies.txt")
        self.change("config/dependencies.txt", "pip>=0\n")
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        actual_run = subprocess.run

        def offline_runner(command, **kwargs):
            if "-c" in command:
                index = command.index("-c") + 1
                return actual_run([sys.executable, "-I", "-c",
                                   *command[index:]], **kwargs)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(update.subprocess, "run", side_effect=offline_runner):
            self.assertTrue(update.requirements_ready(
                self.root, ["config/dependencies.txt"]))
            self.change("config/dependencies.txt", "pip>=999999\n")
            self.assertFalse(update.requirements_ready(
                self.root, ["config/dependencies.txt"]))

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
