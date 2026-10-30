"""Batch B link continuation: read-only MCP behavior and bounded local storage."""
from collections import OrderedDict
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
from fastmcp.exceptions import ToolError
import httpx

import bridge
import document_pages
import domain_policy
import structured_extraction


class LinkContinuationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(self.tmp.name) / "history.jsonl"),
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)
        bridge.request_cache.reset_for_test()
        self.addCleanup(bridge.request_cache.reset_for_test)

    async def test_real_mcp_paginates_unselected_links_without_recrawling(self):
        original = httpx.Client
        visited = []
        page = {
            "url": "https://example.com/articles/index", "success": True,
            "markdown": {"raw_markdown": "Public document index"},
            "links": {
                "internal": [
                    {"href": f"/menu/entry-{i}", "text": "Menu"} for i in range(28)
                ] + [
                    {"href": f"/articles/report-{i}",
                     "text": f"Public research report {i}"} for i in range(15)
                ] + [
                    {"href": "javascript:alert(1)", "text": "Never retain"},
                    {"href": "/articles/report-0#duplicate", "text": "Longer report label"},
                ],
                "external": [
                    {"href": f"https://outside.example/reports/{i}",
                     "text": f"External research report {i}"} for i in range(8)
                ],
            },
        }
        def handler(request):
            visited.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            self.assertEqual(request.url.path, "/crawl")
            return httpx.Response(200, json={"results": [page]})

        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)

        public_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                          ("93.184.215.14", 443))]
        with (patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(domain_policy.socket, "getaddrinfo", return_value=public_answer),
              patch.object(document_pages, "_CACHE", OrderedDict())):
            async with Client(bridge.mcp) as client:
                tools = {t.name: t for t in await client.list_tools()}
                self.assertIn("web_links_page", tools)
                self.assertIn("link_id", tools["web_links_page"].inputSchema["properties"])
                response = await client.call_tool("web_fetch", {
                    "url": "https://example.com/articles/index",
                    "include_links": True,
                })
                self.assertFalse(response.is_error)
                first = response.data
                self.assertEqual(len(first["links"]), 24)
                self.assertTrue(first["structured_info"]["links"]["truncated"])
                self.assertIn("link_continuation", first)
                handle = first["link_continuation"]["link_id"]
                self.assertGreater(first["link_continuation"]["retained_links"], 0)
                initial_urls = [item["url"] for item in first["links"]]
                later_urls = []
                offset = 0
                while True:
                    continued = await client.call_tool("web_links_page", {
                        "link_id": handle, "offset": offset, "max_links": 7})
                    self.assertFalse(continued.is_error)
                    data = continued.data
                    self.assertTrue(data["success"])
                    self.assertLessEqual(len(data["links"]), 7)
                    later_urls.extend(item["url"] for item in data["links"])
                    self.assertEqual(data["source_completeness"], "unknown")
                    if not data["has_more"]:
                        self.assertIsNone(data["next_offset"])
                        break
                    self.assertGreater(data["next_offset"], offset)
                    offset = data["next_offset"]
                self.assertEqual(len(initial_urls + later_urls),
                                 len(set(initial_urls + later_urls)))
                self.assertTrue(all("javascript:" not in url
                                    for url in initial_urls + later_urls))
                self.assertEqual(len(initial_urls + later_urls), 51)
                self.assertEqual(visited, ["/health", "/crawl"])
                with self.assertRaises(ToolError):
                    await client.call_tool("web_links_page", {
                        "link_id": handle, "max_links": 11})
                unknown = await client.call_tool("web_links_page", {
                    "link_id": "A" * 32})
                self.assertEqual(unknown.data["status"], "expired_or_unknown")
                domain_policy.decide("example.com", "deny", scope="exact")
                denied = await client.call_tool("web_links_page", {
                    "link_id": handle})
                self.assertEqual(denied.data["status"], "blocked")
                self.assertNotIn("report-1", str(denied.data))
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        self.assertNotIn(handle, audit)
        self.assertNotIn("Public research report", audit)
        self.assertIn('"action":"links_page"', audit)

    async def test_batch_rejects_evicted_link_handle_without_returning_page_text(self):
        from batch_fetch import _finalize_result
        url = "https://example.com/articles/index"
        answer = {"url": url, "success": True, "markdown": "SENSITIVE_TEXT",
                  "link_continuation": {"link_id": "A" * 32},
                  "links": [{"url": "https://example.com/private-path"}]}
        with patch.object(document_pages, "_CACHE", OrderedDict()):
            result = _finalize_result(url, answer, policy_changed=False)
        self.assertEqual(result["status"], "continuation_evicted")
        self.assertFalse(result["success"])
        self.assertNotIn("SENSITIVE_TEXT", str(result))
        self.assertNotIn("private-path", str(result))
        self.assertNotIn("link_id", result)

    async def test_single_fetch_rejects_immediately_evicted_link_handle(self):
        url = "https://example.com/articles/index"
        answer = {"url": url, "success": True, "markdown": "SENSITIVE_TEXT",
                  "truncated": False,
                  "link_continuation": {"link_id": "A" * 32},
                  "links": [{"url": "https://example.com/hidden"}]}
        with (patch.object(document_pages, "_CACHE", OrderedDict()),
              patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge, "_check_public_target", return_value=None),
              patch.object(bridge, "_run_fetch", return_value=answer)):
            result = bridge.web_fetch(url, include_links=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "continuation_evicted")
        self.assertNotIn("SENSITIVE_TEXT", str(result))
        self.assertNotIn("hidden", str(result))
        self.assertNotIn("link_id", result)

    async def test_retention_skips_oversized_link_and_backfills_shorter_entries(self):
        base = "https://example.com/"
        # A ranked candidate might be larger than the remaining byte
        # allowance. The preparer must still inspect later candidates.
        page = {"links": {"internal": [
            {"href": f"/articles/first-{i}", "text": "Research report"}
            for i in range(24)
        ] + [
            {"href": "/articles/" + ("x" * 1720) + str(i),
             "text": "Research report"} for i in range(70)
        ] + [{"href": "/articles/shortuseful", "text": "Research report"}]}}
        selected = structured_extraction.extract(
            page, base, include_links=True, include_tables=False)["links"]
        retained = structured_extraction.prepare_link_continuation(page, base, selected)
        self.assertIsNotNone(retained)
        self.assertLessEqual(retained["size_bytes"], 96_000)
        self.assertIn("https://example.com/articles/shortuseful",
                      [row["url"] for row in retained["entries"]])

    async def test_retention_limits_and_lifetime_fail_closed(self):
        links = {"internal": [
            {"href": "/articles/" + ("x" * 1700) + str(i),
             "text": "Research report"} for i in range(100)
        ]}
        source = "https://example.com/index"
        selected = structured_extraction.extract(
            {"links": links}, source, include_links=True, include_tables=False)["links"]
        payload = structured_extraction.prepare_link_continuation(
            {"links": links}, source, selected)
        self.assertIsNotNone(payload)
        self.assertLessEqual(payload["size_bytes"], 96_000)
        self.assertLessEqual(len(payload["entries"]), 96)
        self.assertTrue(payload["retained_truncated"])
        with (patch.object(document_pages, "_CACHE", OrderedDict()),
              patch.object(document_pages.time, "monotonic", return_value=100)):
            handle = document_pages.save_links(source, payload, source_url=source)
            first, original, proof = document_pages.read_links_with_security_context(
                handle, offset=0, max_links=10)
            self.assertTrue(first["success"])
            self.assertEqual(original, source)
            self.assertIsNone(proof)
            self.assertLessEqual(len(json.dumps(first, ensure_ascii=True).encode()), 20_000)
            self.assertGreater(len(first["links"]), 0)
        with (patch.object(document_pages.time, "monotonic",
                           return_value=100 + document_pages.TTL_SECONDS),
              patch.object(document_pages, "_CACHE", OrderedDict([(handle, (
                  100 + document_pages.TTL_SECONDS, source, "", {}, source,
                  None, document_pages.PAGE_CHARS, payload))]))):
            expired, original, proof = document_pages.read_links_with_security_context(handle)
            self.assertEqual(expired["status"], "expired_or_unknown")
            self.assertIsNone(original)
            self.assertIsNone(proof)


if __name__ == "__main__":
    unittest.main()
