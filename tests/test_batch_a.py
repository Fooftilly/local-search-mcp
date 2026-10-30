"""Batch A bounded agent-research tools: real MCP contract and security tests."""
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
import httpx

import bridge
import document_pages
import domain_policy


class BatchAContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(self.tmp.name) / "history.jsonl"),
        })
        env.start()
        self.addCleanup(env.stop)
        bridge.request_cache.reset_for_test()
        self.addCleanup(bridge.request_cache.reset_for_test)

    async def test_section_exact_unicode_span_crosses_page_and_never_fetches(self):
        text = "🧭" + "a" * (document_pages.PAGE_CHARS - 2) + "grant deadline" + "Z" * 4000
        with (patch.object(document_pages, "_CACHE", OrderedDict()),
              patch.object(bridge.httpx, "Client",
                           side_effect=AssertionError("Section must never contact an upstream"))):
            domain_policy.decide("example.com", "allow", scope="exact")
            handle, count = document_pages.save(
                "https://example.com/public", text, {}, first_page_chars=4096)
            self.assertEqual(count, 2)
            start = text.index("grant") - 10
            async with Client(bridge.mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertIn("web_extract_section", tools)
                self.assertIn("start_offset", tools["web_extract_section"].inputSchema["properties"])
                response = await client.call_tool("web_extract_section", {
                    "document_id": handle, "start_offset": start, "max_chars": 70})
                self.assertFalse(response.is_error)
                item = response.data
                self.assertTrue(item["success"])
                self.assertEqual(item["text"], text[start:start + 70])
                self.assertEqual(item["start_offset"], start)
                self.assertEqual(item["end_offset"], start + 70)
                self.assertEqual(item["next_offset"], start + 70)
                self.assertEqual(item["page"], 2)
                tail = await client.call_tool("web_extract_section", {
                    "document_id": handle,
                    "start_offset": len(text) - 6, "max_chars": 100})
                self.assertEqual(tail.data["text"], "Z" * 6)
                self.assertIsNone(tail.data["next_offset"])
                for invalid in (-1, True, "1", document_pages.MAX_DOCUMENT_CHARS + 1):
                    bad = await client.call_tool("web_extract_section", {
                        "document_id": handle, "start_offset": invalid})
                    self.assertTrue(bad.is_error)
                oversized = await client.call_tool("web_extract_section", {
                    "document_id": handle, "start_offset": 0, "max_chars": 8001})
                self.assertTrue(oversized.is_error)
                domain_policy.decide("example.com", "deny", scope="exact")
                refused = await client.call_tool("web_extract_section", {
                    "document_id": handle, "start_offset": start, "max_chars": 70})
                self.assertEqual(refused.data["status"], "blocked")
                self.assertNotIn("grant deadline", str(refused.data))
                unknown = await client.call_tool("web_extract_section", {
                    "document_id": "A" * 32, "start_offset": 0})
                self.assertEqual(unknown.data["status"], "expired_or_unknown")
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        self.assertNotIn(handle, audit)
        self.assertNotIn("grant deadline", audit)
        self.assertIn('"action":"section"', audit)

    async def test_compact_capability_filters_preserve_default_and_never_wake(self):
        config = {"engines": [
            {"name": "yahoo", "enabled": True, "categories": ["general", "web"]},
            {"name": "pinterest", "enabled": True, "categories": ["images"]},
            {"name": "bing images", "enabled": True, "categories": ["images", "web"]},
            {"name": "google images", "enabled": False, "categories": ["images"]},
        ], "secret_key": "NEVER_REVEAL_THIS"}
        seen = []
        original = httpx.Client

        def handler(request):
            seen.append(request.url.path)
            self.assertEqual(request.url.path, "/config")
            return httpx.Response(200, json=config)

        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)

        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=AssertionError("No Docker wake"))):
            async with Client(bridge.mcp) as client:
                default = await client.call_tool("web_search_capabilities", {})
                self.assertFalse(default.is_error)
                self.assertEqual(default.data["enabled_engine_count"], 3)
                self.assertNotIn("truncated", default.data)
                self.assertNotIn("matching_engine_count", default.data)
                filtered = await client.call_tool("web_search_capabilities", {
                    "category": "images", "max_engines": 1})
                self.assertEqual(filtered.data["matching_engine_count"], 2)
                self.assertEqual(filtered.data["returned_engine_count"], 1)
                self.assertTrue(filtered.data["truncated"])
                self.assertEqual(filtered.data["enabled_engines"][0]["name"], "bing images")
                self.assertEqual(filtered.data["enabled_engine_count"], 3)
                summary = await client.call_tool("web_search_capabilities", {
                    "engine": "yahoo", "summary_only": True})
                self.assertEqual(summary.data["enabled_engines"], [])
                self.assertEqual(summary.data["matching_engine_count"], 1)
                self.assertEqual(summary.data["categories"], ["general", "web"])
                self.assertTrue(summary.data["details_omitted"])
                absent = await client.call_tool("web_search_capabilities", {
                    "engine": "google images"})
                self.assertEqual(absent.data["status"], "not_enabled")
                for bad in ({"max_engines": -1}, {"summary_only": 1},
                            {"category": "images", "engine": "yahoo"},
                            {"engine": "a@internal.invalid"}):
                    invalid = await client.call_tool("web_search_capabilities", bad)
                    self.assertTrue(invalid.is_error)
        self.assertEqual(seen, ["/config"] * 4)
        self.assertNotIn("NEVER_REVEAL_THIS", str(default.data))
        self.assertNotIn("NEVER_REVEAL_THIS", str(filtered.data))

    async def test_batch_search_partial_failure_is_ordered_and_audited_per_query(self):
        original = httpx.Client
        seen = []

        def handler(request):
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "yahoo", "enabled": True, "categories": ["general"]}]})
            self.assertEqual(request.url.path, "/search")
            query = request.url.params.get("q")
            seen.append(query)
            if query == "offline":
                return httpx.Response(503)
            return httpx.Response(200, json={"results": [{
                "title": query, "url": "https://example.com/" + query,
                "content": "Public description", "engine": "yahoo",
                "engines": ["yahoo"],
            }]})

        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)

        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext())):
            async with Client(bridge.mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertIn("web_search_batch", tools)
                self.assertIn("queries", tools["web_search_batch"].inputSchema["properties"])
                result = await client.call_tool("web_search_batch", {
                    "queries": ["alpha", "offline", "beta"],
                    "engines": "yahoo", "max_results": 3, "fresh": True})
                self.assertFalse(result.is_error)
                data = result.data
                self.assertFalse(data["success"])
                self.assertEqual(data["query_count"], 3)
                self.assertEqual([x["query"] for x in data["results"]],
                                 ["alpha", "offline", "beta"])
                self.assertEqual([x["success"] for x in data["results"]],
                                 [True, False, True])
                self.assertEqual(data["results"][0]["results"][0]["title"], "alpha")
                self.assertEqual(data["results"][1]["status"], "upstream_unavailable")
                self.assertEqual(sorted(set(seen)), ["alpha", "beta", "offline"])
                before = len(seen)
                for invalid in (
                    {"queries": ["alpha", "ALPHA"], "engines": "yahoo"},
                    {"queries": ["valid", "!google unsafe"], "engines": "yahoo"},
                    {"queries": ["a", "b", "c", "d", "e"]},
                    {"queries": ["a"], "engines": "yahoo", "categories": "images"},
                    {"queries": ["a"], "max_results": 9},
                ):
                    error = await client.call_tool("web_search_batch", invalid)
                    self.assertTrue(error.is_error)
                self.assertEqual(len(seen), before)
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        self.assertIn('"query":"alpha"', audit)
        self.assertIn('"query":"beta"', audit)
        self.assertIn('"query":"offline"', audit)
        self.assertNotIn('"query":"[', audit)


if __name__ == "__main__":
    unittest.main()
