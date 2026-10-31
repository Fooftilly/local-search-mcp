"""Isolated gateway acceptance: no Docker, open port, tunnel, or credentials."""
import asyncio
import json
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
        self.upstream_session = [False]
        self.silent_stream = [None]

        def upstream_handler(request: httpx.Request) -> httpx.Response:
            if self.upstream_error[0]:
                raise httpx.ConnectError("upstream disconnected", request=request)
            self.upstream_calls.append(request)
            if self.silent_stream[0] is not None:
                return httpx.Response(
                    200, stream=self.silent_stream[0],
                    headers={"content-type": "text/event-stream"})
            session_header = ({"mcp-session-id": "session-123"}
                              if self.upstream_session[0] else {})
            if request.method == "GET":
                return httpx.Response(
                    self.upstream_status[0],
                    content=b"event: message\ndata: ok\n\n",
                    headers={"content-type": "text/event-stream",
                             **session_header,
                             "set-cookie": "private=not-for-client"})
            return httpx.Response(
                self.upstream_status[0],
                json={"jsonrpc": "2.0", "result": {"tools": []}, "id": 1},
                headers={**session_header,
                         "set-cookie": "private=not-for-client"})

        self.upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream_handler), timeout=5.0)
        gateway = LoopbackMCPGateway(
            upstream=self.upstream,
            grants=lambda: tuple(self.grants),
            guard_ready=lambda: self.guard[0],
            clock=lambda: self.clock[0])
        self.gateway = gateway
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
        self.assertNotIn("mcp-session-id", r.headers)
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
        # A stateless MCP bridge can return its own DELETE status; the
        # gateway forwards the method, never an arbitrary client's session.
        delete = await self.call(self.auth(), method="DELETE", content=b"")
        self.assertEqual(delete.status_code, 200)
        self.assertNotIn("mcp-session-id", self.upstream_calls[-1].headers)

    async def test_upstream_redirect_does_not_escape_fixed_target(self):
        self.upstream_status[0] = 302
        self.assertEqual((await self.call(self.auth())).status_code, 502)
        self.assertEqual(len(self.upstream_calls), 1)

    async def test_inaccessible_upstream_returns_generic_gateway_error(self):
        self.upstream_error[0] = True
        result = await self.call(self.auth())
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("upstream disconnected", result.text)


    async def test_mcp_session_and_replay_identifiers_are_rejected(self):
        for header in ("mcp-session-id", "last-event-id"):
            with self.subTest(header=header):
                incoming = {**self.auth(), header: "other-client-session"}
                self.assertEqual((await self.call(incoming)).status_code, 400)
        self.assertFalse(self.upstream_calls)
        self.upstream_session[0] = True
        self.assertEqual((await self.call(self.auth())).status_code, 502)

    async def test_case_insensitive_bearer_and_bracketed_ipv6_authority(self):
        lower = self.auth()
        lower["authorization"] = "bearer " + "a" * 48
        self.assertEqual((await self.call(lower)).status_code, 200)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=self.gateway, client=("::1", 4271)),
            base_url="http://[::1]:18790", timeout=5.0
        ) as ipv6:
            response = await ipv6.post(
                "/mcp", headers=self.auth(),
                content=b'{"jsonrpc":"2.0","method":"tools/list","id":2}')
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.upstream_calls), 2)

    async def test_silent_sse_stream_disconnect_closes_upstream(self):
        class SilentStream(httpx.AsyncByteStream):
            def __init__(self):
                self.wait = asyncio.Event()
                self.closed = asyncio.Event()

            async def __aiter__(self):
                await self.wait.wait()
                yield b"never"

            async def aclose(self):
                self.closed.set()

        stream = SilentStream()
        self.silent_stream[0] = stream
        started = asyncio.Event()
        seen = []

        async def receive():
            if not seen:
                seen.append("body")
                return {"type": "http.request", "body": b"",
                        "more_body": False}
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            seen.append(message["type"])
            if message["type"] == "http.response.start":
                started.set()

        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/mcp", "raw_path": b"/mcp", "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:18790"),
                (b"authorization", b"Bearer " + b"a" * 48),
                (b"accept", b"text/event-stream"),
            ],
            "client": ("127.0.0.1", 4567), "server": ("127.0.0.1", 18790),
            "scheme": "http", "root_path": "",
        }
        await asyncio.wait_for(self.gateway(scope, receive, send), timeout=2)
        self.assertTrue(stream.closed.is_set())
        self.assertIn("http.response.start", seen)
        self.assertNotIn("http.response.body", seen)

    async def test_actual_stateless_fastmcp_tools_list(self):
        """Verify against the real bridge, not only a permissive test mock."""
        import bridge

        app = bridge.mcp.http_app(
            path="/mcp", stateless_http=True,
            host_origin_protection=True,
            allowed_hosts=bridge.MCP_ALLOWED_HOSTS,
            allowed_origins=bridge.MCP_ALLOWED_ORIGINS,
        )
        async with app.lifespan(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8790", timeout=5,
            ) as actual_upstream:
                gateway = LoopbackMCPGateway(
                    upstream=actual_upstream,
                    grants=lambda: tuple(self.grants),
                    guard_ready=lambda: True,
                    clock=lambda: self.clock[0],
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway),
                    base_url="http://127.0.0.1:18790", timeout=5,
                ) as client:
                    response = await client.post(
                        "/mcp", headers=self.auth(),
                        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
                    )
        self.assertEqual(response.status_code, 200)
        # The hosted-client gateway must preserve the full advertised
        # discovery contract, not merely return an HTTP 200 or two names.
        content_type = response.headers["content-type"].split(";", 1)[0]
        if content_type == "text/event-stream":
            # FastMCP may frame JSON-RPC results as SSE on Streamable HTTP.
            events = [line[5:].strip() for line in response.text.splitlines()
                      if line.startswith("data:")]
            self.assertEqual(len(events), 1)
            payload = json.loads(events[0])
        else:
            self.assertEqual(content_type, "application/json")
            payload = response.json()
        discovered = {tool["name"]: tool
                      for tool in payload["result"]["tools"]}
        expected = {
            "web_search", "web_search_capabilities", "web_search_batch",
            "web_fetch", "web_fetch_batch", "web_fetch_page",
            "web_table_page", "web_links_page", "web_find",
            "web_extract_section", "web_compare_documents", "web_metrics",
        }
        self.assertEqual(set(discovered), expected)
        for name, tool in discovered.items():
            with self.subTest(name=name):
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertIsInstance(
                    tool["inputSchema"].get("properties", {}), dict)
        self.assertIn("include_image_metadata",
                      discovered["web_search"]["inputSchema"]["properties"])
        self.assertIn("include_retrieval_info",
                      discovered["web_fetch"]["inputSchema"]["properties"])
        self.assertNotIn("mcp-session-id", response.headers)

    async def test_no_configuration_provider_is_accepted(self):
        with self.assertRaises(ValueError):
            LoopbackMCPGateway(
                upstream=self.upstream, grants=lambda: self.grants,
                guard_ready=None)


if __name__ == "__main__":
    unittest.main()
