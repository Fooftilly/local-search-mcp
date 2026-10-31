"""Privacy-constrained, bounded per-client gateway audit, without listeners.

These tests use synthetic tokens, an in-memory ASGI gateway and a mock
upstream. The audit is not a durable/compliance record or public endpoint.
"""
import time
import unittest

import httpx

from remote_agent_gateway import (
    GatewayAudit, GatewayAuditEvent, LoopbackMCPGateway,
    MAX_AUDIT_EVENTS, synthetic_grant,
)


class GatewayAuditCapacityTests(unittest.TestCase):
    def test_snapshot_is_bounded_and_returns_only_sanitized_fields(self):
        events = GatewayAudit()
        for n in range(MAX_AUDIT_EVENTS + 13):
            events.record("client-a", "POST", 200 if n % 2 else 503, True)
        snapshot = events.snapshot()
        self.assertEqual(len(snapshot), MAX_AUDIT_EVENTS)
        self.assertEqual(snapshot[0].status,
                         200 if 13 % 2 else 503)
        self.assertEqual(snapshot[-1].client_id, "client-a")
        self.assertEqual(
            set(GatewayAuditEvent.__dataclass_fields__),
            {"client_id", "method", "status", "completed"})
        with self.assertRaises(AttributeError):
            snapshot[0].status = 400


class GatewayAuditIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.grants = [
            synthetic_grant("a" * 48, "client-a", time.time() + 3600),
            synthetic_grant("b" * 48, "client-b", time.time() + 3600),
        ]
        self.guard = [True]
        self.upstream_calls = []
        def upstream_handler(request):
            self.upstream_calls.append(request)
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "result": {"tools": []}, "id": 1})
        self.upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream_handler),
            trust_env=False, timeout=5)
        self.gateway = LoopbackMCPGateway(
            upstream=self.upstream, grants=lambda: tuple(self.grants),
            guard_ready=lambda: self.guard[0])
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.gateway),
            base_url="http://127.0.0.1:18790", timeout=5)
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.upstream.aclose)

    async def post(self, token):
        return await self.client.post(
            "/mcp",
            headers={"authorization": "Bearer " + token},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                  "params": {"query": "PRIVATE_QUERY_NOT_FOR_AUDIT"}})

    async def test_authorized_client_identity_is_separate_and_private(self):
        self.assertEqual((await self.post("a" * 48)).status_code, 200)
        self.assertEqual((await self.post("b" * 48)).status_code, 200)
        self.assertEqual((await self.post("z" * 48)).status_code, 401)
        self.guard[0] = False
        self.assertEqual((await self.post("b" * 48)).status_code, 503)

        audit = self.gateway.audit_snapshot()
        self.assertEqual(audit, (
            GatewayAuditEvent("client-a", "POST", 200, True),
            GatewayAuditEvent("client-b", "POST", 200, True),
            GatewayAuditEvent("client-b", "POST", 503, True),
        ))
        joined = repr(audit)
        for secret in ("a" * 48, "b" * 48,
                       "z" * 48, "PRIVATE_QUERY_NOT_FOR_AUDIT",
                       "127.0.0.1:8790"):
            self.assertNotIn(secret, joined)
        self.assertEqual(len(self.upstream_calls), 2)

    async def test_quota_denial_is_attributed_without_forwarding(self):
        from remote_agent_gateway import MAX_PER_CLIENT_PER_MINUTE
        for _ in range(MAX_PER_CLIENT_PER_MINUTE):
            self.assertEqual((await self.post("a" * 48)).status_code, 200)
        before = len(self.upstream_calls)
        self.assertEqual((await self.post("a" * 48)).status_code, 429)
        self.assertEqual(len(self.upstream_calls), before)
        self.assertEqual(
            self.gateway.audit_snapshot()[-1],
            GatewayAuditEvent("client-a", "POST", 429, True))
        self.assertEqual((await self.post("b" * 48)).status_code, 200)
        self.assertEqual(
            self.gateway.audit_snapshot()[-1].client_id, "client-b")

    async def test_quota_reply_disconnect_after_headers_stays_in_audit(self):
        from remote_agent_gateway import MAX_PER_CLIENT_PER_MINUTE
        for _ in range(MAX_PER_CLIENT_PER_MINUTE):
            self.assertEqual((await self.post("a" * 48)).status_code, 200)
        count = len(self.upstream_calls)

        # ASGITransport buffers response bodies; simulate a real downstream
        # write failure after headers through the gateway's direct ASGI API.
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "path": "/mcp", "raw_path": b"/mcp", "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1:18790"),
                (b"authorization", b"Bearer " + b"a" * 48),
            ],
            "client": ("127.0.0.1", 4567),
            "server": ("127.0.0.1", 18790), "scheme": "http",
        }
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"{}",
                    "more_body": False}
        async def send(message):
            sent.append(message["type"])
            if message["type"] == "http.response.body":
                raise ConnectionResetError("synthetic client disconnect")

        with self.assertRaises(ConnectionResetError):
            await self.gateway(scope, receive, send)
        self.assertEqual(sent, ["http.response.start",
                                "http.response.body"])
        self.assertEqual(self.gateway.audit_snapshot()[-1],
                         GatewayAuditEvent("client-a", "POST", 429, False))
        self.assertEqual(len(self.upstream_calls), count)


if __name__ == "__main__":
    unittest.main()
