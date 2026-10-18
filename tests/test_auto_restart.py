"""Regression tests for opt-in offline Git-driven systemd restart checks."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import restart_on_update as update
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
        self.assertNotIn("git pull", "".join(extra.values()))

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
