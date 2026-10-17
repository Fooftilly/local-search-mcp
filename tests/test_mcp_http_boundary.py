"""Exercise the real Streamable HTTP Host/Origin guard without binding sockets."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx

import bridge


class MCPHTTPBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_and_origin_validation_on_real_fastmcp_asgi_app(self):
        """Foreign authorities must be rejected before JSON-RPC dispatch."""
        # Even when a deployment env variable attempts to disable the guard,
        # explicit production-style settings must continue rejecting outsiders.
        with patch.dict(os.environ, {"FASTMCP_HTTP_HOST_ORIGIN_PROTECTION": "false"}):
            app = bridge.mcp.http_app(
                path="/mcp",
                stateless_http=True,
                host_origin_protection=True,
                allowed_hosts=bridge.MCP_ALLOWED_HOSTS,
                allowed_origins=bridge.MCP_ALLOWED_ORIGINS,
            )
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        common = {"Accept": "application/json, text/event-stream"}
        # ASGITransport doesn't start the app lifespan automatically.
        # The session manager must run or successful MCP requests cannot work.
        async with app.lifespan(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8790",
                timeout=5,
            ) as client:
                foreign_host = await client.post(
                    "/mcp", json=payload,
                    headers={**common, "Host": "attacker.example"},
                )
                foreign_origin = await client.post(
                    "/mcp", json=payload,
                    headers={**common, "Origin": "http://attacker.example"},
                )
                null_origin = await client.post(
                    "/mcp", json=payload,
                    headers={**common, "Origin": "null"},
                )
                # Positive path: the local tunnel uses a loopback Host with no
                # browser Origin and must still be able to list both tools.
                local = await client.post(
                    "/mcp", json=payload,
                    headers={**common, "Host": "127.0.0.1:8790"},
                )
        self.assertEqual(local.status_code, 200)
        self.assertIn("web_fetch", local.text)
        self.assertIn("web_search", local.text)
        self.assertEqual(foreign_host.status_code, 421)
        self.assertEqual(foreign_origin.status_code, 403)
        self.assertEqual(null_origin.status_code, 403)
        self.assertNotIn("web_fetch", foreign_host.text)
        self.assertNotIn("web_fetch", foreign_origin.text)

    async def test_server_startup_forces_guard_even_if_env_disables_it(self):
        """A deployment variable must not silently disable this local guard."""
        with patch.dict(os.environ, {"FASTMCP_HTTP_HOST_ORIGIN_PROTECTION": "false"}):
            with patch.object(bridge.mcp, "run") as run:
                bridge.serve_mcp()
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["transport"], "http")
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 8790)
        self.assertTrue(kwargs["host_origin_protection"])
        self.assertEqual(kwargs["allowed_hosts"], ["127.0.0.1:8790", "localhost:8790"])
        self.assertEqual(kwargs["allowed_origins"], ["http://127.0.0.1:8790", "http://localhost:8790"])


if __name__ == "__main__":
    unittest.main()
