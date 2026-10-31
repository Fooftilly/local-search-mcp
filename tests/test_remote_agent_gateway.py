"""Isolated gateway acceptance: no Docker, open port, tunnel, or credentials."""
import asyncio
import time
import unittest

import httpx

from remote_agent_gateway import LoopbackMCPGateway, synthetic_grant, UPSTREAM


class RemoteGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = [100.0]
        self.guard = [True]
        self.grants = [synthetic_grant("a" * 48, "client-a", 200.0)]
        self.upstream_calls = []
        self.upstream_status = [200]
        self.upstream_error = [False]

        def upstream_handler(request: httpx.Request) -> httpx.Response:
            if self.upstream_error[0]:
                raise httpx.ConnectError("upstream disconnected", request=request)
            self.upstream_calls.append(request)
            if request.method == "GET":
                return httpx.Response(
                    self.upstream_status[0],
                    content=b"event: message\ndata: ok\n\n",
                    headers={"content-type": "text/event-stream",
                             "mcp-session-id": "session-123",
                             "set-cookie": "private=not-for-client"})
            return httpx.Response(
                self.upstream_status[0],
                json={"jsonrpc": "2.0", "result": {"tools": []}, "id": 1},
                headers={"mcp-session-id": "session-123",
                         "set-cookie": "private=not-for-client"})

        self.upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream_handler), timeout=5.0)
        gateway = LoopbackMCPGateway(
            upstream=self.upstream,
            grants=lambda: tuple(self.grants),
            guard_ready=lambda: self.guard[0],
            clock=lambda: self.clock[0])
        self.proxy = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway),
            base_url="http://127.0.0.1:18790", timeout=5.0)
        self.addAsyncCleanup(self.proxy.aclose)
        self.addAsyncCleanup(self.upstream.aclose)

    def auth(self, token=None):
        return {"authorization": "Bearer " + (token or "a" * 48),
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                "mcp-protocol-version": "2025-06-18",
                "x-forwarded-host": "evil.example",
                "cookie": "secret=not-forwarded"}

    async def call(self, headers=None, *, method="POST", path="/mcp",
                   content=b'{"jsonrpc":"2.0","method":"tools/list","id":1}'):
        return await self.proxy.request(method, path,
                                        headers=headers or {}, content=content)

    async def test_authenticated_request_preserves_mcp_and_sanitizes_headers(self):
        r = await self.call(self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"]["tools"], [])
        self.assertEqual(r.headers["mcp-session-id"], "session-123")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(len(self.upstream_calls), 1)
        request = self.upstream_calls[0]
        self.assertEqual(str(request.url), UPSTREAM)
        self.assertEqual(request.headers["host"], "127.0.0.1:8790")
        self.assertEqual(request.headers["mcp-protocol-version"], "2025-06-18")
        for forbidden in ("authorization", "cookie", "x-forwarded-host",
                          "origin", "forwarded"):
            self.assertNotIn(forbidden, request.headers)

    async def test_missing_invalid_and_duplicate_credentials_do_not_forward(self):
        for headers in (
            {}, {"authorization": "Bearer short"},
            {"authorization": "Basic " + "a" * 48},
        ):
            with self.subTest(headers=headers):
                self.assertEqual((await self.call(headers)).status_code, 401)
        # Raw ASGI headers preserve duplicates.
        dup = [("authorization", "Bearer " + "a" * 48),
               ("authorization", "Bearer " + "a" * 48)]
        self.assertEqual((await self.call(dup)).status_code, 401)
        self.assertEqual(len(self.upstream_calls), 0)

    async def test_expiry_rotation_and_client_isolation(self):
        self.assertEqual((await self.call(self.auth("b" * 48))).status_code, 401)
        self.assertEqual((await self.call(self.auth())).status_code, 200)
        self.clock[0] = 201.0
        self.assertEqual((await self.call(self.auth())).status_code, 401)
        self.grants[:] = [synthetic_grant("b" * 48, "client-b", 300.0)]
        self.assertEqual((await self.call(self.auth())).status_code, 401)
        self.assertEqual((await self.call(self.auth("b" * 48))).status_code, 200)
        self.assertEqual(len(self.upstream_calls), 2)

    async def test_guard_unavailable_or_crashing_is_fail_closed(self):
        self.guard[0] = False
        self.assertEqual((await self.call(self.auth())).status_code, 503)
        self.assertFalse(self.upstream_calls)
        self.guard[0] = True
        self.assertEqual((await self.call(self.auth())).status_code, 200)
        self.assertEqual(len(self.upstream_calls), 1)

    async def test_no_open_proxy_or_dashboard_routes(self):
        for path in ("/", "/dashboard", "/mcp/", "/mcp?upstream=8791",
                     "/mcp/%2e%2e/dashboard", "http://127.0.0.1:8791/"):
            with self.subTest(path=path):
                response = await self.call(self.auth(), path=path)
                self.assertNotEqual(response.status_code, 200)
        self.assertFalse(self.upstream_calls)

    async def test_reject_external_host_and_browser_origin(self):
        for headers in (
            {**self.auth(), "host": "evil.example"},
            {**self.auth(), "origin": "https://evil.example"},
            {**self.auth(), "origin": "null"},
        ):
            with self.subTest(headers=headers.get("host") or headers.get("origin")):
                self.assertEqual((await self.call(headers)).status_code, 403)
        self.assertFalse(self.upstream_calls)

    async def test_request_size_and_method_limit(self):
        self.assertEqual((await self.call(self.auth(), content=b"x" * 131073)).status_code, 413)
        self.assertEqual((await self.call(self.auth(), method="PUT")).status_code, 405)
        self.assertEqual((await self.call(self.auth(), method="POST", content=b"")).status_code, 400)
        self.assertFalse(self.upstream_calls)

    async def test_sse_get_and_session_delete(self):
        get = await self.call(
            self.auth(), method="GET", content=b"")
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.headers["content-type"], "text/event-stream")
        self.assertIn(b"event: message", get.content)
        delete = await self.call(
            {**self.auth(), "mcp-session-id": "session-123"},
            method="DELETE", content=b"")
        self.assertEqual(delete.status_code, 200)
        self.assertEqual(self.upstream_calls[-1].headers["mcp-session-id"],
                         "session-123")

    async def test_upstream_redirect_does_not_escape_fixed_target(self):
        self.upstream_status[0] = 302
        self.assertEqual((await self.call(self.auth())).status_code, 502)
        self.assertEqual(len(self.upstream_calls), 1)

    async def test_inaccessible_upstream_returns_generic_gateway_error(self):
        self.upstream_error[0] = True
        result = await self.call(self.auth())
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("upstream disconnected", result.text)

    async def test_no_configuration_provider_is_accepted(self):
        with self.assertRaises(ValueError):
            LoopbackMCPGateway(
                upstream=self.upstream, grants=lambda: self.grants,
                guard_ready=None)


if __name__ == "__main__":
    unittest.main()
