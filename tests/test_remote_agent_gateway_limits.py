"""In-memory security acceptance for gateway quotas and active revocation.

Only mock ASGI and upstream transports are used; no public listener, Docker,
network tunnel, operator credentials, or firewall operations.
"""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from remote_agent_gateway import (
    ClientRequestBudget, LoopbackMCPGateway, MAX_PER_CLIENT_PER_MINUTE,
    MAX_TOTAL_ACTIVE, MAX_PER_CLIENT_ACTIVE, MAX_CACHED_IDENTITIES,
    MAX_GRANTS, synthetic_grant,
)


class RequestBudgetTests(unittest.TestCase):
    def test_per_client_concurrency_and_global_caps(self):
        b = ClientRequestBudget()
        self.assertTrue(b.reserve("a", 1.0))
        self.assertTrue(b.reserve("a", 1.0))
        self.assertFalse(b.reserve("a", 1.0))
        for i in range(MAX_TOTAL_ACTIVE - MAX_PER_CLIENT_ACTIVE):
            self.assertTrue(b.reserve("b" + str(i // MAX_PER_CLIENT_ACTIVE), 1.0))
        self.assertFalse(b.reserve("other", 1.0))
        b.release("a")
        self.assertTrue(b.reserve("other", 1.0))
        self.assertEqual(b._active_total, MAX_TOTAL_ACTIVE)
        for k, count in list(b._active.items()):
            for _ in range(count):
                b.release(k)
        self.assertEqual(b._active_total, 0)

    def test_window_and_identity_storage_are_bounded(self):
        b = ClientRequestBudget()
        for i in range(MAX_PER_CLIENT_PER_MINUTE):
            self.assertTrue(b.reserve("a", 100.0))
            b.release("a")
        self.assertFalse(b.reserve("a", 100.0))
        self.assertTrue(b.reserve("b", 100.0))
        b.release("b")
        self.assertTrue(b.reserve("a", 161.0))
        b.release("a")
        self.assertEqual(set(b._calls), {"a"})
        self.assertEqual(len(b._calls["a"]), 1)

    def test_grant_rotation_preserves_history_without_lockout(self):
        b = ClientRequestBudget()
        for i in range(MAX_GRANTS):
            client_id = "client-" + str(i)
            self.assertTrue(b.reserve(client_id, 100.0))
            b.release(client_id)

        # Authorized grant slot 8 can be replaced, but the previous
        # client's recent rate history must not prevent the replacement.
        self.assertTrue(b.reserve("replacement", 100.0))
        b.release("replacement")
        self.assertEqual(len(b._calls), MAX_GRANTS + 1)

        # Reinstating the original identity does not reset its quota.
        for _ in range(MAX_PER_CLIENT_PER_MINUTE - 1):
            self.assertTrue(b.reserve("client-7", 100.0))
            b.release("client-7")
        self.assertFalse(b.reserve("client-7", 100.0))

    def test_history_cache_has_independent_finite_cap(self):
        b = ClientRequestBudget()
        for i in range(MAX_CACHED_IDENTITIES):
            key = "historical-" + str(i)
            self.assertTrue(b.reserve(key, 100.0))
            b.release(key)
        self.assertFalse(b.reserve("too-many-rotations", 100.0))
        self.assertTrue(b.reserve("too-many-rotations", 161.0))
        b.release("too-many-rotations")
        self.assertEqual(len(b._calls), 1)

    def test_invalid_release_does_not_inflate_available_slots(self):
        b = ClientRequestBudget()
        with self.assertRaises(RuntimeError):
            b.release("unknown")
        self.assertEqual(b._active_total, 0)


class GatewayLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.current_time = [100.0]
        self.grants = [synthetic_grant("a" * 48, "client-a", 300)]
        self.calls = []
        self.stream = [None]

        def handler(request):
            self.calls.append(request)
            if self.stream[0] is not None:
                return httpx.Response(200, stream=self.stream[0],
                                      headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {"tools": []}})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.gateway = LoopbackMCPGateway(
            upstream=self.upstream,
            grants=lambda: tuple(self.grants),
            guard_ready=lambda: True,
            clock=lambda: self.current_time[0],
            rate_clock=lambda: self.current_time[0])
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.gateway),
            base_url="http://127.0.0.1:18790")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.upstream.aclose)

    def auth(self, token="a" * 48):
        return {"authorization": "Bearer " + token,
                "accept": "application/json, text/event-stream"}

    async def post(self, token="a" * 48):
        return await self.client.post("/mcp", headers=self.auth(token),
                                      json={"jsonrpc": "2.0", "id": 1,
                                            "method": "tools/list"})

    async def test_admission_rate_limit_prevents_upstream_work(self):
        for _ in range(MAX_PER_CLIENT_PER_MINUTE):
            response = await self.post()
            self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.calls), MAX_PER_CLIENT_PER_MINUTE)
        denied = await self.post()
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(denied.text, "Request rejected")
        self.assertEqual(len(self.calls), MAX_PER_CLIENT_PER_MINUTE)
        self.assertEqual(self.gateway._budget._active_total, 0)

        self.grants.append(synthetic_grant("b" * 48, "client-b", 300))
        self.assertEqual((await self.post("b" * 48)).status_code, 200)
        self.current_time[0] += 61
        self.assertEqual((await self.post()).status_code, 200)

    async def _run_silent_stream(self, trigger):
        class SilentStream(httpx.AsyncByteStream):
            def __init__(self):
                self.wait = asyncio.Event()
                self.closed = asyncio.Event()

            async def __aiter__(self):
                await self.wait.wait()
                yield b"unexpected"

            async def aclose(self):
                self.closed.set()

        stream = SilentStream()
        self.stream[0] = stream
        started = asyncio.Event()
        received = [False]
        events = []

        async def receive():
            if not received[0]:
                received[0] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Future()

        async def send(message):
            events.append(message["type"])
            if message["type"] == "http.response.start":
                started.set()

        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/mcp", "raw_path": b"/mcp", "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:18790"),
                        (b"authorization", b"Bearer " + b"a" * 48),
                        (b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 2000),
            "server": ("127.0.0.1", 18790),
            "scheme": "http", "root_path": "",
        }
        task = asyncio.create_task(self.gateway(scope, receive, send))
        try:
            await asyncio.wait_for(started.wait(), 1)
            await trigger()
            await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(stream.closed.is_set())
        self.assertIn("http.response.start", events)
        self.assertNotIn("http.response.body", events)
        self.assertEqual(self.gateway._budget._active_total, 0)

    async def test_active_stream_revocation_cancels_upstream(self):
        async def revoke():
            self.grants[:] = []
        with patch("remote_agent_gateway.REVOCATION_POLL_SECONDS", 0.01):
            await self._run_silent_stream(revoke)
        self.assertEqual((await self.post()).status_code, 401)

    async def test_active_stream_expiration_cancels_upstream(self):
        async def expire():
            self.current_time[0] = 301
        with patch("remote_agent_gateway.REVOCATION_POLL_SECONDS", 0.01):
            await self._run_silent_stream(expire)
        self.assertEqual((await self.post()).status_code, 401)

    async def test_stream_lifetime_limit_closes_idle_upstream(self):
        async def no_change():
            return None
        with patch("remote_agent_gateway.MAX_RESPONSE_SECONDS", 0.02):
            await self._run_silent_stream(no_change)

    async def test_incomplete_request_body_has_deadline(self):
        received = [False]
        events = []

        async def receive():
            if not received[0]:
                received[0] = True
                return {"type": "http.request", "body": b"partial",
                        "more_body": True}
            await asyncio.Future()

        async def send(message):
            events.append(message)

        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "path": "/mcp", "raw_path": b"/mcp", "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:18790"),
                        (b"authorization", b"Bearer " + b"a" * 48)],
            "client": ("127.0.0.1", 2000),
            "server": ("127.0.0.1", 18790),
            "scheme": "http", "root_path": "",
        }
        with patch("remote_agent_gateway.MAX_BODY_SECONDS", 0.02):
            await asyncio.wait_for(self.gateway(scope, receive, send), 1)
        self.assertEqual(events[0]["status"], 408)
        self.assertFalse(self.calls)
        self.assertEqual(self.gateway._budget._active_total, 0)


if __name__ == "__main__":
    unittest.main()
