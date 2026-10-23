"""Offline checks for the conservative ChatGPT-first setup entry point."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
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
