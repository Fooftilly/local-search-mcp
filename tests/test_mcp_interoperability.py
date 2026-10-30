"""Baseline FastMCP interoperability contract: tests-only, no real network.

These in-process checks do not claim that ChatGPT, Cursor, Hermes, or a
deployed tunnel have passed live cross-client acceptance.
"""
from contextlib import nullcontext
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
import httpx

import bridge


class MCPInteroperabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(
                Path(self.temp.name) / "history.jsonl"),
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        settings.start()
        self.addCleanup(settings.stop)
        bridge.request_cache.reset_for_test()
        self.addCleanup(bridge.request_cache.reset_for_test)

    async def test_discovery_preserves_existing_tool_names_and_input_contract(self):
        async with Client(bridge.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
        expected = {
            "web_search", "web_search_capabilities", "web_fetch",
            "web_fetch_batch", "web_fetch_page", "web_table_page",
            "web_find", "web_metrics",
        }
        # Additional opt-in tools in later PRs must not break this baseline.
        self.assertTrue(expected.issubset(tools), expected - set(tools))
        properties = {
            "web_search": {"query", "max_results", "include_search_status",
                           "include_image_metadata", "allowed_result_domains"},
            "web_search_capabilities": set(),
            "web_fetch": {"url", "mode", "fresh", "include_links",
                          "include_tables", "compact_chars", "include_retrieval_info"},
            "web_fetch_batch": {"urls", "mode", "fresh", "compact_chars"},
            "web_fetch_page": {"document_id", "page"},
            "web_table_page": {"table_id", "table_index", "row_offset",
                               "column_offset", "max_rows", "max_columns"},
            "web_find": {"document_id", "query", "max_matches", "start_offset"},
            "web_metrics": {"hours"},
        }
        for name in sorted(expected):
            with self.subTest(name=name):
                tool = tools[name]
                self.assertIsInstance(tool.inputSchema, dict)
                self.assertEqual(tool.inputSchema["type"], "object")
                self.assertTrue(properties[name].issubset(
                    tool.inputSchema.get("properties", {})))
                self.assertTrue(bool(tool.description))
                self.assertTrue(tool.annotations.readOnlyHint)
        self.assertEqual(tools["web_fetch_batch"].inputSchema["properties"][
            "urls"]["type"], "array")
        self.assertFalse(tools["web_search"].inputSchema["properties"][
            "include_image_metadata"]["default"])
        self.assertFalse(tools["web_fetch"].inputSchema["properties"][
            "include_links"]["default"])

    async def test_capability_result_is_structured_and_never_wakes_docker(self):
        original_client = httpx.Client
        requests = []
        def handler(request):
            requests.append(request.url.path)
            self.assertEqual(request.url.path, "/config")
            return httpx.Response(200, json={
                "engines": [
                    {"name": "yahoo", "enabled": True,
                     "categories": ["general"]},
                    {"name": "google images", "enabled": False,
                     "categories": ["images"]},
                ],
                "secret_key": "NEVER_LEAK_PRIVATE_CONFIG",
            })
        def client_factory(**kwargs):
            return original_client(
                transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=client_factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=AssertionError("Inventory cannot wake Docker"))):
            async with Client(bridge.mcp) as client:
                response = await client.call_tool("web_search_capabilities", {})
        self.assertFalse(response.is_error)
        self.assertIsInstance(response.data, dict)
        self.assertEqual(response.data["status"], "available")
        self.assertEqual(response.data["enabled_engine_count"], 1)
        self.assertEqual(response.data["enabled_engines"][0]["name"], "yahoo")
        self.assertNotIn("google images", str(response.data))
        self.assertNotIn("NEVER_LEAK_PRIVATE_CONFIG", str(response.data))
        self.assertEqual(requests, ["/config"])

    async def test_legacy_search_output_and_validation_errors_stay_distinct(self):
        original_client = httpx.Client
        seen = []
        def handler(request):
            seen.append(str(request.url))
            self.assertEqual(request.url.path, "/search")
            return httpx.Response(200, json={"results": [{
                "title": "Public source", "url": "https://example.com/article",
                "content": "Published page", "engine": "yahoo",
            }]})
        def client_factory(**kwargs):
            return original_client(
                transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=client_factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext())):
            async with Client(bridge.mcp) as client:
                normal = await client.call_tool("web_search", {
                    "query": "public technical documentation", "max_results": 2})
                self.assertFalse(normal.is_error)
                self.assertIsInstance(normal.data, list)
                self.assertEqual(len(normal.data), 1)
                self.assertEqual(set(normal.data[0]),
                                 {"title", "url", "snippet", "engine"})
                before = len(seen)
                # An invalid selection must never send an upstream search.
                with self.assertRaisesRegex(Exception, "Choose categories or engines"):
                    await client.call_tool("web_search", {
                        "query": "public technical documentation",
                        "categories": "news", "engines": "yahoo"})
                self.assertEqual(len(seen), before)

    async def test_rejected_private_fetch_never_reaches_backend(self):
        with patch.object(bridge.httpx, "Client",
                          side_effect=AssertionError("No network on blocked input")):
            async with Client(bridge.mcp) as client:
                response = await client.call_tool(
                    "web_fetch", {"url": "http://127.0.0.1/private"})
        self.assertFalse(response.is_error)
        self.assertFalse(response.data["success"])
        self.assertIn(response.data["status"], ("blocked", "denied"))
        self.assertNotIn("document_id", response.data)


if __name__ == "__main__":
    unittest.main()
