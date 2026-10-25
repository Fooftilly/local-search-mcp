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
import sys
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
        other = self.home / "scratch-2"
        other.mkdir(mode=0o700)
        with self.assertRaises((OSError, ValueError)):
            runner._copy_candidate_state(self.candidate, other)

    def test_unverified_archive_never_starts_sandbox(self):
        with (patch.object(runner.rollback_release_audit, "audit",
                           return_value={"static_contracts_passed": False,
                                         "blockers": ["historical Python contract missing"]}),
              patch.object(runner, "_sandbox_output",
                           side_effect=AssertionError("must not start")),
              patch.object(runner, "_copy_candidate_state",
                           side_effect=AssertionError("must not copy"))):
            with self.assertRaisesRegex(ValueError, "did not pass"):
                runner.run(self.candidate, python=self.python)

    def test_command_has_zero_host_credentials_network_or_mutations(self):
        scratch = self.home / "scratch"
        c = runner._sandbox_command
        with patch.object(runner, "_trusted_bwrap", return_value=Path("/usr/bin/bwrap")):
            cmd = c(self.candidate, self.python, scratch, ["httpx>=0.28,<1"])
        self.assertEqual(cmd[0], "/usr/bin/bwrap")
        for critical in ("--unshare-all", "--clearenv", "--die-with-parent",
                         "--new-session", "--ro-bind", "--setenv", "--tmpfs"):
            self.assertIn(critical, cmd)
        self.assertIn(str(self.src), cmd)
        self.assertIn(str(scratch), cmd)
        self.assertTrue(any(cmd[i:i + 3] ==
                            ["--ro-bind", str(self.src), "/app"]
                            for i in range(len(cmd) - 2)))
        self.assertTrue(any(cmd[i:i + 3] ==
                            ["--ro-bind", str(self.python.parent.parent), "/venv"]
                            for i in range(len(cmd) - 2)))
        self.assertTrue(any(cmd[i:i + 3] ==
                            ["--ro-bind", str(scratch), "/input"]
                            for i in range(len(cmd) - 2)))
        self.assertNotIn("--bind", cmd)  # No host directory mounted writable
        self.assertTrue(any(cmd[i:i + 6] == [
                            "--size", str(512 * 1024 * 1024),
                            "--perms", "0700", "--tmpfs", "/work"]
                            for i in range(len(cmd) - 5)))
        self.assertTrue(any(cmd[i:i + 6] == [
                            "--size", str(16 * 1024 * 1024),
                            "--perms", "0700", "--tmpfs", "/tmp"]
                            for i in range(len(cmd) - 5)))
        self.assertNotIn("/home", cmd)
        self.assertNotIn("/etc", cmd)
        self.assertFalse(any(x in cmd for x in ("--share-net", "--bind-try",
                                               "--cap-add", "--ro-bind-data")))
        self.assertEqual(cmd[-5:-1], ["-I", "-B", "-c", runner.WORKER])
        self.assertEqual(json.loads(cmd[-1]), ["httpx>=0.28,<1"])

    def test_run_uses_sandbox_only_and_never_passes_private_environment(self):
        payload = {
            "historical_dependency_versions": True,
            "historical_policy_reader": True,
            "historical_history_reader": True,
            "recent_history_count": 1,
        }
        calls = []
        def fake_output(command):
            calls.append(command)
            self.assertEqual(command[0], "/usr/bin/bwrap")
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertIn("--ro-bind", command)
            self.assertNotIn("PRIVATE-TOKEN", command)
            self.assertEqual(json.loads(command[-1]), ["httpx>=0.28,<1"])
            return json.dumps(payload).encode()
        static = {"static_contracts_passed": True,
                  "blockers": ["independent historical Python environment not provided"]}
        with (patch.object(runner.rollback_release_audit, "audit",
                           return_value=static) as gate,
              patch.object(runner.rollback_release_audit, "_requirements",
                           return_value=["httpx>=0.28,<1"]),
              patch.object(runner.rollback_release_audit, "_venv_layout"),
              patch.object(runner.rollback_candidate, "inspect") as verify,
              patch.object(runner, "_trusted_bwrap", return_value=Path("/usr/bin/bwrap")),
              patch.object(runner, "_sandbox_output", side_effect=fake_output)):
            with patch.dict(os.environ, {"HTTP_PROXY": "http://secret.invalid:80",
                                        "LOCAL_SEARCH_MCP_TUNNEL_TOKEN": "PRIVATE-TOKEN"}):
                result = runner.run(self.candidate, python=self.python)
        self.assertTrue(result["sandboxed_old_state_readers_passed"])
        self.assertTrue(result["sandboxed_dependency_versions_passed"])
        self.assertFalse(result["ready_for_live_use"])
        self.assertFalse(result["credentials_provided"])
        self.assertFalse(result["network_access_enabled"])
        self.assertFalse(result["live_state_changed"])
        self.assertEqual(result["staged_files"], 2)
        self.assertEqual(gate.call_args.kwargs, {"python": None})
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(len(calls), 1)

    def test_crashed_or_malformed_sandbox_never_reports_ready(self):
        results = [
            b"PRIVATE_ERROR",
            b"not JSON",
            b'{"historical_dependency_versions":true,"historical_policy_reader":true,"historical_history_reader":true,"recent_history_count":1,"token":"LEAK"}',
            b'{"historical_dependency_versions":false,"historical_policy_reader":true,"historical_history_reader":true,"recent_history_count":0}',
            b'{"historical_dependency_versions":true,"historical_policy_reader":true,"historical_history_reader":false,"recent_history_count":0}',
            b'{"historical_dependency_versions":true,"historical_policy_reader":true,"historical_history_reader":true,"recent_history_count":99}',
        ]
        static = {"static_contracts_passed": True,
                  "blockers": ["independent historical Python environment not provided"]}
        for raw in results:
            with self.subTest(raw=raw[:30]):
                with (patch.object(runner.rollback_release_audit, "audit",
                                   return_value=static),
                      patch.object(runner.rollback_release_audit, "_requirements",
                                   return_value=["httpx>=0.28,<1"]),
                      patch.object(runner.rollback_release_audit, "_venv_layout"),
                      patch.object(runner.rollback_candidate, "inspect"),
                      patch.object(runner, "_trusted_bwrap", return_value=Path("/usr/bin/bwrap")),
                      patch.object(runner, "_sandbox_output", return_value=raw)):
                    with self.assertRaises(ValueError):
                        runner.run(self.candidate, python=self.python)

    def test_bounded_capture_does_not_restrict_sqlite_file_writes(self):
        # Only trusted fixture Python is run here, never a staged interpreter.
        output = runner._sandbox_output([
            sys.executable, "-c", "import sys; print('hello')"])
        self.assertEqual(output, b"hello\n")
        with self.assertRaisesRegex(ValueError, "oversized output"):
            runner._sandbox_output([
                sys.executable, "-c", "import sys; sys.stdout.write('x'*10000)"])

    def test_subprocess_crash_is_rejected_without_disclosing_stderr(self):
        # Run a trusted fixture, not a staged historical interpreter.
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_TUNNEL_TOKEN": "TOP_SECRET"}):
            with self.assertRaisesRegex(ValueError, "exited unsuccessfully") as caught:
                runner._sandbox_output([
                    sys.executable, "-c",
                    "import sys; sys.stderr.write('TOP_SECRET'); sys.exit(19)"])
        self.assertNotIn("TOP_SECRET", str(caught.exception))
        self.assertNotIn("LOCAL_SEARCH_MCP_TUNNEL_TOKEN", str(caught.exception))

    def test_sandbox_child_environment_excludes_host_secrets(self):
        code = ("import json,os; print(json.dumps(dict(os.environ), "
                "sort_keys=True))")
        with patch.dict(os.environ, {
                "HOME": "/tmp/private-home", "HTTP_PROXY": "http://private",
                "HTTPS_PROXY": "http://private", "PYTHONPATH": "/tmp/inject",
                "LOCAL_SEARCH_MCP_TUNNEL_TOKEN": "TOP_SECRET",
                "BWRAP_TEST_SECRET": "TOP_SECRET"}):
            raw = runner._sandbox_output([sys.executable, "-c", code])
        env = json.loads(raw)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        # Python may add a fixed LC_CTYPE=C.UTF-8 through locale coercion;
        # that is not an inherited host secret.
        self.assertFalse(set(env) - {"PATH", "LC_CTYPE"})
        self.assertNotIn("TOP_SECRET", raw.decode())

    def test_worker_validates_dependencies_before_inserting_untrusted_source(self):
        code = runner.WORKER
        self.assertLess(code.index("metadata.version(req.name)"),
                        code.index("sys.path.insert(0, '/app')"))
        self.assertLess(code.index("from packaging.requirements"),
                        code.index("sys.path.insert(0, '/app')"))
        self.assertLess(code.index("shutil.copyfileobj"),
                        code.index("import domain_policy"))
        self.assertIn("Path('/input')", code)
        self.assertIn("Path('/work')", code)

    def test_sqlite_can_write_real_multi_page_index_under_child_limits(self):
        # A real trusted child writes >2 KiB to the isolated test fixture DB.
        # RLIMIT_FSIZE must not inherit stdout's small capture cap.
        db = self.home / "trusted-index.sqlite3"
        script = ("import sqlite3,sys; "
                  "c=sqlite3.connect(sys.argv[1]); "
                  "c.execute('CREATE TABLE rows (payload BLOB)'); "
                  "c.execute('INSERT INTO rows VALUES (?)', (b'X'*8192,)); "
                  "c.commit(); c.close(); print('ok')")
        self.assertEqual(runner._sandbox_output([
            sys.executable, "-c", script, str(db)]), b"ok\n")
        self.assertGreater(db.stat().st_size, runner._LIMIT)

    def test_lib64_loader_uses_only_trusted_system_directory(self):
        args = runner._lib64_args()
        self.assertIn(args[0], ("--symlink", "--ro-bind"))
        self.assertEqual(args[-1], "/lib64")
        if args[0] == "--symlink":
            self.assertIn(args[1], ("usr/lib", "usr/lib64"))

    def test_history_worker_checks_dependency_metadata_inside_sandbox(self):
        # Regression: do not run audit(candidate, python=historical_python),
        # which would execute the untrusted interpreter on the host.
        static = {"static_contracts_passed": True,
                  "blockers": ["independent historical Python environment not provided"]}
        with (patch.object(runner.rollback_release_audit, "audit",
                           return_value=static) as check,
              patch.object(runner.rollback_release_audit, "_requirements",
                           return_value=["httpx>=0.28,<1"]),
              patch.object(runner.rollback_release_audit, "_venv_layout"),
              patch.object(runner.rollback_candidate, "inspect"),
              patch.object(runner, "_trusted_bwrap", return_value=Path("/usr/bin/bwrap")),
              patch.object(runner, "_sandbox_output", return_value=json.dumps({
                  "historical_dependency_versions": True,
                  "historical_policy_reader": True,
                  "historical_history_reader": True,
                  "recent_history_count": 0}).encode())):
            runner.run(self.candidate, python=self.python)
        check.assert_called_once_with(self.candidate, python=None)

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
