"""Historical-reader sandbox regressions; never execute real staged code."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import rollback_runtime_smoke as runner


class HistoricalSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.candidate = self.home / "candidate"
        self.state = self.candidate / "state"
        self.state.mkdir(parents=True, mode=0o700)
        self.python = self.home / "private-old-venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("fixture interpreter")
        self.python.chmod(0o700)
        self.src = self.candidate / "source"
        self.src.mkdir(mode=0o700)
        self.put("domains.sqlite3", b"fake policy fixture bytes")
        self.put("history.jsonl", b'{"kind":"search","status":"ok"}\n')
        self.manifest()
        self.uid = patch.object(runner.os, "geteuid", return_value=1000)
        self.uid.start()
        self.addCleanup(self.uid.stop)

    def put(self, name: str, value: bytes):
        p = self.state / name
        p.write_bytes(value)
        p.chmod(0o600)

    def manifest(self):
        inventory = {}
        for p in self.state.iterdir():
            if p.is_file() and not p.is_symlink():
                inventory["state/" + p.name] = (
                    "F:0600:" + hashlib.sha256(p.read_bytes()).hexdigest())
        metadata = self.candidate / "candidate.json"
        metadata.write_text(json.dumps({"inventory": inventory}))
        metadata.chmod(0o600)

    def test_copy_state_is_disposable_private_and_exact(self):
        scratch = self.home / "scratch"
        scratch.mkdir(mode=0o700)
        count = runner._copy_candidate_state(self.candidate, scratch)
        self.assertEqual(count, 2)
        policy = scratch / "state" / "local-search-mcp" / "domains.sqlite3"
        history = scratch / "state" / "local-search-mcp" / "history.jsonl"
        self.assertEqual(policy.read_bytes(), (self.state / policy.name).read_bytes())
        self.assertEqual(history.read_bytes(), (self.state / history.name).read_bytes())
        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(policy.parent.stat().st_mode), 0o700)
        self.assertFalse((scratch / "config" / "local-search-mcp" / "config.json").exists())

    def test_copy_settings_relocates_into_isolated_config(self):
        self.put("history-settings.json", b'{"schema":"test","settings":{}}\n')
        self.manifest()
        scratch = self.home / "scratch"
        scratch.mkdir(mode=0o700)
        runner._copy_candidate_state(self.candidate, scratch)
        self.assertTrue((scratch / "config" / "local-search-mcp" /
                         "history-settings.json").exists())
        self.assertFalse((scratch / "state" / "local-search-mcp" /
                          "history-settings.json").exists())

    def test_changed_private_source_and_symlink_fail_closed(self):
        scratch = self.home / "scratch"
        scratch.mkdir(mode=0o700)
        self.put("domains.sqlite3", b"tampered")
        with self.assertRaisesRegex(ValueError, "digest"):
            runner._copy_candidate_state(self.candidate, scratch)
        self.put("domains.sqlite3", b"fake policy fixture bytes")
        (self.state / "history.jsonl").unlink()
        (self.state / "history.jsonl").symlink_to(self.home / "PRIVATE_CREDS")
        with self.assertRaises((OSError, ValueError)):
            runner._copy_candidate_state(self.candidate, scratch)

    def test_unverified_archive_never_starts_sandbox(self):
        with (patch.object(runner.rollback_release_audit, "audit",
                           return_value={"offline_checks_passed": False}),
              patch.object(runner.subprocess, "run",
                           side_effect=AssertionError("must not start")),
              patch.object(runner, "_copy_candidate_state",
                           side_effect=AssertionError("must not copy"))):
            with self.assertRaisesRegex(ValueError, "did not pass"):
                runner.run(self.candidate, python=self.python)

    def test_command_has_zero_host_credentials_network_or_mutations(self):
        scratch = self.home / "scratch"
        c = runner._sandbox_command
        with patch.object(runner, "_trusted_bwrap", return_value=Path("/usr/bin/bwrap")):
            cmd = c(self.candidate, self.python, scratch)
        self.assertEqual(cmd[0], "/usr/bin/bwrap")
        for critical in ("--unshare-all", "--clearenv", "--die-with-parent",
                         "--new-session", "--ro-bind", "--setenv", "--tmpfs"):
            self.assertIn(critical, cmd)
        self.assertIn(str(self.src), cmd)
        self.assertIn(str(scratch), cmd)
        self.assertNotIn("/home", cmd)
        self.assertNotIn("/etc", cmd)
        self.assertFalse(any(x in cmd for x in ("--share-net", "--bind-try",
                                               "--cap-add", "--ro-bind-data")))
        self.assertEqual(cmd[-4:-1], ["-B", "-c", runner.WORKER][-3:])

    def test_run_uses_sandbox_only_and_never_passes_private_environment(self):
        payload = {
            "historical_policy_reader": True,
            "historical_history_reader": True,
            "recent_history_count": 1,
        }
        expected = json.dumps(payload).encode()
        calls = []
        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(kwargs["timeout"], 40)
            self.assertNotIn("HOME", kwargs["env"])
            self.assertNotIn("HTTP_PROXY", kwargs["env"])
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin"})
            kwargs["stdout"].write(expected)
            return subprocess.CompletedProcess(command, 0)
        with (patch.object(runner.rollback_release_audit, "audit",
                           return_value={"offline_checks_passed": True}) as gate,
              patch.object(runner.rollback_candidate, "inspect") as verify,
              patch.object(runner, "_trusted_bwrap", return_value=Path("/usr/bin/bwrap")),
              patch.object(runner.subprocess, "run", side_effect=fake_run)):
            with patch.dict(os.environ, {"HTTP_PROXY": "http://secret.invalid:80",
                                        "LOCAL_SEARCH_MCP_TUNNEL_TOKEN": "PRIVATE-TOKEN"}):
                result = runner.run(self.candidate, python=self.python)
        self.assertTrue(result["sandboxed_old_state_readers_passed"])
        self.assertFalse(result["ready_for_live_use"])
        self.assertFalse(result["credentials_provided"])
        self.assertFalse(result["network_access_enabled"])
        self.assertFalse(result["live_state_changed"])
        self.assertEqual(result["staged_files"], 2)
        self.assertEqual(gate.call_count, 1)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(len(calls), 1)

    def test_crashed_or_malformed_sandbox_never_reports_ready(self):
        results = [
            (1, b"PRIVATE_ERROR"),
            (0, b"not JSON"),
            (0, b'{"historical_policy_reader":true,"historical_history_reader":true,"recent_history_count":1,"token":"LEAK"}'),
            (0, b'{"historical_policy_reader":true,"historical_history_reader":false,"recent_history_count":0}'),
            (0, b'{"historical_policy_reader":true,"historical_history_reader":true,"recent_history_count":99}'),
        ]
        for rc, raw in results:
            with self.subTest(rc=rc, raw=raw[:30]):
                def launch(args, **kwargs):
                    kwargs["stdout"].write(raw)
                    return subprocess.CompletedProcess(args, rc)
                with (patch.object(runner.rollback_release_audit, "audit",
                                   return_value={"offline_checks_passed": True}),
                      patch.object(runner.rollback_candidate, "inspect"),
                      patch.object(runner, "_trusted_bwrap", return_value=Path("/usr/bin/bwrap")),
                      patch.object(runner.subprocess, "run", side_effect=launch)):
                    with self.assertRaises(ValueError):
                        runner.run(self.candidate, python=self.python)

    def test_cli_redacts_failure_and_never_prints_candidate_paths(self):
        with (patch.object(runner, "run", side_effect=ValueError("SECRET private backup")),
              redirect_stdout(io.StringIO()) as out,
              redirect_stderr(io.StringIO()) as err):
            self.assertEqual(runner.main([str(self.candidate),
                                         "--python", str(self.python)]), 1)
        self.assertFalse(out.getvalue())
        self.assertNotIn("SECRET", err.getvalue())
        self.assertNotIn(str(self.home), err.getvalue())


if __name__ == "__main__":
    unittest.main()
