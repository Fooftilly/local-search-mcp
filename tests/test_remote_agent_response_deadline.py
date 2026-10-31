"""Absolute upstream header deadline for the import-only guarded MCP gateway.

No real listener, network, credentials, Docker or root guard is used.
"""
import asyncio
import time
import unittest
from unittest.mock import patch

import httpx

from remote_agent_gateway import LoopbackMCPGateway, synthetic_grant


class ResponseHeaderDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = "a" * 48
        self.grants = [synthetic_grant(
            self.token, "client-test", time.time() + 3600)]
        self.guard_count = [0]
        self.upstream = None
        self.proxy = None

    async def make_client(self, transport):
        def guard():
            self.guard_count[0] += 1
            return True
        self.upstream = httpx.AsyncClient(
            transport=transport, trust_env=False, timeout=5)
        self.addAsyncCleanup(self.upstream.aclose)
        app = LoopbackMCPGateway(
            upstream=self.upstream, grants=lambda: tuple(self.grants),
            guard_ready=guard)
        self.proxy = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:18790", timeout=3)
        self.addAsyncCleanup(self.proxy.aclose)
        return self.proxy

    def auth(self):
        return {"authorization": "Bearer " + self.token}

    async def test_silent_response_headers_timeout_and_cancel_upstream(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def header_stall(request):
            entered.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return httpx.Response(200, json={"unexpected": True})

        client = await self.make_client(httpx.MockTransport(header_stall))
        with patch("remote_agent_gateway.MAX_RESPONSE_START_SECONDS", 0.025):
            result = await asyncio.wait_for(
                client.post("/mcp", headers=self.auth(),
                            json={"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/list"}), timeout=1)
        self.assertTrue(entered.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result.status_code, 504)
        self.assertEqual(result.text, "Request rejected")
        self.assertNotIn(self.token, result.text)
        self.assertEqual(self.guard_count[0], 1)

    async def test_header_timeout_does_not_truncate_slow_sse_body(self):
        class SlowBody(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False

            async def __aiter__(self):
                await asyncio.sleep(0.06)
                yield b"event: message\ndata: accepted\n\n"

            async def aclose(self):
                self.closed = True

        stream = SlowBody()
        seen = []

        async def handler(request):
            seen.append(request)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=stream)

        client = await self.make_client(httpx.MockTransport(handler))
        with patch("remote_agent_gateway.MAX_RESPONSE_START_SECONDS", 0.025):
            response = await asyncio.wait_for(
                client.get("/mcp", headers=self.auth()), timeout=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"],
                         "text/event-stream")
        self.assertIn(b"data: accepted", response.content)
        self.assertEqual(len(seen), 1)
        self.assertTrue(stream.closed)

    async def test_one_byte_at_a_time_response_headers_have_total_deadline(self):
        """Real loopback HTTP parsing: each byte arrives before read timeout."""
        accepted = asyncio.Event()
        closed = asyncio.Event()

        async def slow_header_server(reader, writer):
            accepted.set()
            try:
                await reader.readuntil(b"\r\n\r\n")
                # HTTP status arrives promptly; incomplete headers follow
                # in tiny increments. No single read exceeds 200 ms.
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: ")
                await writer.drain()
                for char in b"text/plain\r\nContent-Length: 0\r\n\r\n":
                    writer.write(bytes((char,)))
                    await writer.drain()
                    await asyncio.sleep(0.025)
            except (ConnectionResetError, BrokenPipeError,
                    asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                closed.set()

        server = await asyncio.start_server(
            slow_header_server, host="127.0.0.1", port=0)
        try:
            port = server.sockets[0].getsockname()[1]
            endpoint = f"http://127.0.0.1:{port}/mcp"
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(2, read=0.2),
            ) as upstream:
                app = LoopbackMCPGateway(
                    upstream=upstream,
                    grants=lambda: tuple(self.grants),
                    guard_ready=lambda: True)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:18790",
                    timeout=2,
                ) as client:
                    with patch("remote_agent_gateway.UPSTREAM", endpoint), \
                            patch("remote_agent_gateway.MAX_RESPONSE_START_SECONDS",
                                  0.12):
                        result = await asyncio.wait_for(
                            client.post("/mcp", headers=self.auth(),
                                json={"jsonrpc": "2.0", "id": 1,
                                      "method": "tools/list"}), timeout=2)
            self.assertTrue(accepted.is_set())
            self.assertEqual(result.status_code, 504)
            self.assertEqual(result.text, "Request rejected")
            self.assertNotIn(self.token, result.text)
            await asyncio.wait_for(closed.wait(), timeout=2)
        finally:
            server.close()
            await server.wait_closed()

    async def test_immediate_upstream_http_error_remains_502(self):
        async def failing(request):
            raise httpx.ConnectError("private upstream info", request=request)

        client = await self.make_client(httpx.MockTransport(failing))
        with patch("remote_agent_gateway.MAX_RESPONSE_START_SECONDS", 0.025):
            response = await client.post(
                "/mcp", headers=self.auth(),
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private upstream", response.text)


if __name__ == "__main__":
    unittest.main()
