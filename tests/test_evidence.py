"""Opt-in citation evidence over already authorized, short-lived Markdown."""
from collections import OrderedDict
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
from fastmcp.exceptions import ToolError
import httpx

import bridge
import document_pages
import domain_policy


class EvidenceMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        setting = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(self.tmp.name) / "history.jsonl"),
        })
        setting.start()
        self.addCleanup(setting.stop)
        bridge.request_cache.reset_for_test()
        self.addCleanup(bridge.request_cache.reset_for_test)

    async def test_short_fetch_opt_in_evidence_preserves_default_and_no_extra_crawl(self):
        url = "https://example.com/articles?credential=DO_NOT_LOG"
        redirect = "https://example.com/published?token=PRIVATE_QUERY"
        content = "🧭 Šćepan said: the eligibility deadline is 31 October.\n"
        calls = []
        original = httpx.Client

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            self.assertEqual(request.url.path, "/crawl")
            self.assertEqual(json.loads(request.content)["urls"], [url])
            return httpx.Response(200, json={"results": [{
                "url": url, "redirected_url": redirect, "success": True,
                "markdown": content,
                "metadata": {"title": "Example", "canonical_url": redirect},
            }]})

        def client_factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)

        with (patch.object(document_pages, "_CACHE", OrderedDict()),
              patch.object(bridge.httpx, "Client", side_effect=client_factory),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext()),
              patch.object(bridge, "_check_public_target", return_value=None),
              patch.object(bridge, "CRAWLER_SECURITY_WARNING", None)):
            domain_policy.decide("example.com", "allow", scope="exact")
            async with Client(bridge.mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertIn("retain_for_evidence",
                              tools["web_fetch"].inputSchema["properties"])
                self.assertIn("include_evidence",
                              tools["web_extract_section"].inputSchema["properties"])
                ordinary = await client.call_tool("web_fetch", {"url": url})
                self.assertFalse(ordinary.is_error)
                self.assertTrue(ordinary.data["success"])
                self.assertNotIn("document_id", ordinary.data)
                self.assertNotIn("retained_for_evidence", ordinary.data)
                opted = await client.call_tool("web_fetch", {
                    "url": url, "retain_for_evidence": True})
                self.assertFalse(opted.is_error)
                self.assertTrue(opted.data["success"])
                self.assertFalse(opted.data["truncated"])
                self.assertEqual(opted.data["page_count"], 1)
                self.assertTrue(opted.data["retained_for_evidence"])
                handle = opted.data["document_id"]
                self.assertEqual(len(handle), 32)
                before = len(calls)
                short = await client.call_tool("web_extract_section", {
                    "document_id": handle, "start_offset": 0, "max_chars": 56,
                    "include_evidence": True})
                self.assertFalse(short.is_error)
                self.assertTrue(short.data["success"])
                self.assertEqual(short.data["text"], content)
                proof = short.data["evidence"]
                self.assertEqual(proof["quotation"], content)
                self.assertEqual(proof["quote_start_offset"], 0)
                self.assertEqual(proof["quote_end_offset"], len(content))
                self.assertFalse(proof["quote_truncated"])
                self.assertEqual(proof["page"], 1)
                self.assertEqual(proof["source_url"], "https://example.com/articles")
                self.assertEqual(proof["final_url"], "https://example.com/published")
                self.assertTrue(proof["query_parameters_removed"])
                self.assertFalse(proof["publisher_claims_verified"])
                self.assertEqual(proof["retrieval_timestamp"],
                                 "not_recorded_in_temporary_document")
                self.assertEqual(len(calls), before)
                same = await client.call_tool("web_extract_section", {
                    "document_id": handle, "start_offset": 0, "max_chars": 10})
                self.assertNotIn("evidence", same.data)
                domain_policy.decide("example.com", "deny", scope="exact")
                blocked = await client.call_tool("web_extract_section", {
                    "document_id": handle, "start_offset": 0,
                    "include_evidence": True})
                self.assertEqual(blocked.data["status"], "blocked")
                self.assertNotIn("evidence", blocked.data)
                self.assertNotIn("deadline", str(blocked.data))
                unknown = await client.call_tool("web_extract_section", {
                    "document_id": "Z" * 32, "start_offset": 0,
                    "include_evidence": True})
                self.assertEqual(unknown.data["status"], "expired_or_unknown")
                # Pydantic may coerce "yes" into bool at the MCP
                # boundary; the underlying tool itself must fail closed.
                with self.assertRaises(ValueError):
                    bridge.web_extract_section(handle, 0, include_evidence="yes")
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        self.assertNotIn(handle, audit)
        self.assertNotIn("DO_NOT_LOG", audit)
        self.assertNotIn("PRIVATE_QUERY", audit)
        self.assertNotIn("eligibility deadline", audit)

    async def test_long_quote_is_capped_with_exact_unicode_offsets(self):
        text = "🧭" + "ž" * 1500 + "some text"
        with patch.object(document_pages, "_CACHE", OrderedDict()):
            token, count = document_pages.save(
                "https://example.com/report", text, {}, allow_short=True)
            self.assertEqual(count, 1)
            domain_policy.decide("example.com", "allow", scope="exact")
            async with Client(bridge.mcp) as client:
                r = await client.call_tool("web_extract_section", {
                    "document_id": token, "start_offset": 1, "max_chars": 1450,
                    "include_evidence": True})
                self.assertTrue(r.data["success"])
                self.assertEqual(r.data["text"], "ž" * 1450)
                e = r.data["evidence"]
                self.assertEqual(e["quotation"], "ž" * 1200)
                self.assertEqual((e["quote_start_offset"], e["quote_end_offset"]),
                                 (1, 1201))
                self.assertTrue(e["quote_truncated"])
                self.assertEqual(e["offset_units"],
                                 "unicode_code_points_in_normalized_markdown")

    async def test_citation_preserves_explicit_ports_and_ignores_fragment_question_mark(self):
        source = "http://example.com:443/source#section?not-a-query"
        final = "https://example.com:80/target#part?also-not-a-query"
        with patch.object(document_pages, "_CACHE", OrderedDict()):
            domain_policy.decide("example.com", "allow", scope="exact")
            token, _ = document_pages.save(
                final, "Valid text", {}, source_url=source, allow_short=True)
            async with Client(bridge.mcp) as client:
                result = await client.call_tool("web_extract_section", {
                    "document_id": token, "start_offset": 0,
                    "include_evidence": True})
                self.assertTrue(result.data["success"])
                proof = result.data["evidence"]
                self.assertEqual(proof["source_url"], "http://example.com:443/source")
                self.assertEqual(proof["final_url"], "https://example.com:80/target")
                self.assertFalse(proof["query_parameters_removed"])
        src, stripped = bridge._citation_url(
            "https://example.com:80/article?key=PRIVATE#fragment?part")
        self.assertEqual(src, "https://example.com:80/article")
        self.assertTrue(stripped)
        self.assertNotIn("PRIVATE", src)
        for malformed in ("https://user:pass@example.com/item", "http://example.com:bad/item",
                          "ftp://example.com/item"):
            self.assertEqual(bridge._citation_url(malformed), ("", False))

    def test_short_retention_is_explicit_ttl_bounded_and_rejects_non_text(self):
        with patch.object(document_pages, "_CACHE", OrderedDict()):
            with self.assertRaises(ValueError):
                document_pages.save("https://example.com/a", "short", {})
            for bad in (1, "yes", None):
                with self.subTest(value=bad), self.assertRaises(ValueError):
                    document_pages.save("https://example.com/a", "short", {},
                                        allow_short=bad)
            with self.assertRaises(ValueError):
                document_pages.save("https://example.com/a", "", {},
                                    allow_short=True)
            with patch.object(document_pages.time, "monotonic", return_value=100):
                handle, count = document_pages.save(
                    "https://example.com/a", "short", {}, allow_short=True)
                self.assertEqual(count, 1)
                self.assertEqual(document_pages.section_with_security_context(
                    handle, 0, 5)[0]["text"], "short")
            with patch.object(document_pages.time, "monotonic",
                              return_value=100 + document_pages.TTL_SECONDS):
                result = document_pages.section_with_security_context(handle, 0, 5)[0]
                self.assertEqual(result["status"], "expired_or_unknown")


if __name__ == "__main__":
    unittest.main()
