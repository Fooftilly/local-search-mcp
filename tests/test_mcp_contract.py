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
from fastmcp.exceptions import ToolError
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

    async def test_bounded_batch_fetch_mixed_outcomes_and_private_audit(self):
        good = "https://example.com/article?token=PRIVATE_QUERY_SENTINEL"
        other = "https://example.com/other"
        denied = "https://unapproved.example/path"
        broken = "https://example.com/timeout"
        inputs = [good, denied, good, broken, other]
        requests = []
        original = httpx.Client

        def policy(url):
            if url == denied:
                return {"url": url, "success": False, "status": "approval_required"}
            return None

        def handler(request: httpx.Request):
            requests.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            target = json.loads(request.content)["urls"][0]
            if target == broken:
                raise httpx.ReadTimeout("PRIVATE_EXCEPTION_SENTINEL", request=request)
            return httpx.Response(200, json={"results": [{
                "url": target, "success": True, "markdown": "safe text"}]})

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)

        with (patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge, "_check_public_target", side_effect=policy),
              patch.object(bridge.httpx, "Client", side_effect=client)):
            async with Client(bridge.mcp) as mcp_client:
                names = {tool.name for tool in await mcp_client.list_tools()}
                self.assertIn("web_fetch_batch", names)
                response = await mcp_client.call_tool("web_fetch_batch", {
                    "urls": inputs, "mode": "raw",
                })
                self.assertFalse(response.is_error)
                with self.assertRaisesRegex(ToolError, "list of 1 to 8 URLs"):
                    await mcp_client.call_tool("web_fetch_batch", {
                        "urls": [good] * 9,
                    })

        rows = response.data["results"]
        self.assertEqual(response.data["count"], 5)
        self.assertEqual([x["index"] for x in rows], list(range(5)))
        self.assertEqual([x["requested_url"] for x in rows], inputs)
        for row in rows:
            if "status" not in row["result"]:
                self.assertIs(row["result"].get("success"), True)
        self.assertEqual([x["result"].get("status", "success") for x in rows],
                         ["success", "approval_required", "success",
                          "upstream_unavailable", "success"])
        self.assertEqual(requests.count("/crawl"), 3)
        self.assertEqual(requests.count("/health"), 3)
        self.assertEqual(rows[0]["result"], rows[2]["result"])
        self.assertIsNot(rows[0]["result"], rows[2]["result"])
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        events = [json.loads(line) for line in audit.splitlines()]
        self.assertEqual(len(events), len(inputs))
        self.assertEqual([x["kind"] for x in events], ["fetch"] * len(inputs))
        self.assertNotIn("PRIVATE_QUERY_SENTINEL", audit)
        self.assertNotIn("PRIVATE_EXCEPTION_SENTINEL", audit)
        self.assertNotIn("safe text", audit)
        self.assertEqual(sum(x["url"] == "https://example.com/article" for x in events), 2)

    async def test_search_published_date_opt_in_over_actual_mcp(self):
        seen = []
        original = httpx.Client
        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [
                {"title": "A", "url": "https://example.org/article",
                 "engine": "yahoo", "publishedDate": "2024-10-23T12:00:38"},
                {"title": "B", "url": "https://example.org/no-date",
                 "engine": "yahoo", "publishedDate": None},
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=client):
            async with Client(bridge.mcp) as client:
                tool = next(tool for tool in await client.list_tools()
                            if tool.name == "web_search")
                self.assertEqual(
                    tool.inputSchema["properties"]["include_published_date"]["default"],
                    False)
                normal = await client.call_tool("web_search", {
                    "query": "published mcp validation"})
                dated = await client.call_tool("web_search", {
                    "query": "published mcp validation",
                    "include_published_date": True})
        self.assertFalse(normal.is_error)
        self.assertFalse(dated.is_error)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertNotIn("include_published_date", seen[0])
        self.assertNotIn("published_date", normal.data[0])
        self.assertEqual(dated.data[0]["published_date"], "2024-10-23")
        self.assertEqual(dated.data[0]["date_source"], "searxng.publishedDate")
        self.assertNotIn("published_date", dated.data[1])

    async def test_opt_in_search_status_reports_partial_provider_failures(self):
        original = httpx.Client
        seen = []
        failures = [
            ["yahoo", "timeout SECRET_PRIVATE_DETAIL"],
            ["bing images", "backend unavailable"],
            ["yahoo", "duplicate"],
            ["google\\nleak", "CREDENTIAL"],
        ]
        def handler(request):
            seen.append((request.url.path, dict(request.url.params)))
            return httpx.Response(200, json={
                "results": [
                    {"title": "Official reference",
                     "url": "https://example.org/reference",
                     "content": "Public source text", "engine": "qwant"},
                ], "unresponsive_engines": failures,
                "private_setting": "PRIVATE_TOKEN",
            })
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=factory):
            async with Client(bridge.mcp) as client:
                tools = {x.name: x for x in await client.list_tools()}
                self.assertFalse(tools["web_search"].inputSchema[
                    "properties"]["include_search_status"]["default"])
                usual = await client.call_tool("web_search", {"query": "public info"})
                enhanced = await client.call_tool("web_search", {
                    "query": "public info", "include_search_status": True})
                refreshed = await client.call_tool("web_search", {
                    "query": "public info", "include_search_status": True})
        self.assertFalse(usual.is_error)
        self.assertIsInstance(usual.data, list)
        self.assertEqual(set(usual.data[0]), {"title", "url", "snippet", "engine"})
        self.assertEqual(enhanced.data["results"], usual.data)
        self.assertTrue(enhanced.data["success"])
        self.assertEqual(enhanced.data["status"], "degraded")
        diagnostic = enhanced.data["search_status"]
        self.assertEqual(diagnostic["coverage"], "partial")
        self.assertEqual(diagnostic["reported_count"], 4)
        self.assertEqual(diagnostic["unresponsive_engines"],
                         ["yahoo", "bing images"])
        self.assertEqual(diagnostic["unparsed_count"], 1)
        self.assertFalse(diagnostic["truncated"])
        self.assertEqual(enhanced.data, refreshed.data)
        self.assertNotIn("PRIVATE_TOKEN", str(diagnostic))
        self.assertNotIn("SECRET_PRIVATE_DETAIL", str(diagnostic))
        self.assertEqual(len(seen), 3)
        audit = [json.loads(line) for line in
                 (Path(self.tmp.name) / "history.jsonl").read_text().splitlines()]
        searches = [row for row in audit if row.get("kind") == "search"]
        self.assertEqual(len(searches), 3)
        self.assertTrue(all(row["status"] == "success" for row in searches))
        self.assertTrue(all(row["result_count"] == 1 for row in searches))
        self.assertTrue(all(row["results"][0]["title"] == "Official reference"
                            for row in searches))
        self.assertTrue(all("error_type" not in row for row in searches))
        self.assertNotIn("PRIVATE_TOKEN", str(searches))
        self.assertNotIn("SECRET_PRIVATE_DETAIL", str(searches))
        self.assertNotIn("Public source text", str(searches))
        self.assertTrue(all(path == "/search" for path, _ in seen))
        self.assertTrue(all(params == {"q": "public info", "format": "json"}
                            for _, params in seen))

    async def test_search_status_reports_absence_and_does_not_change_failures(self):
        original = httpx.Client
        payloads = [
            {"results": [{"url": "https://example.org/a", "title": "A",
                          "engine": "yahoo"}]},
            {"results": [{"url": "https://example.org/b", "title": "B",
                          "engine": "yahoo"}],
             "unresponsive_engines": {"PRIVATE_SECRET": "wrong schema"}},
            {"results": [], "unresponsive_engines": [
                ["yahoo", "timeout SECRET"]]},
        ]
        seen = []
        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, json=payloads[len(seen) - 1])
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=factory):
            async with Client(bridge.mcp) as client:
                for _ in range(2):
                    response = await client.call_tool("web_search", {
                        "query": "public provider status",
                        "include_search_status": True})
                    self.assertFalse(response.is_error)
                    status = response.data["search_status"]
                    self.assertEqual(status["coverage"],
                                     "no_failures_reported" if len(seen) == 1
                                     else "unverifiable")
                    self.assertEqual(response.data["status"],
                                     "ok" if len(seen) == 1 else "unknown")
                    self.assertNotIn("PRIVATE_SECRET", str(status))
                failed = await client.call_tool("web_search", {
                    "query": "public provider status",
                    "include_search_status": True})
        self.assertEqual(failed.data["status"], "upstream_unavailable")
        self.assertFalse(failed.data["success"])
        self.assertNotIn("SECRET", str(failed.data))
        self.assertEqual(seen, ["/search"] * 3)

    async def test_search_dedupe_and_source_count_rank_over_real_mcp(self):
        urls = []
        original = httpx.Client
        def handler(request):
            urls.append(request.url)
            return httpx.Response(200, json={"results": [
                {"title": "Low", "url": "https://low.example/a",
                 "engine": "qwant", "content": "low text"},
                {"title": "High", "url": "https://high.example/post#one",
                 "engine": "bing", "content": "first source"},
                {"title": "Other", "url": "https://other.example/", "engine": "yahoo"},
                {"title": "High again", "url": "https://HIGH.example:443/post",
                 "engine": "brave", "content": "second source"},
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=client):
            async with Client(bridge.mcp) as client:
                tool = next(tool for tool in await client.list_tools()
                            if tool.name == "web_search")
                ranking_schema = tool.inputSchema["properties"]["ranking"]
                self.assertEqual(set(ranking_schema["enum"]),
                                 {"searxng", "source_count"})
                self.assertEqual(ranking_schema["default"], "searxng")
                self.assertIn("deduplicate=true", tool.description)
                self.assertIn("fresh=true", tool.description)
                standard = await client.call_tool("web_search", {
                    "query": "dedupe mcp", "max_results": 2,
                })
                enhanced = await client.call_tool("web_search", {
                    "query": "dedupe mcp", "max_results": 2,
                    "deduplicate": True, "ranking": "source_count",
                })
        self.assertFalse(standard.is_error)
        self.assertFalse(enhanced.is_error)
        self.assertEqual([r["title"] for r in standard.data], ["Low", "High"])
        self.assertEqual([r["title"] for r in enhanced.data], ["High", "Low"])
        self.assertEqual(enhanced.data[0]["source_count"], 2)
        self.assertEqual(enhanced.data[0]["source_engines"], ["bing", "brave"])
        self.assertEqual(enhanced.data[0]["source_matches"][0]["title"], "High again")
        self.assertEqual(enhanced.data[1]["source_matches"], [])
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("deduplicate" not in dict(url.params)
                            and "ranking" not in dict(url.params) for url in urls))

    async def test_search_result_domains_filter_over_real_mcp(self):
        original = httpx.Client
        paths = []
        def handler(request):
            paths.append(str(request.url))
            self.assertEqual(request.url.path, "/search")
            self.assertNotIn("allowed_result_domains", request.url.params)
            return httpx.Response(200, json={"results": [
                {"title": "Government", "url": "https://example.org/a",
                 "engine": "yahoo"},
                {"title": "Government subsite", "url": "https://news.example.org/b",
                 "engine": "yahoo"},
                {"title": "External", "url": "https://unrelated.org/c",
                 "engine": "yahoo"},
            ]})
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.domain_policy, "request",
                           side_effect=AssertionError("No approvals")),
              patch.object(bridge.domain_policy.socket, "getaddrinfo",
                           side_effect=AssertionError("No DNS lookups"))):
            async with Client(bridge.mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertIn("allowed_result_domains",
                              tools["web_search"].inputSchema["properties"])
                self.assertEqual(
                    tools["web_search"].inputSchema["properties"][
                        "allowed_result_domains"]["default"], "")
                exact = await client.call_tool("web_search", {
                    "query": "official reference", "allowed_result_domains": "example.org"})
                children = await client.call_tool("web_search", {
                    "query": "official reference", "allowed_result_domains": "*.example.org"})
                combo = await client.call_tool("web_search", {
                    "query": "official reference",
                    "allowed_result_domains": "example.org,*.example.org",
                    "include_search_status": True})
                none = await client.call_tool("web_search", {
                    "query": "official reference",
                    "allowed_result_domains": "another.org"})
        for value in (exact, children, combo, none):
            self.assertFalse(value.is_error)
        self.assertEqual([x["title"] for x in exact.data], ["Government"])
        self.assertEqual([x["title"] for x in children.data],
                         ["Government subsite"])
        self.assertEqual([x["title"] for x in combo.data["results"]],
                         ["Government", "Government subsite"])
        self.assertEqual(combo.data["status"], "ok")
        self.assertEqual(none.data, [])
        self.assertEqual(len(paths), 4)

    async def test_search_capabilities_read_only_live_inventory_over_mcp(self):
        original = httpx.Client
        requests = []
        config = {"engines": [
            {"name": "yahoo", "enabled": True, "categories": ["general"]},
            {"name": "duckduckgo news", "enabled": True, "categories": ["news"]},
            {"name": "bing images", "enabled": True,
             "categories": ["images", "general"]},
            {"name": "google cse images", "enabled": False,
             "categories": ["images"]},
        ], "secret_key": "NEVER_LEAK_CONFIG_SECRET"}
        def handler(request):
            requests.append(str(request.url))
            self.assertEqual(request.url.path, "/config")
            return httpx.Response(200, json=config)
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=AssertionError("Never wake sleeping backend"))):
            async with Client(bridge.mcp) as client:
                tools = {x.name: x for x in await client.list_tools()}
                self.assertIn("web_search_capabilities", tools)
                result = await client.call_tool("web_search_capabilities", {})
        self.assertFalse(result.is_error)
        data = result.data
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "available")
        self.assertEqual([e["name"] for e in data["enabled_engines"]],
                         ["bing images", "duckduckgo news", "yahoo"])
        self.assertEqual(data["categories"], ["general", "images", "news"])
        self.assertEqual(data["enabled_engine_count"], 3)
        self.assertNotIn("google cse images", str(data))
        self.assertNotIn("NEVER_LEAK_CONFIG_SECRET", str(data))
        self.assertEqual(len(requests), 1)

    async def test_long_valid_engine_names_do_not_hide_selectable_providers(self):
        long_disabled = "disabled-" + "a" * 45
        long_enabled = "enabled-" + "b" * 44
        self.assertGreater(len(long_disabled), 40)
        config = {"engines": [
            {"name": long_disabled, "enabled": False, "categories": ["news"]},
            {"name": long_enabled, "enabled": True, "categories": ["science"]},
            {"name": "yahoo", "enabled": True, "categories": ["general"]},
        ]}
        original = httpx.Client
        def handler(request):
            self.assertEqual(request.url.path, "/config")
            return httpx.Response(200, json=config)
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=factory):
            async with Client(bridge.mcp) as client:
                result = await client.call_tool("web_search_capabilities", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.data["status"], "available")
        self.assertEqual([e["name"] for e in result.data["enabled_engines"]],
                         ["yahoo"])
        self.assertEqual(result.data["categories"], ["general"])
        self.assertEqual(result.data["enabled_engine_count"], 1)
        self.assertEqual(result.data["omitted_unselectable_engine_count"], 1)
        self.assertNotIn(long_disabled, str(result.data))
        self.assertNotIn(long_enabled, str(result.data))

    async def test_search_capabilities_tolerates_valid_unselectable_upstream_labels(self):
        # SearXNG /config can include named engines with parentheses even
        # when they are disabled. Those are valid upstream, not valid MCP
        # engine selectors. An irrelevant disabled name must not hide Yahoo.
        original = httpx.Client
        config = {"engines": [
            {"name": "seznam (CZ)", "enabled": False,
             "categories": ["general"]},
            {"name": "naver (KO)", "enabled": True,
             "categories": ["general"]},
            {"name": "yahoo", "enabled": True,
             "categories": ["general", "q&a"]},
            {"name": "duckduckgo images", "enabled": True,
             "categories": ["images"]},
        ]}
        requests = []
        def handler(request):
            requests.append(str(request.url))
            self.assertEqual(request.url.path, "/config")
            return httpx.Response(200, json=config)
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler),
                            trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=AssertionError("No silent wake"))):
            async with Client(bridge.mcp) as client:
                result = await client.call_tool("web_search_capabilities", {})
        self.assertFalse(result.is_error)
        data = result.data
        self.assertEqual(data["status"], "available")
        self.assertEqual([row["name"] for row in data["enabled_engines"]],
                         ["duckduckgo images", "yahoo"])
        self.assertEqual(data["categories"], ["general", "images"])
        self.assertEqual(data["enabled_engine_count"], 2)
        self.assertEqual(data["omitted_unselectable_engine_count"], 1)
        self.assertEqual(data["omitted_unselectable_category_count"], 1)
        self.assertNotIn("naver (KO)", str(data))
        self.assertNotIn("seznam (CZ)", str(data))
        self.assertNotIn("q&a", str(data))
        self.assertEqual(len(requests), 1)

    async def test_search_capabilities_fail_closed_on_bad_or_absent_inventory(self):
        original = httpx.Client
        cases = [
            httpx.Response(503, json={"secret": "SECRET"}),
            httpx.Response(200, json={"engines": [
                {"name": "malicious\\nprovider", "categories": ["general"],
                 "enabled": True}]}),
            httpx.Response(200, json={"engines": [
                {"name": "yahoo", "categories": ["general"], "enabled": 1}]}),
            httpx.Response(200, json={"engines": [
                {"name": "yahoo", "categories": ["general"], "enabled": True},
                {"name": "yahoo", "categories": ["images"], "enabled": True}]}),
            httpx.Response(200, content=b"x" * (bridge.MAX_SEARCH_CONFIG_BYTES + 1)),
            httpx.Response(200, json={"engines": [
                {"name": "yahoo", "categories": ["general\\nunsafe"],
                 "enabled": True}]}),
            httpx.Response(200, json={"engines": []}),
        ]
        async with Client(bridge.mcp) as client:
            for response in cases:
                with self.subTest(response=response.status_code):
                    def handler(request):
                        self.assertEqual(request.url.path, "/config")
                        return response
                    def factory(**kwargs):
                        return original(transport=httpx.MockTransport(handler),
                                        trust_env=False)
                    with (patch.object(bridge.httpx, "Client", side_effect=factory),
                          patch.object(bridge.on_demand, "lease",
                                       side_effect=AssertionError("No wake"))):
                        result = await client.call_tool(
                            "web_search_capabilities", {})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.data["status"], "unavailable")
                    self.assertFalse(result.data["success"])
                    self.assertNotIn("SECRET", str(result.data))
            def error(_request):
                raise httpx.ConnectError("SECRET_LOCAL_BACKEND_ERROR")
            def failure_factory(**kwargs):
                return original(transport=httpx.MockTransport(error), trust_env=False)
            with patch.object(bridge.httpx, "Client", side_effect=failure_factory):
                result = await client.call_tool("web_search_capabilities", {})
            self.assertEqual(result.data["status"], "unavailable")
            self.assertNotIn("SECRET", str(result.data))

    async def test_optional_search_filters_are_exposed_over_real_mcp(self):
        seen = []
        def handler(request: httpx.Request):
            if request.url.path == "/config":
                seen.append({"path": "/config"})
                return httpx.Response(200, json={"engines": [
                    {"name": "qwant", "enabled": True, "categories": ["general"]},
                ]})
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
                    "engines": "qwant", "time_range": "day", "page": 2,
                })
                with self.assertRaisesRegex(
                        Exception, "Choose categories or engines"):
                    await client.call_tool("web_search", {
                        "query": "local query", "categories": "news",
                        "engines": "qwant",
                    })
        self.assertFalse(response.is_error)
        self.assertEqual(response.data[0]["engine"], "qwant")
        self.assertEqual(seen, [{"path": "/config"}, {
            "q": "local query", "format": "json", "language": "en",
            "time_range": "day", "engines": "qwant", "pageno": "2",
        }])

    async def test_web_find_match_offset_pagination_over_actual_mcp(self):
        from collections import OrderedDict
        content = "a" * (bridge.document_pages.PAGE_CHARS + 37)
        with (patch.object(bridge.document_pages, "_CACHE", OrderedDict()),
              patch.object(bridge.httpx, "Client",
                           side_effect=AssertionError("No crawler/search HTTP permitted"))):
            domain_policy.decide("example.com", "allow", scope="exact")
            token, count = bridge.document_pages.save(
                "https://example.com/long", content, {})
            self.assertEqual(count, 2)
            async with Client(bridge.mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertIn("web_find", tools)
                self.assertIn("start_offset", tools["web_find"].inputSchema["properties"])
                first = await client.call_tool("web_find", {
                    "document_id": token, "query": "A", "max_matches": 3,
                })
                self.assertFalse(first.is_error)
                self.assertEqual(
                    [m["offset"] for m in first.data["matches"]], [0, 1, 2])
                self.assertTrue(first.data["has_more"])
                self.assertEqual(first.data["next_offset"], 3)
                second = await client.call_tool("web_find", {
                    "document_id": token, "query": "a", "max_matches": 3,
                    "start_offset": first.data["next_offset"],
                })
                self.assertFalse(second.is_error)
                self.assertEqual(
                    [m["offset"] for m in second.data["matches"]], [3, 4, 5])
                self.assertEqual(second.data["next_offset"], 6)
                last = await client.call_tool("web_find", {
                    "document_id": token, "query": "A", "max_matches": 3,
                    "start_offset": len(content) - 2,
                })
                self.assertFalse(last.is_error)
                self.assertEqual(
                    [m["offset"] for m in last.data["matches"]],
                    [len(content) - 2, len(content) - 1])
                self.assertFalse(last.data["has_more"])
                self.assertIsNone(last.data["next_offset"])
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        self.assertNotIn(token, audit)
        self.assertNotIn(content[:500], audit)

    async def test_compact_fetch_reconstructs_complete_document_over_mcp(self):
        from collections import OrderedDict
        content = "# Intro\n" + "🧭" * 40 + "x" * (bridge.document_pages.PAGE_CHARS + 190)
        calls = []
        def handler(request: httpx.Request):
            calls.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/compact", "success": True,
                "markdown": {"raw_markdown": content}
            }]})
        original = httpx.Client
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        global_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                          ("93.184.215.14", 443))]
        with (patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(domain_policy.socket, "getaddrinfo", return_value=global_answer),
              patch.object(bridge.document_pages, "_CACHE", OrderedDict())):
            async with Client(bridge.mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                for name in ("web_fetch", "web_fetch_batch"):
                    self.assertIn("compact_chars", tools[name].inputSchema["properties"])
                    self.assertEqual(tools[name].inputSchema["properties"][
                        "compact_chars"]["default"], bridge.document_pages.PAGE_CHARS)
                first = await client.call_tool("web_fetch", {
                    "url": "https://example.com/compact", "compact_chars": 2048,
                    "include_outline": True,
                })
                self.assertFalse(first.is_error)
                self.assertTrue(first.data["success"])
                self.assertEqual(len(first.data["markdown"]), 2048)
                self.assertEqual(first.data["outline"][0]["page"], 1)
                self.assertEqual(first.data["page_count"], 2)
                page = await client.call_tool("web_fetch_page", {
                    "document_id": first.data["document_id"], "page": 2,
                })
                self.assertFalse(page.is_error)
                self.assertEqual(first.data["markdown"] + page.data["markdown"], content)
                self.assertIsNone(page.data["next_page"])
                find = await client.call_tool("web_find", {
                    "document_id": first.data["document_id"], "query": "x",
                    "start_offset": 2048,
                })
                self.assertFalse(find.is_error)
                self.assertEqual(find.data["matches"][0]["page"], 2)
            audit = (Path(self.tmp.name) / "history.jsonl").read_text()
            self.assertNotIn(first.data["document_id"], audit)
            self.assertNotIn(content[100:200], audit)
        self.assertEqual(calls.count("/crawl"), 1)

    async def test_retrieval_info_schema_and_cache_hit_over_actual_mcp(self):
        original = httpx.Client
        seen = []
        def handler(request: httpx.Request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            self.assertEqual(request.url.path, "/crawl")
            seen.append(1)
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/time", "success": True,
                "markdown": {"raw_markdown": "Public timestamp document"},
            }]})
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        global_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                          ("93.184.215.14", 443))]
        with (patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(domain_policy.socket, "getaddrinfo", return_value=global_answer)):
            async with Client(bridge.mcp) as client:
                tools = {x.name: x for x in await client.list_tools()}
                for name in ("web_fetch", "web_fetch_batch"):
                    schema = tools[name].inputSchema["properties"]
                    self.assertIn("include_retrieval_info", schema)
                    self.assertFalse(schema["include_retrieval_info"]["default"])
                one = await client.call_tool("web_fetch", {
                    "url": "https://example.com/time", "include_retrieval_info": True})
                two = await client.call_tool("web_fetch", {
                    "url": "https://example.com/time", "include_retrieval_info": True})
                legacy = await client.call_tool("web_fetch", {
                    "url": "https://example.com/time"})
                batch = await client.call_tool("web_fetch_batch", {
                    "urls": ["https://example.com/time"],
                    "include_retrieval_info": True})
        self.assertTrue(one.data["success"])
        self.assertFalse(one.data["retrieval_info"]["served_from_cache"])
        self.assertEqual(two.data["retrieval_info"]["source"], "cache_hit")
        self.assertTrue(two.data["retrieval_info"]["served_from_cache"])
        self.assertEqual(one.data["retrieval_info"]["retrieved_at"],
                         two.data["retrieval_info"]["retrieved_at"])
        self.assertNotIn("retrieval_info", legacy.data)
        self.assertEqual(batch.data["results"][0]["result"]["retrieval_info"][
            "source"], "cache_hit")
        self.assertEqual(seen, [1])
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        self.assertNotIn("retrieval_info", audit)
        self.assertNotIn("Public timestamp document", audit)

    async def test_table_continuation_over_real_mcp_and_private_audit(self):
        from collections import OrderedDict
        original = httpx.Client
        crawls = []
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            crawls.append(request.url.path)
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/tables", "success": True,
                "markdown": {"raw_markdown": "Short table page"},
                "tables": [{"headers": ["Code"], "rows": [[f"PRIVATE_CELL_{i}"]
                           for i in range(21)]} for _ in range(5)],
            }]})
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        global_answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                          ("93.184.215.14", 443))]
        with (patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(domain_policy.socket, "getaddrinfo", return_value=global_answer),
              patch.object(bridge.document_pages, "_CACHE", OrderedDict())):
            async with Client(bridge.mcp) as client:
                tools = {tool.name: tool for tool in await client.list_tools()}
                self.assertIn("web_table_page", tools)
                self.assertIn("table_index", tools["web_table_page"].inputSchema["properties"])
                first = await client.call_tool("web_fetch", {
                    "url": "https://example.com/tables", "include_tables": True})
                self.assertFalse(first.is_error)
                self.assertIn("table_continuation", first.data)
                handle = first.data["table_continuation"]["table_id"]
                rows = []
                offset = 0
                while True:
                    page = await client.call_tool("web_table_page", {
                        "table_id": handle, "table_index": 4,
                        "row_offset": offset, "max_rows": 8})
                    self.assertFalse(page.is_error)
                    rows.extend(x[0] for x in page.data["rows"])
                    if not page.data["has_more"]:
                        break
                    offset = page.data["next_offset"]
                self.assertEqual(rows, [f"PRIVATE_CELL_{i}" for i in range(21)])
                self.assertEqual(crawls, ["/crawl"])
            audit = (Path(self.tmp.name) / "history.jsonl").read_text()
            self.assertNotIn(handle, audit)
            self.assertNotIn("PRIVATE_CELL_", audit)
            events = [json.loads(line) for line in audit.splitlines()]
            self.assertTrue(any(entry["kind"] == "page"
                                and entry.get("action") == "table_page"
                                for entry in events))

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

    async def test_search_discards_unusable_links_on_actual_mcp_dispatch(self):
        """A valid MCP search cannot report an unsafe upstream link as a hit."""
        original = httpx.Client
        def handler(request):
            # Upstream JSON can legally represent an unpaired surrogate as
            # a \\u escape; httpx.Response(json=...) rejects it prematurely.
            response = {"results": [
                {"url": "javascript:alert(1)", "title": "Unsafe"},
                {"title": "Missing link"},
                {"url": "https://example.com/good", "title": "Good\ud800title",
                 "content": "Snippet\ud800"},
            ]}
            return httpx.Response(200, content=json.dumps(
                response, ensure_ascii=True).encode("utf-8"),
                headers={"content-type": "application/json"})
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(bridge.httpx, "Client", side_effect=factory):
            async with Client(bridge.mcp) as client:
                result = await client.call_tool("web_search", {
                    "query": "mcp malformed upstream links", "fresh": True,
                    "max_results": 2,
                })
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]["url"], "https://example.com/good")
        self.assertIn("?", result.data[0]["title"])
        json.dumps(result.data, ensure_ascii=False).encode("utf-8", "strict")

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
