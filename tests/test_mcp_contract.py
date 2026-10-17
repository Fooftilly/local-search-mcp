"""Exercise the real FastMCP tool protocol without external services or credentials.

In-process FastMCP transport verifies tool registration, structured return
values, domain-approval boundaries, and mocked SearXNG/Crawl4AI HTTP calls.
It does NOT exercise a real Chromium browser or certify network isolation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
import httpx

import bridge
import domain_policy


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bridge.request_cache.reset_for_test()
        self.addCleanup(bridge.request_cache.reset_for_test)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(self.tmp.name) / "history.jsonl"),
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)

    async def test_tools_exposed_and_unknown_domain_is_pending(self):
        """An actual MCP call must return an actionable, non-granting response."""
        async with Client(bridge.mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            self.assertIn("web_search", names)
            self.assertIn("web_fetch", names)
            with patch.object(bridge.httpx, "Client") as backend:
                result = await client.call_tool(
                    "web_fetch", {"url": "https://docs.python.org/safe?secret=hidden"}
                )
                backend.assert_not_called()
        self.assertFalse(result.is_error)
        self.assertEqual(result.data["status"], "approval_required")
        self.assertIn("approval_dashboard", result.data)
        saved = {entry["domain"]: entry for entry in domain_policy.entries()}
        self.assertIn("docs.python.org", saved)
        self.assertEqual(saved["docs.python.org"]["status"], "pending")
        self.assertEqual(saved["docs.python.org"]["request_count"], 1)
        self.assertEqual(saved["docs.python.org"]["last_requested_url"], "https://docs.python.org/safe")
        self.assertNotIn("hidden", str(saved))

    async def test_optional_search_filters_are_exposed_over_real_mcp(self):
        seen = []
        def handler(request: httpx.Request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [
                {"title": "One", "url": "https://example.org", "content": "ok", "engine": "qwant"}
            ]})
        original = httpx.Client
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=factory):
            async with Client(bridge.mcp) as client:
                response = await client.call_tool("web_search", {
                    "query": "local query", "max_results": 2, "language": "en",
                    "categories": "news", "engines": "qwant", "time_range": "day", "page": 2,
                })
        self.assertFalse(response.is_error)
        self.assertEqual(response.data[0]["engine"], "qwant")
        self.assertEqual(seen, [{
            "q": "local query", "format": "json", "language": "en",
            "categories": "news", "time_range": "day", "engines": "qwant", "pageno": "2",
        }])

    async def test_long_document_continuation_over_actual_mcp_dispatch(self):
        from collections import OrderedDict
        content = "A" * 24000 + "B" * 17000
        calls = []
        def handler(request: httpx.Request):
            calls.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/long", "success": True,
                "markdown": {"raw_markdown": content}
            }]})
        original = httpx.Client
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        global_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                          ("93.184.215.14", 443))]
        with patch.object(bridge, "CRAWLER_SECURITY_WARNING", None), \
             patch.object(bridge.httpx, "Client", side_effect=factory), \
             patch.object(domain_policy.socket, "getaddrinfo", return_value=global_answer), \
             patch.object(bridge.document_pages, "_CACHE", OrderedDict()):
            async with Client(bridge.mcp) as client:
                tools = {tool.name for tool in await client.list_tools()}
                self.assertIn("web_fetch_page", tools)
                first = await client.call_tool("web_fetch", {"url": "https://example.com/long"})
                self.assertFalse(first.is_error)
                second = await client.call_tool("web_fetch_page", {
                    "document_id": first.data["document_id"], "page": 2
                })
                self.assertFalse(second.is_error)
                self.assertEqual(first.data["markdown"] + second.data["markdown"], content)
                self.assertIsNone(second.data["next_page"])
                missing = await client.call_tool("web_fetch_page", {
                    "document_id": "A" * 32, "page": 2
                })
                self.assertEqual(missing.data["status"], "expired_or_unknown")
            # Handles are never written into local audit history.
            audit = (Path(self.tmp.name) / "history.jsonl").read_text()
            self.assertNotIn(first.data["document_id"], audit)
            self.assertNotIn(first.data["markdown"], audit)
            self.assertNotIn(content[24000:], audit)
        self.assertEqual(calls, ["/health", "/crawl"])

    async def test_fresh_search_parameter_is_backward_compatible_in_real_mcp(self):
        seen = []
        original = httpx.Client
        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [
                {"url": "https://example.com", "title": "ok", "content": "ok", "engine": "test"}
            ]})
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=factory):
            async with Client(bridge.mcp) as client:
                first = await client.call_tool("web_search", {"query": "mcp cache test"})
                self.assertEqual(len(seen), 1)
                second = await client.call_tool("web_search", {"query": "mcp cache test"})
                self.assertEqual(len(seen), 1)
                fresh = await client.call_tool("web_search", {
                    "query": "mcp cache test", "fresh": True
                })
                self.assertEqual(len(seen), 2)
        self.assertFalse(first.is_error)
        self.assertFalse(second.is_error)
        self.assertFalse(fresh.is_error)
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.data, fresh.data)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all("fresh" not in params for params in seen))

    async def test_invalid_public_targets_are_blocked_without_backend(self):
        """MCP cannot fetch local, special-use, credentialed or nonstandard URLs."""
        invalid = (
            "http://127.0.0.1/",
            "http://localhost/",
            "file:///etc/passwd",
            "http://example.com:8080/",
            "https://co.uk/",
            "https://username:password@example.com/",
        )
        async with Client(bridge.mcp) as client:
            for target in invalid:
                with self.subTest(url=target):
                    with patch.object(bridge.httpx, "Client") as backend:
                        result = await client.call_tool("web_fetch", {"url": target})
                        backend.assert_not_called()
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.data["status"], "blocked")

    async def test_approved_search_and_fetch_over_real_mcp_dispatch(self):
        """Both real MCP tools must work against simulated local upstream APIs."""
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/search":
                self.assertEqual(request.url.params["q"], "secure search")
                return httpx.Response(200, json={
                    "results": [{"title": "Result", "url": "https://example.com",
                                 "content": "summary", "engine": "mock"}]
                })
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            if request.url.path == "/crawl":
                self.assertEqual(request.method, "POST")
                self.assertEqual(json.loads(request.content)["urls"], ["https://example.com/page"])
                return httpx.Response(200, json={
                    "results": [{"url": "https://example.com/page",
                                 "success": True, "markdown": "Page content"}]
                })
            raise AssertionError(f"Unexpected upstream API path: {request.url.path}")

        original = httpx.Client

        def client_factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)

        global_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                          ("93.184.215.14", 443))]
        with patch.object(bridge, "CRAWLER_SECURITY_WARNING", None), \
             patch.object(bridge.httpx, "Client", side_effect=client_factory), \
             patch.object(domain_policy.socket, "getaddrinfo", return_value=global_answer):
            async with Client(bridge.mcp) as client:
                search = await client.call_tool(
                    "web_search", {"query": "secure search", "max_results": 2}
                )
                fetch = await client.call_tool(
                    "web_fetch", {"url": "https://example.com/page"}
                )
        self.assertFalse(search.is_error)
        self.assertEqual(search.data[0]["title"], "Result")
        self.assertFalse(fetch.is_error)
        self.assertTrue(fetch.data["success"])
        self.assertEqual(fetch.data["markdown"], "Page content")
        self.assertEqual(requests, [
            ("GET", "/search"), ("GET", "/health"), ("POST", "/crawl")
        ])


if __name__ == "__main__":
    unittest.main()
