"""Offline private-origin routing and authenticated-gateway regression tests.

No Cloudflare API, live listener, Docker, sudo or external network access.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from remote_agent_cloudflare_origin import (
    CLIENT_ID, CloudflarePrivateOrigin, ORIGIN_HOST, ORIGIN_PORT,
    SCHEMA, StartupRefused, build_config, check_portal_grant,
    read_private_hostname, validate_hostname,
)
from remote_agent_gateway import LoopbackMCPGateway, synthetic_grant
from remote_agent_prerequisites import SCHEMA as GRANTS_SCHEMA


class PrivateOriginConfigTests(unittest.TestCase):
    def test_hostname_validation_exact_dns_not_url_or_loopback(self):
        self.assertEqual(validate_hostname("MCP.Example.Internal"),
                         "mcp.example.internal")
        for host in ("", "localhost", "127.0.0.1", "mcp.local",
                     "mcp.example", "*.example.internal", "mcp.example.internal:443",
                     "https://mcp.example.internal", "mcp..internal",
                     "mcp.internal/other", "mcp.internal.", " a.internal",
                     "bad_label.internal", "xn--.internal"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_hostname(host)

    def test_private_config_requires_owned_exact_schema_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "origin.json"
            path.write_text(json.dumps({
                "schema": SCHEMA, "hostname": "mcp.example.internal"}))
            path.chmod(0o600)
            self.assertEqual(read_private_hostname(path), "mcp.example.internal")
            path.chmod(0o644)
            with self.assertRaises(StartupRefused):
                read_private_hostname(path)
            path.chmod(0o600)
            alias = root / "symlink.json"
            alias.symlink_to(path)
            with self.assertRaises(StartupRefused):
                read_private_hostname(alias)
            path.write_text(json.dumps({
                "schema": SCHEMA, "hostname": "mcp.example.internal",
                "Authorization": "never-accepted"}))
            with self.assertRaises(StartupRefused):
                read_private_hostname(path)

    def test_requires_dedicated_active_grant(self):
        grant = synthetic_grant("z" * 48, CLIENT_ID, time.time() + 300)
        with patch("remote_agent_cloudflare_origin.load_client_grants",
                   return_value=(grant,)):
            check_portal_grant()
        with patch("remote_agent_cloudflare_origin.load_client_grants",
                   return_value=(synthetic_grant("z" * 48, "another-client",
                                              time.time() + 300),)):
            with self.assertRaises(StartupRefused):
                check_portal_grant()
        with patch("remote_agent_cloudflare_origin.load_client_grants",
                   return_value=(synthetic_grant("z" * 48, CLIENT_ID,
                                              time.time() - 1),)):
            with self.assertRaises(StartupRefused):
                check_portal_grant()

    def test_local_only_service_config(self):
        cfg = build_config(object())
        self.assertEqual((cfg.host, cfg.port), (ORIGIN_HOST, ORIGIN_PORT))
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertFalse(cfg.proxy_headers)
        self.assertFalse(cfg.access_log)
        self.assertEqual(cfg.workers, 1)
        self.assertEqual(cfg.ws, "none")


class PrivateOriginHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = "T" * 48
        self.hostname = "mcp.example.internal"
        self.gateway_requests = []
        self.bridge_requests = []
        self.guard_calls = 0

        def bridge_handler(request):
            self.bridge_requests.append(request)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                "result": {"tools": [{"name": "web_search"}]}})

        self.bridge_client = httpx.AsyncClient(
            transport=httpx.MockTransport(bridge_handler), trust_env=False)
        self.gateway = LoopbackMCPGateway(
            upstream=self.bridge_client,
            grants=lambda: (synthetic_grant(self.token, CLIENT_ID,
                                            time.time() + 3600),),
            guard_ready=self._guard,
        )

        async def gateway_app(scope, receive, send):
            self.gateway_requests.append(scope)
            await self.gateway(scope, receive, send)

        self.gateway_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app, client=("127.0.0.1", 12345)),
            trust_env=False)
        self.adapter = CloudflarePrivateOrigin(self.hostname, self.gateway_client)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.adapter, client=("127.0.0.1", 12346)),
            base_url="http://" + self.hostname, trust_env=False)

    def _guard(self):
        self.guard_calls += 1
        return True

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.gateway_client.aclose()
        await self.bridge_client.aclose()

    async def send(self, *, host=None, token=None, path="/mcp", method="POST",
                   headers=None, data=None):
        all_headers = {"host": self.hostname if host is None else host}
        if token is not None:
            all_headers["authorization"] = "Bearer " + token
        if headers:
            all_headers.update(headers)
        return await self.client.request(
            method, path, headers=all_headers,
            content=(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
                     if data is None and method == "POST" else data))

    async def test_authorized_request_reaches_only_fixed_gateway_and_bridge(self):
        response = await self.send(token=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.gateway_requests), 1)
        self.assertEqual(len(self.bridge_requests), 1)
        self.assertGreater(self.guard_calls, 0)
        request_scope = self.gateway_requests[0]
        self.assertEqual(request_scope["path"], "/mcp")
        self.assertEqual([v for k, v in request_scope["headers"]
                          if k == b"host"], [b"127.0.0.1:18790"])
        self.assertEqual(self.bridge_requests[0].url.host, "127.0.0.1")
        self.assertEqual(self.bridge_requests[0].url.port, 8790)
        self.assertNotIn("authorization", self.bridge_requests[0].headers)

    async def test_unauthenticated_or_wrong_grant_never_reaches_bridge(self):
        for tok in (None, "X" * 48, "short"):
            with self.subTest(token=tok):
                response = await self.send(token=tok)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.bridge_requests, [])
        self.assertEqual(self.guard_calls, 0)

    async def test_bad_host_origin_private_path_and_forward_headers_are_rejected(self):
        cases = [
            dict(host="other.internal", token=self.token),
            dict(host="mcp.example.internal:80", token=self.token),
            dict(token=self.token, headers={"origin": "https://evil.example"}),
            dict(token=self.token, headers={"x-forwarded-host": self.hostname}),
            dict(token=self.token, headers={"cookie": "SESSION=x"}),
            dict(token=self.token, headers={"mcp-session-id": "session"}),
            dict(token=self.token, headers={"cf-access-client-secret": "faked"}),
            dict(token=self.token, path="/mcp?x=1"),
            dict(token=self.token, path="/mcp/"),
            dict(token=self.token, path="/health"),
        ]
        for case in cases:
            with self.subTest(case=list(case)):
                response = await self.send(**case)
                self.assertIn(response.status_code, (403, 404))
        self.assertEqual(self.gateway_requests, [])
        self.assertEqual(self.bridge_requests, [])

    async def test_adapter_cannot_proxy_lan_peer(self):
        denied = CloudflarePrivateOrigin(self.hostname, self.gateway_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=denied, client=("192.168.1.10", 3333)),
            base_url="http://" + self.hostname, trust_env=False) as remote:
            response = await remote.post(
                "/mcp", headers={"authorization": "Bearer " + self.token},
                content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.gateway_requests, [])

    async def test_gateway_guard_lost_blocks_authorized_request(self):
        self.gateway._guard_ready = lambda: False
        response = await self.send(token=self.token)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.bridge_requests, [])


if __name__ == "__main__":
    unittest.main()
