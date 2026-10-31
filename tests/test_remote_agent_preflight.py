"""Safe preflight checks for remote-agent gateway; no real host changes.

No installed sudo guard, public listener, credentials, SearXNG, Docker or
network request is used. Real MCP discovery is mocked at HTTPX transport.
"""
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
    MAX_DISCOVERY_BYTES, MCP_DISCOVERY_URL, REQUIRED_MCP_TOOLS,
    StartupRefused, main, preflight_local,
)
from remote_agent_prerequisites import SCHEMA


class ReadonlyPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._close_temp)
        self.path = Path(self.tmp.name) / "grants.json"
        self._store([{
            "client_id": "grok-test",
            "token_sha256": hashlib.sha256(b"synthetic").hexdigest(),
            "expires_at": time.time() + 3600,
        }])
        self.queries = []
        self.guards = []

    async def _close_temp(self):
        self.tmp.cleanup()

    def _store(self, grants):
        self.path.write_text(json.dumps({"schema": SCHEMA, "grants": grants}))
        self.path.chmod(0o600)

    def _guard(self):
        self.guards.append("guard")
        return True

    def _mcp_reply(self):
        return {"jsonrpc": "2.0", "id": 1,
                "result": {"tools": [{"name": name, "inputSchema": {
                    "type": "object", "properties": {}}}
                    for name in sorted(REQUIRED_MCP_TOOLS)]}}

    def _mock_transport(self, response):
        def handler(request):
            self.queries.append(request)
            return response
        return httpx.MockTransport(handler)

    async def test_valid_inventory_checks_guard_and_fixed_local_mcp_only(self):
        transport = self._mock_transport(httpx.Response(
            200, json=self._mcp_reply()))
        self.assertEqual(await preflight_local(
            self.path, guard_probe=self._guard, transport=transport), 1)
        self.assertEqual(self.guards, ["guard"])
        self.assertEqual(len(self.queries), 1)
        req = self.queries[0]
        self.assertEqual(str(req.url), MCP_DISCOVERY_URL)
        self.assertEqual(req.url.host, "127.0.0.1")
        self.assertEqual(req.headers["host"], "127.0.0.1:8790")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.headers["accept"],
                         "application/json, text/event-stream")
        self.assertEqual(req.headers["mcp-protocol-version"], "2025-06-18")
        self.assertEqual(req.extensions.get("timeout")["connect"], 3.0)
        self.assertEqual(req.headers.get("authorization"), None)
        self.assertEqual(req.headers.get("cookie"), None)
        self.assertEqual(req.headers.get("x-forwarded-host"), None)
        self.assertEqual(req.content, b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')

    async def test_streamable_sse_inventory_is_accepted(self):
        message = json.dumps(self._mcp_reply(), separators=(",", ":"))
        transport = self._mock_transport(httpx.Response(
            200, headers={"content-type": "text/event-stream; charset=utf-8"},
            content=("event: message\r\ndata: " + message +
                     "\r\n\r\n").encode()))
        self.assertEqual(await preflight_local(
            self.path, guard_probe=self._guard, transport=transport), 1)
        self.assertEqual(len(self.queries), 1)

    async def test_bad_credentials_or_guard_never_contact_local_mcp(self):
        network = self._mock_transport(httpx.Response(
            200, json=self._mcp_reply()))
        self._store([])
        with self.assertRaises(StartupRefused):
            await preflight_local(self.path, guard_probe=self._guard,
                                  transport=network)
        self.assertEqual(self.guards, [])
        self.assertFalse(self.queries)
        self._store([{
            "client_id": "grok-test",
            "token_sha256": hashlib.sha256(b"synthetic").hexdigest(),
            "expires_at": time.time() + 3600,
        }])
        with self.assertRaisesRegex(StartupRefused, "guard"):
            await preflight_local(self.path, guard_probe=lambda: False,
                                  transport=network)
        self.assertFalse(self.queries)

    async def test_missing_tools_redirect_bad_json_and_oversize_fail_closed(self):
        cases = [
            httpx.Response(302, headers={"location": "http://127.0.0.1:8791/"}),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                       "result": {"tools": []}}),
            httpx.Response(200, headers={"content-type": "application/json"},
                           content=b"{not json"),
            httpx.Response(200, headers={"content-type": "text/html"},
                           content=b"<html/>"),
            httpx.Response(200, headers={"content-type": "application/json"},
                           content=b"0" * (MAX_DISCOVERY_BYTES + 1)),
        ]
        for response in cases:
            with self.subTest(status=response.status_code):
                self.queries.clear()
                with self.assertRaises(StartupRefused):
                    await preflight_local(
                        self.path, guard_probe=self._guard,
                        transport=self._mock_transport(response))
                self.assertEqual(len(self.queries), 1)

    async def test_guard_exception_and_timeout_fail_closed(self):
        network = self._mock_transport(httpx.Response(
            200, json=self._mcp_reply()))
        def broken_guard():
            raise OSError("private message not for CLI")
        with self.assertRaisesRegex(StartupRefused, "guard"):
            await preflight_local(self.path, guard_probe=broken_guard,
                                  transport=network)
        with patch("remote_agent_server.STARTUP_GUARD_SECONDS", 0.02):
            with self.assertRaisesRegex(StartupRefused, "guard"):
                await preflight_local(
                    self.path, guard_probe=lambda: time.sleep(0.09),
                    transport=network)
        self.assertFalse(self.queries)


class PreflightCLIIsolationTests(unittest.TestCase):
    def test_explicit_flag_only_and_no_server(self):
        output = io.StringIO()
        with patch("remote_agent_server.default_store_path",
                   return_value=Path("/tmp/not-actually-read")), \
                patch("remote_agent_server.preflight_local",
                      new_callable=AsyncMock, return_value=2) as mocked, \
                patch("remote_agent_server.uvicorn.Server",
                      side_effect=AssertionError("no server binding")), \
                patch("sys.stdout", output):
            self.assertEqual(main(["--preflight"]), 0)
            mocked.assert_awaited_once()
        self.assertIn("2 active client grant(s)", output.getvalue())
        self.assertIn("Nothing started or changed", output.getvalue())

    def test_preflight_and_serve_cannot_coexist(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--preflight", "--serve-loopback"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
