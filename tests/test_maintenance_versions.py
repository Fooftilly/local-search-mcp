"""Offline verification of consolidated maintenance version checks."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scripts import maintenance_versions as maintenance


class MaintenanceVersionsTests(unittest.TestCase):
    def test_default_report_has_no_public_requests_or_updates(self):
        deps = {"state": "available", "packages": []}
        crawler = {"local": {"version": "0.9.5"}, "remote_requested": False}
        git = {"state": "inspected", "updates_applied": False}
        with (patch.object(maintenance.setup_wizard, "dependency_report",
                           return_value=deps) as packages,
              patch.object(maintenance.update_preflight, "report",
                           return_value=git) as local,
              patch.object(maintenance.check_crawler_versions, "version_report",
                           return_value=crawler) as crawl,
              patch.object(maintenance.check_bridge_version, "check",
                           side_effect=AssertionError("No remote requests")),
              patch.object(maintenance.subprocess, "run",
                           side_effect=AssertionError("No subprocess"))):
            result = maintenance.report()
        packages.assert_called_once_with()
        local.assert_called_once_with()
        crawl.assert_called_once_with(check_remote=False)
        self.assertFalse(result["remote_requested"])
        self.assertFalse(result["updates_applied"])
        self.assertFalse(result["downloads_performed"])
        self.assertEqual(result["dependencies"], deps)
        self.assertNotIn("public_bridge_revision", result)

    def test_explicit_remote_dependency_check_uses_project_interpreter(self):
        payload = {"remote_requested": True, "packages": [
            {"package": name, "installed": "1.0", "pypi": "1.1",
             "same_version": False} for name in ("fastmcp", "httpx", "idna")]}
        completed = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(payload), stderr="PRIVATE_SECRET")
        with (patch.object(maintenance.setup_wizard, "project_python",
                           return_value=Path("/private/venv/python")),
              patch.object(maintenance.subprocess, "run",
                           return_value=completed) as invoke):
            result = maintenance._dependency_versions(True)
        self.assertEqual(result["state"], "available")
        self.assertEqual(len(result["packages"]), 3)
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        command = invoke.call_args.args[0]
        self.assertEqual(command[:3], ["/private/venv/python", "-I", "-B"])
        self.assertEqual(command[-1], "--check-remote")
        self.assertFalse(invoke.call_args.kwargs["check"])
        self.assertTrue(invoke.call_args.kwargs["capture_output"])

    def test_remote_incomplete_or_untrusted_output_fails_closed(self):
        valid = [{"package": name, "installed": "1.0", "pypi": "1.1"}
                 for name in ("fastmcp", "httpx", "idna")]
        documents = (
            "INVALID",
            json.dumps({"remote_requested": False, "packages": valid}),
            json.dumps({"remote_requested": True, "packages": valid[:2]}),
            json.dumps({"remote_requested": True, "packages": [
                {**valid[0], "pypi": "1.1\\nSECRET"}, *valid[1:]]}),
            json.dumps({"remote_requested": True, "packages": [
                {**valid[0], "token": "PRIVATE"}, *valid[1:]]}),
        )
        with patch.object(maintenance.setup_wizard, "project_python",
                          return_value=Path("/private/venv/python")):
            for output in documents:
                with self.subTest(output=output[:40]), patch.object(
                    maintenance.subprocess, "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, stdout=output, stderr="PRIVATE_SECRET")):
                    result = maintenance._dependency_versions(True)
                    self.assertEqual(result["state"], "unavailable")
                    self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
                    self.assertNotIn("token", json.dumps(result))

    def test_missing_venv_does_not_fall_back_to_system_python(self):
        with (patch.object(maintenance.setup_wizard, "project_python",
                           return_value=None),
              patch.object(maintenance.subprocess, "run",
                           side_effect=AssertionError("No fallback"))):
            result = maintenance._dependency_versions(True)
        self.assertEqual(result["state"], "unavailable")

    def test_public_failures_are_sanitized(self):
        with (patch.object(maintenance, "_dependency_versions",
                           return_value={"state": "unavailable"}),
              patch.object(maintenance.update_preflight, "report",
                           return_value={"state": "inspected"}),
              patch.object(maintenance.check_crawler_versions, "version_report",
                           side_effect=ValueError("private-token")),
              patch.object(maintenance.check_bridge_version, "check",
                           side_effect=ValueError("private-path"))):
            result = maintenance.report(check_remote=True)
        self.assertTrue(result["remote_requested"])
        self.assertEqual(result["crawler"]["state"], "unavailable")
        self.assertEqual(result["public_bridge_revision"]["state"], "unavailable")
        self.assertNotIn("private-token", json.dumps(result))
        self.assertNotIn("private-path", json.dumps(result))

    def test_cli_help_does_not_check_network_or_spawn_services(self):
        with (patch.object(maintenance, "report",
                           side_effect=AssertionError("must not run")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            with self.assertRaises(SystemExit) as result:
                maintenance.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--check-remote", out.getvalue())


if __name__ == "__main__":
    unittest.main()
