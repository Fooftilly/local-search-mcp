"""Offline historical-release contract checks; no historical modules are imported."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import rollback_release_audit as audit


class HistoricalCompatibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.candidate = self.home / "candidate"
        source = self.candidate / "source"
        source.mkdir(parents=True, mode=0o700)
        self.source = source
        expected = {
            "bridge.py": ("web_search", "web_fetch"),
            "bootstrap.py": ("run_unattended_service",),
            "domain_policy.py": ("initialize",),
            "history_index.py": ("sync_history",),
            "history_log.py": ("logged_tool", "history_path", "append_history"),
            "health_cli.py": ("snapshot",),
        }
        self.assertEqual(audit.REQUIRED, expected)
        self.assertEqual(audit.UNITS, {
            "config/systemd/local-search-mcp-bridge.service": "bridge",
            "config/systemd/local-search-mcp-tunnel.service": "tunnel",
        })
        for name, functions in expected.items():
            if name == "bridge.py":
                body = ("@mcp.tool()\ndef web_search(query):\n    return query\n"
                        "@mcp.tool()\nasync def web_fetch(url):\n    return url\n")
            else:
                body = "".join(f"def {func}():\n    return True\n"
                               for func in functions) or "pass\n"
            self.put(name, body)
        for name in ("config/systemd/local-search-mcp-bridge.service",
                     "config/systemd/local-search-mcp-tunnel.service"):
            self.put(name, (audit.ROOT / name).read_text(encoding="utf-8"))
        self.put("requirements.txt", "fastmcp>=4,<5\nhttpx>=0.28,<1\nidna>=3,<4\n")
        self.python = self.home / "independent-venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        (self.python.parent.parent / "pyvenv.cfg").write_text(
            "home = /usr/bin\ninclude-system-site-packages = false\n")
        self.python.write_text("not executable fixture")
        self.python.chmod(0o700)
        self.uid = patch.object(audit.os, "geteuid", return_value=1000)
        self.uid.start()
        self.addCleanup(self.uid.stop)
        self.inspect = patch.object(audit.rollback_candidate, "inspect",
                                    return_value={"integrity_verified": True})
        self.inspect.start()
        self.addCleanup(self.inspect.stop)

    def put(self, name, text):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text)
        path.chmod(0o600)

    def test_valid_static_contracts_and_separate_dependency_report(self):
        with patch.object(audit, "_environment",
                          return_value={"checked": 3, "satisfied": 3,
                                        "all_satisfied": True}):
            result = audit.audit(self.candidate, python=self.python)
        self.assertTrue(result["static_contracts_passed"])
        self.assertTrue(result["dependency_environment_verified"])
        self.assertTrue(result["offline_checks_passed"])
        self.assertFalse(result["ready_for_live_use"])
        self.assertFalse(result["activation_performed"])
        self.assertFalse(result["old_state_reader_tested"])
        self.assertFalse(result["host_services_tested"])

    def test_missing_or_changed_api_cannot_pass(self):
        self.put("bridge.py", "def web_search(query):\n    return query\n"
                              "def web_fetch(url):\n    return url\n")
        with patch.object(audit, "_environment",
                          return_value={"checked": 3, "satisfied": 3,
                                        "all_satisfied": True}):
            result = audit.audit(self.candidate, python=self.python)
        self.assertFalse(result["offline_checks_passed"])
        self.assertFalse(result["static_contracts_passed"])
        self.assertIn("historical Python contract missing or invalid",
                      result["blockers"])

    def test_renamed_or_dynamic_mcp_registration_fails_closed(self):
        for decorator in ("@mcp.tool(name='search_v2')",
                          "@mcp.tool(**dynamic_config)", "@mcp.tool('search_v2')"):
            with self.subTest(decorator=decorator):
                self.put("bridge.py", (
                    decorator + "\ndef web_search(query):\n    return query\n"
                    "@mcp.tool()\ndef web_fetch(url):\n    return url\n"))
                result = audit.audit(self.candidate, python=self.python)
                self.assertFalse(result["static_contracts_passed"])

    def test_overridden_or_extra_systemd_directives_fail_closed(self):
        unit = next(iter(audit.UNITS))
        canonical = (audit.ROOT / unit).read_text()
        for malicious in ("NoNewPrivileges=false\n", "ExecStartPre=/bin/false\n",
                          "EnvironmentFile=/tmp/private-token\n",
                          "ExecStart=/bin/false\n", "User=root\n"):
            with self.subTest(malicious=malicious):
                self.put(unit, canonical + malicious)
                report = audit.audit(self.candidate, python=self.python)
                self.assertFalse(report["static_contracts_passed"])
                self.assertIn("historical systemd template contract differs",
                              report["blockers"])
        self.put(unit, canonical)

    def test_candidate_contained_virtualenv_is_rejected(self):
        nested = self.source / "internal-env" / "bin" / "python"
        nested.parent.mkdir(parents=True)
        (nested.parent.parent / "pyvenv.cfg").write_text(
            "home=/usr/bin\ninclude-system-site-packages = false\n")
        nested.write_text("fake")
        with self.assertRaisesRegex(ValueError, "independent"):
            audit._environment(nested, ["httpx>=0.28,<1"], self.candidate)

    def test_broken_syntax_missing_launcher_and_weak_systemd_fail(self):
        self.put("domain_policy.py", "def initialize(:\n pass\n")
        (self.source / "bootstrap.py").unlink()
        self.put("config/systemd/local-search-mcp-tunnel.service",
                 "[Service]\nExecStart=/bin/true\n")
        result = audit.audit(self.candidate)
        self.assertFalse(result["offline_checks_passed"])
        self.assertTrue(any("systemd" in msg for msg in result["blockers"]))
        self.assertTrue(any("Python" in msg for msg in result["blockers"]))
        self.assertFalse(result["ready_for_live_use"])

    def test_requirements_fail_closed_on_includes_urls_and_settings(self):
        for bad in ("-r other.txt\n", "httpx @ https://example.com/package.whl\n",
                    "--extra-index-url https://example.com\n",
                    "httpx>=0.28; python_version > '3.10'\n", "httpx\n",
                    "fastmcp==4\nidna --hash=xyz\n",
                    "httpx>=\n", "httpx=0.28\n", "httpx>=1 2\n"):
            with self.subTest(bad=bad):
                self.put("requirements.txt", bad)
                result = audit.audit(self.candidate, python=self.python)
                self.assertFalse(result["offline_checks_passed"])
                self.assertIn("historical dependency input needs manual review",
                              result["blockers"])

    def test_replaced_source_symlink_and_insecure_permissions_fail_closed(self):
        original = self.source / "bridge.py"
        approved = original.read_text()
        outside = self.home / "PRIVATE_SOURCE_SECRET.py"
        outside.write_text(approved)
        original.unlink()
        original.symlink_to(outside)
        with patch.object(audit, "_environment", return_value={
                "checked": 3, "satisfied": 3, "all_satisfied": True}):
            result = audit.audit(self.candidate, python=self.python)
        self.assertFalse(result["static_contracts_passed"])
        self.assertIn("historical Python contract unavailable", result["blockers"])
        original.unlink()
        self.put("bridge.py", approved)
        original.chmod(0o644)
        with patch.object(audit, "_environment", return_value={
                "checked": 3, "satisfied": 3, "all_satisfied": True}):
            result = audit.audit(self.candidate, python=self.python)
        self.assertFalse(result["static_contracts_passed"])
        self.assertIn("historical Python contract unavailable", result["blockers"])
        self.put("bridge.py", approved)
        with patch.object(audit, "_environment", return_value={
                "checked": 3, "satisfied": 3, "all_satisfied": True}):
            result = audit.audit(self.candidate, python=self.python)
        self.assertTrue(result["static_contracts_passed"])
        self.assertTrue(result["offline_checks_passed"])

    def test_dependency_environment_subprocess_is_isolated_no_install(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            self.assertEqual(args[:4], [str(self.python), "-I", "-B", "-c"])
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertNotIn("install", args)
            self.assertEqual(kwargs["timeout"], 20)
            self.assertEqual(kwargs["env"]["PYTHONNOUSERSITE"], "1")
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            self.assertEqual(len(json.loads(kwargs["input"])), 2)
            return subprocess.CompletedProcess(
                args, 0, stdout=b'{"checked":2,"satisfied":2,"all_satisfied":true}',
                stderr=b"PRIVATE_INTERNAL_PATH")
        with patch.object(audit.subprocess, "run", side_effect=runner):
            result = audit._environment(self.python, ["fastmcp>=4,<5", "idna>=3,<4"],
                                        self.candidate)
        self.assertTrue(result["all_satisfied"])
        self.assertEqual(len(calls), 1)

    def test_failed_dependency_checks_and_malformed_output_do_not_disclose(self):
        payloads = [
            subprocess.CompletedProcess([], 0, b'{"checked":3,"satisfied":3,"all_satisfied":true}'),
            subprocess.CompletedProcess([], 0, b'{"checked":2,"satisfied":1,"all_satisfied":true}'),
            subprocess.CompletedProcess([], 1, b"PRIVATE_EXE_OUTPUT"),
        ]
        for payload in payloads:
            with self.subTest(payload=payload.returncode), patch.object(
                    audit.subprocess, "run", return_value=payload):
                with self.assertRaises(ValueError):
                    audit._environment(self.python, ["fastmcp>=4,<5", "idna>=3,<4"],
                                       self.candidate)
        with (patch.object(audit, "audit", side_effect=ValueError(
                "PRIVATE_ARCHIVE_AND_HISTORY")),
              redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as error):
            self.assertEqual(audit.main([str(self.candidate)]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("PRIVATE_ARCHIVE", error.getvalue())
        self.assertNotIn(str(self.home), error.getvalue())

    def test_no_historical_code_is_executed(self):
        marker = self.home / "old-code-ran"
        self.put("history_log.py", "from pathlib import Path\n"
                 f"Path({str(marker)!r}).write_text('not allowed')\n"
                 "def logged_tool():\n    pass\n"
                 "def history_path():\n    pass\n"
                 "def append_history():\n    pass\n")
        with patch.object(audit, "_environment",
                          return_value={"checked": 3, "satisfied": 3,
                                        "all_satisfied": True}):
            result = audit.audit(self.candidate, python=self.python)
        self.assertTrue(result["offline_checks_passed"])
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
