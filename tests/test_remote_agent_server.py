"""Opt-in loopback gateway runner acceptance without binding TCP sockets."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from remote_agent_server import (
    LOCAL_HOST, LOCAL_PORT, StartupRefused, build_uvicorn_config,
    check_local_config, main, run_loopback,
)
from remote_agent_prerequisites import SCHEMA


class LoopbackCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "grants.json"
        self.token = "T" * 48
        self.write_grants([{
            "client_id": "grok-api",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            "expires_at": time.time() + 3600,
        }])

    def write_grants(self, grants):
        self.path.write_text(json.dumps({"schema": SCHEMA, "grants": grants}))
        self.path.chmod(0o600)

    def test_read_only_default_does_not_start_guard_or_server(self):
        output = io.StringIO()
        with patch("remote_agent_server.default_store_path", return_value=self.path), \
                patch("remote_agent_server.fixed_root_tunnel_gate",
                      side_effect=AssertionError("sudo must not run")), \
                patch("remote_agent_server.uvicorn.Server",
                      side_effect=AssertionError("no server may start")), \
                patch("sys.stdout", output):
            self.assertEqual(main([]), 0)
            self.assertEqual(main(["--check"]), 0)
        self.assertIn("nothing started", output.getvalue())
        self.assertEqual(check_local_config(self.path), 1)
        self.assertFalse(self.path.with_name("grants.json.lock").exists())

    def test_missing_expired_and_unsafe_grants_refuse(self):
        self.write_grants([])
        with self.assertRaises(StartupRefused):
            check_local_config(self.path)
        self.write_grants([{
            "client_id": "x", "token_sha256": hashlib.sha256(b"x").hexdigest(),
            "expires_at": time.time() - 10,
        }])
        with self.assertRaises(StartupRefused):
            check_local_config(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(StartupRefused):
            check_local_config(self.path)
        self.path.unlink()
        with self.assertRaises(StartupRefused):
            check_local_config(self.path)

    def test_cannot_set_host_port_worker_or_public_bind_from_cli(self):
        for flag in ("--host", "--port", "--workers", "--listen", "--public",
                     "--proxy-headers", "--forwarded-allow-ips"):
            with self.subTest(flag=flag), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    main([flag, "0.0.0.0", "--serve-loopback"])
                self.assertEqual(ctx.exception.code, 2)
        config = build_uvicorn_config(object())
        self.assertEqual(config.host, LOCAL_HOST)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, LOCAL_PORT)
        self.assertEqual(config.workers, 1)
        self.assertFalse(config.proxy_headers)
        self.assertFalse(config.access_log)
        self.assertFalse(config.server_header)
        self.assertFalse(config.date_header)
        self.assertEqual(config.lifespan, "off")
        self.assertEqual(config.ws, "none")
        self.assertEqual(config.limit_concurrency, 16)

    def test_abbreviated_flags_never_start_a_listener(self):
        for argv in (["--serve"], ["--ser"], ["--serve-loop"],
                     ["--che"]):
            with self.subTest(argv=argv), \
                    patch("sys.stderr", io.StringIO()), \
                    patch("remote_agent_server.asyncio.run",
                          side_effect=AssertionError("listener started")) as runner:
                with self.assertRaises(SystemExit) as failure:
                    main(argv)
                self.assertEqual(failure.exception.code, 2)
                runner.assert_not_called()

    def test_explicit_serve_flag_only_and_fail_closed_root(self):
        output = io.StringIO()
        with patch("remote_agent_server.default_store_path", return_value=self.path), \
                patch("remote_agent_server.asyncio.run",
                      side_effect=StartupRefused("Root-owned crawler egress guard is not ready")) as runner, \
                patch("sys.stderr", output):
            self.assertEqual(main(["--serve-loopback"]), 1)
            self.assertEqual(runner.call_count, 1)
        self.assertIn("Root-owned crawler egress guard", output.getvalue())

    def test_explicit_start_refused_as_root(self):
        with patch("remote_agent_server.os.geteuid", return_value=0):
            with self.assertRaises(StartupRefused):
                check_local_config(self.path)


class LoopbackStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "grants.json"
        self.token = "K" * 48
        self.path.write_text(json.dumps({
            "schema": SCHEMA,
            "grants": [{
                "client_id": "local-test",
                "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
                "expires_at": time.time() + 3600,
            }],
        }))
        self.path.chmod(0o600)

    async def test_guard_fails_before_listener_and_does_not_start_server(self):
        with patch("remote_agent_server.uvicorn.Server") as mock_server:
            with self.assertRaises(StartupRefused):
                await run_loopback(self.path, guard_probe=lambda: False)
        mock_server.assert_not_called()

    async def test_guard_exception_and_timeout_refuse_startup(self):
        def bad_guard():
            raise OSError("private guard failure")
        with patch("remote_agent_server.uvicorn.Server") as mock_server:
            with self.assertRaises(StartupRefused):
                await run_loopback(self.path, guard_probe=bad_guard)
        mock_server.assert_not_called()

        stop = asyncio.Event()
        async def await_guard():
            return None
        with patch("remote_agent_server.STARTUP_GUARD_SECONDS", 0.02), \
                patch("remote_agent_server.uvicorn.Server") as mock_server:
            with self.assertRaises(StartupRefused):
                await run_loopback(
                    self.path, guard_probe=lambda: __import__("time").sleep(0.10))
        mock_server.assert_not_called()

    async def test_authorized_loopback_runner_builds_one_secure_server(self):
        calls = []
        def guard():
            calls.append("guard")
            return True

        with patch("remote_agent_server.uvicorn.Server") as server_type:
            server = server_type.return_value
            server.serve = AsyncMock(return_value=None)
            await run_loopback(self.path, guard_probe=guard)
            server.serve.assert_awaited_once()
            server_type.assert_called_once()
            config = server_type.call_args.args[0]
            self.assertEqual(config.host, "127.0.0.1")
            self.assertEqual(config.port, 18790)
            self.assertEqual(config.workers, 1)
            self.assertEqual(config.app.__class__.__name__, "LoopbackMCPGateway")
        self.assertEqual(calls, ["guard"])

    async def test_preflight_revalidates_grants_and_no_server_for_empty_store(self):
        self.path.write_text(json.dumps({"schema": SCHEMA, "grants": []}))
        self.path.chmod(0o600)
        with patch("remote_agent_server.uvicorn.Server") as server_type:
            with self.assertRaises(StartupRefused):
                await run_loopback(
                    self.path, guard_probe=lambda: True)
        server_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
