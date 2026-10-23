"""Offline checks for the conservative ChatGPT-first setup entry point."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

from scripts import setup_wizard as wizard


class SetupWizardTests(unittest.TestCase):
    def call(self, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = wizard.main(list(args))
        return status, stdout.getvalue()

    def test_help_has_no_imports_commands_or_health_probes(self):
        with (patch.object(wizard, "check_report", side_effect=AssertionError("check")),
              patch.object(wizard, "status_report", side_effect=AssertionError("status")),
              patch.object(wizard, "run_existing", side_effect=AssertionError("run")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            with self.assertRaises(SystemExit) as exc:
                wizard.main(["--help"])
        self.assertEqual(exc.exception.code, 0)
        for option in ("--check", "--status", "--dry-run", "--chatgpt"):
            self.assertIn(option, out.getvalue())
        self.assertIn("30 minutes", out.getvalue())

    def test_shell_help_is_read_only_and_noninteractive(self):
        exe = Path(__file__).resolve().parents[1] / "setup.sh"
        result = subprocess.run(["bash", str(exe), "--help"], capture_output=True,
                                text=True, check=False, timeout=10, input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Examples:", result.stdout)

    def test_real_setup_entrypoint_imports_root_diagnostics_without_pythonpath(self):
        # The shell entry point runs from any working directory; importing
        # bootstrap and health_cli must not rely on a developer PYTHONPATH.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as config_root:
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["XDG_CONFIG_HOME"] = config_root
            env["XDG_STATE_HOME"] = config_root
            for option in ("--status", "--check"):
                with self.subTest(option=option):
                    result = subprocess.run(
                        ["bash", str(root / "setup.sh"), option],
                        cwd=config_root, env=env, capture_output=True, text=True,
                        check=False, timeout=35, input="")
                    self.assertIn(result.returncode, (0, 1), result.stderr)
                    data = json.loads(result.stdout)
                    health = (data["local_health"] if option == "--status"
                              else data["runtime"]["local_health"])
                    self.assertIn("TCP listener checks", health["note"])
                    if option == "--check":
                        self.assertTrue(data["configuration"]["available"])

    def test_project_venv_dependency_probe_uses_local_checker_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            payload = {"remote_requested": False, "packages": [
                {"package": "fastmcp", "installed": "4.1.0"},
                {"package": "httpx", "installed": "0.28.1"},
                {"package": "idna", "installed": "3.11"},
            ]}
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))
            with (patch.object(wizard, "ROOT", root),
                  patch.object(wizard.subprocess, "run", return_value=result) as invoke):
                report = wizard.dependency_report()
            self.assertEqual(report["state"], "available")
            self.assertEqual(report["packages"][0]["installed"], "4.1.0")
            argv = invoke.call_args.args[0]
            self.assertEqual(argv, [str(python), "-I", "-B",
                                    str(root / "scripts" / "check_dependency_versions.py")])
            self.assertEqual(invoke.call_args.kwargs["cwd"], root)
            self.assertTrue(invoke.call_args.kwargs["capture_output"])
            self.assertFalse(invoke.call_args.kwargs["check"])

    def test_missing_venv_is_not_silent_system_python_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(wizard, "ROOT", Path(tmp)),
                  patch.object(wizard.subprocess, "run",
                               side_effect=AssertionError("must not run"))):
                result = wizard.dependency_report()
            self.assertEqual(result["state"], "missing")
            self.assertEqual(result["packages"], [])

    def test_failed_venv_probe_redacts_subprocess_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            failed = subprocess.CompletedProcess([], 1, stdout="sensitive output",
                                                 stderr="SECRET_TOKEN")
            with (patch.object(wizard, "ROOT", root),
                  patch.object(wizard.subprocess, "run", return_value=failed)):
                result = wizard.dependency_report()
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("SECRET_TOKEN", json.dumps(result))
            self.assertNotIn("sensitive output", json.dumps(result))

    def test_check_uses_project_venv_report_for_readiness(self):
        dependencies = {"state": "available", "packages": [
            {"package": "fastmcp", "installed": "4.1"},
            {"package": "httpx", "installed": "0.28"},
            {"package": "idna", "installed": "3.11"},
        ]}
        with (patch.object(wizard, "dependency_report", return_value=dependencies),
              patch.object(wizard, "configured_sources",
                           return_value={"available": True}),
              patch.object(wizard, "status_report",
                           return_value={"local_health": {"status": "healthy"}}),
              patch.object(wizard, "project_python", return_value=Path("/virtual/python"))):
            report = wizard.check_report()
        self.assertTrue(report["ready"])
        self.assertEqual(report["dependency_versions_in_project_venv"], dependencies)
        self.assertNotIn("dependency_versions_in_current_interpreter", report)
        dependencies = {**dependencies, "packages": [
            *dependencies["packages"][:-1],
            {"package": "idna", "installed": None},
        ]}
        with (patch.object(wizard, "dependency_report", return_value=dependencies),
              patch.object(wizard, "configured_sources",
                           return_value={"available": True}),
              patch.object(wizard, "status_report",
                           return_value={"local_health": {"status": "healthy"}})):
            self.assertFalse(wizard.check_report()["ready"])

    def test_menu_dependency_inspection_only_reads_project_venv(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["5", "0"]),
              patch.object(wizard, "dependency_report",
                           return_value={"state": "missing", "packages": []}) as report,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("no system python helper")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(wizard.main([]), 0)
        report.assert_called_once_with()
        self.assertIn('"state": "missing"', output.getvalue())

    def test_dry_run_never_checks_health_or_runs_programs(self):
        with (patch.object(wizard, "check_report", side_effect=AssertionError("check")),
              patch.object(wizard, "status_report", side_effect=AssertionError("status")),
              patch.object(wizard, "run_existing", side_effect=AssertionError("run"))):
            rc, output = self.call("--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output)["default_client"].split()[0], "ChatGPT")
        self.assertEqual(len(json.loads(output)["actions"]), 7)

    def test_status_returns_only_allowlisted_service_state(self):
        with (patch.object(wizard, "health",
                           return_value={"status": "unavailable", "checks": {}}),
              patch.object(wizard, "user_unit_states",
                           return_value={"tunnel": {"state": "unknown"}}),
              patch.object(wizard, "run_existing", side_effect=AssertionError("run"))):
            rc, out = self.call("--status")
        report = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(report["local_health"]["status"], "unavailable")
        self.assertNotIn("private", out.lower())

    def test_check_failure_redacts_and_fails_closed(self):
        with (patch.object(wizard, "configured_sources",
                           return_value={"available": False, "error": "unsafe config"}),
              patch.object(wizard, "status_report",
                           return_value={"local_health": {"status": "unavailable"}})):
            rc, out = self.call("--check")
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ready"])
        self.assertEqual(json.loads(out)["configuration"]["available"], False)

    def test_unavailable_systemd_is_unknown_not_inactive(self):
        with patch.object(wizard.subprocess, "run",
                          return_value=Mock(returncode=1, stdout="private token")):
            result = wizard.user_unit_states()
        self.assertTrue(all(s["state"] == "unknown" for s in result.values()))

    def test_non_tty_never_invokes_setup_or_shutdown(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: False)),
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("run"))):
            self.assertEqual(wizard.main([]), 2)

    def test_cancelled_mutating_action_has_no_side_effect(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["7", "NO", "0"]),
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("run")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(wizard.main([]), 0)
        self.assertIn("Cancelled", out.getvalue())

    def test_confirmed_setup_uses_existing_script_only(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["3", "CONFIGURE", "0"]),
              patch.object(wizard, "run_existing", return_value=0) as command,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(wizard.main([]), 0)
        command.assert_called_once_with("bootstrap.py", "--setup-only")

    def test_other_clients_not_silently_installed(self):
        rc, message = self.call("--chatgpt")
        self.assertEqual(rc, 0)
        self.assertIn("manually", message)
        self.assertIn("No settings for Cursor, Claude, or Hermes", message)


if __name__ == "__main__":
    unittest.main()
