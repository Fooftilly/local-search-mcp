"""Mocks the upstream HTTP API to prove unsafe crawls never reach POST."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
import os
import socket
import asyncio
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import httpx

# The isolated bridge loader temporarily patches sys.modules. Preload these
# shared singletons before the patch so standalone test execution uses the
# same policy/cache/document modules as the bridge under test.
import domain_policy
import document_pages
import request_cache
import upstream_settings

class FakeFastMCP:
    def __init__(self, *_args, **_kwargs):pass
    def tool(self, **_kwargs):
        return lambda func: func

class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType('fastmcp')
        fake.FastMCP = FakeFastMCP
        path=Path(__file__).resolve().parents[1] / 'bridge.py'
        spec=importlib.util.spec_from_file_location('bridge_under_test',path)
        cls.module=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'fastmcp':fake}),patch('crawler_security.local_container_warning',return_value=None):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.module.request_cache.reset_for_test()
        self.addCleanup(self.module.request_cache.reset_for_test)
        # Never let a policy check performed by the bridge touch the
        # developer's real ~/.local/state domain approval database.
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": state.name,
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)


    def test_ambiguous_idn_cannot_reach_crawl4ai_even_with_twin_approved(self):
        # The agent supplies the original Unicode URL; policy must reject it
        # before a browser could navigate a different punycode host.
        domain_policy.decide("fass.de", "allow")
        with (patch.object(domain_policy.socket, "getaddrinfo",
                           side_effect=AssertionError("no DNS on rejected URL")),
              patch.object(self.module, "_run_fetch",
                           side_effect=AssertionError("Crawl4AI must not run"))):
            result = self.module.web_fetch("https://faß.de/path?secret=1")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("Ambiguous IDN", result["reason"])
        self.assertEqual(domain_policy.counts()["pending"], 0)

    def test_saved_upstream_timeouts_reach_live_http_clients(self):
        with tempfile.TemporaryDirectory() as config_home, patch.dict(os.environ, {
            "XDG_CONFIG_HOME": config_home,
        }):
            state = upstream_settings.dashboard_settings()["state"]
            upstream_settings.update_settings({
                "searxng_seconds": "9", "crawl_seconds": "110",
            }, state)
            observed = []
            original = httpx.Client

            def handler(request):
                if request.url.path == "/search":
                    return httpx.Response(200, json={"results": []})
                if request.url.path == "/health":
                    # Deliberate upstream version failure; do not invoke crawl.
                    return httpx.Response(200, json={"status": "ok", "version": "0.9.4"})
                self.fail("Unexpected upstream path: " + request.url.path)

            def factory(**kwargs):
                observed.append(kwargs["timeout"])
                return original(transport=httpx.MockTransport(handler), trust_env=False)

            with patch.object(self.module.httpx, "Client", side_effect=factory):
                self.assertEqual(
                    self.module._run_search({"q": "test", "format": "json"}, 8), [])
                result = self.module._run_fetch("https://example.com/a", "raw")
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(observed, [9, 110])
            # Subsequent requests in the same process see updated settings.
            state = upstream_settings.dashboard_settings()["state"]
            upstream_settings.update_settings({
                "searxng_seconds": "12", "crawl_seconds": "90",
            }, state)
            observed.clear()
            with patch.object(self.module.httpx, "Client", side_effect=factory):
                self.module._run_search({"q": "again", "format": "json"}, 8)
                self.module._run_fetch("https://example.com/a", "raw")
            self.assertEqual(observed, [12, 90])

    def test_opt_in_structured_fetch_uses_existing_crawl_and_separate_cache(self):
        posted = []
        original_client = httpx.Client
        page = {
            "url": "https://example.com/articles/index",
            "success": True,
            "markdown": "An ordinary article",
            "links": {"internal": [
                {"href": "/procurement", "text": "Tender documents", "title": "Notice"},
                {"href": "javascript:alert(1)", "text": "unsafe"},
                {"href": "http://127.0.0.1/admin", "text": "private"},
            ], "external": [{"href": "https://outside.example/item", "text": "External"}]},
            "tables": [{"headers": ["Lot", "Price"],
                        "rows": [["Laptop", "1000"], ["Monitor", "180"]]}],
        }
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            self.assertEqual(request.url.path, "/crawl")
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={"results": [page]})
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler))
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module.httpx, "Client", side_effect=client)):
            simple = self.module.web_fetch("https://example.com/articles/index")
            again = self.module.web_fetch("https://example.com/articles/index")
            links = self.module.web_fetch(
                "https://example.com/articles/index", include_links=True)
            both = self.module.web_fetch(
                "https://example.com/articles/index",
                include_links=True, include_tables=True)
            tables = self.module.web_fetch(
                "https://example.com/articles/index", include_tables=True)
        self.assertEqual(simple, again)
        self.assertNotIn("links", simple)
        self.assertNotIn("tables", simple)
        self.assertIn("links", links)
        self.assertNotIn("tables", links)
        self.assertEqual([x["url"] for x in links["links"]],
                         ["https://example.com/procurement",
                          "https://outside.example/item"])
        self.assertEqual(both["tables"][0]["rows"][0], ["Laptop", "1000"])
        self.assertIn("tables", tables)
        self.assertNotIn("links", tables)
        self.assertEqual(len(posted), 4)
        # Opt-in structured extraction does not add browser or crawler config,
        # probe extra links, or silently change default cached result shape.
        self.assertTrue(all(x["urls"] == ["https://example.com/articles/index"]
                            for x in posted))

    def test_structured_data_not_returned_on_blocked_or_failed_fetch(self):
        calls = []
        original_client = httpx.Client
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"version": "0.9.5", "status": "ok"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/missing", "success": True,
                "status_code": 404, "markdown": "Oops",
                "links": {"external": [{"href": "https://other.example/private"}]},
                "tables": [{"headers": ["Private"], "rows": [["Do not disclose"]]}],
            }]})
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler))
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module.httpx, "Client", side_effect=client)):
            result = self.module.web_fetch("https://example.com/missing",
                                           include_links=True, include_tables=True)
        self.assertEqual(result["status"], "http_error")
        self.assertNotIn("links", result)
        self.assertNotIn("tables", result)
        self.assertEqual(calls, ["/health", "/crawl"])
        with (patch.object(self.module, "_check_public_target", return_value={
                  "url": "https://private.invalid", "success": False, "status": "blocked",
                  "reason": "Not approved",
              }),
              patch.object(self.module.httpx, "Client",
                           side_effect=AssertionError("blocked must not touch crawler"))):
            denied = self.module.web_fetch("https://private.invalid",
                                            include_links=True, include_tables=True)
        self.assertEqual(denied["status"], "blocked")
        self.assertNotIn("links", denied)

    def test_structured_batch_and_invalid_options(self):
        original_client = httpx.Client
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            target = json.loads(request.content)["urls"][0]
            return httpx.Response(200, json={"results": [{
                "url": target, "success": True, "markdown": "A page",
                "links": {"internal": [{"href": "/related", "text": "Related"}]},
                "tables": [{"headers": ["H"], "rows": [["R"]]}],
            }]})
        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(handler))
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module.httpx, "Client", side_effect=client)):
            data = asyncio.run(self.module.web_fetch_batch(
                ["https://example.com/a", "https://example.com/b"],
                include_links=True, include_tables=True))
        self.assertEqual(data["count"], 2)
        for row in data["results"]:
            self.assertTrue(row["result"]["success"])
            self.assertEqual(row["result"]["links"][0]["url"],
                             "https://example.com/related")
            self.assertEqual(row["result"]["tables"][0]["rows"], [["R"]])
        with patch.object(self.module.httpx, "Client") as c:
            for options in ({"include_links": "true"}, {"include_tables": 1}):
                with self.assertRaises(ValueError):
                    self.module.web_fetch("https://example.com/a", **options)
                with self.assertRaises(ValueError):
                    asyncio.run(self.module.web_fetch_batch(["https://example.com/a"],
                                                             **options))
            c.assert_not_called()

    def test_opt_in_search_dedupe_rank_and_cache_isolation(self):
        calls = []
        rows = [
            {"title": "First", "url": "https://EXAMPLE.org:443/post#part",
             "content": "first snippet", "engine": "qwant"},
            {"title": "Other", "url": "https://other.example/path",
             "content": "unrelated", "engine": "bing"},
            {"title": "Alternate", "url": "https://example.org/post",
             "content": "second snippet", "engine": "yahoo"},
            {"title": "Third", "url": "https://third.example/", "engine": "brave"},
        ]
        original = httpx.Client
        def handler(request):
            calls.append(dict(request.url.params))
            return httpx.Response(200, json={"results": rows})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            default = self.module.web_search("dedupe lookup", max_results=2)
            unique = self.module.web_search("dedupe lookup", max_results=2,
                                            deduplicate=True)
            ranked = self.module.web_search("dedupe lookup", max_results=2,
                                            deduplicate=True, ranking="source_count")
            self.assertEqual(self.module.web_search(
                "dedupe lookup", max_results=2, deduplicate=True), unique)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(params == {"q": "dedupe lookup", "format": "json"}
                            for params in calls))
        self.assertEqual([x["title"] for x in default], ["First", "Other"])
        self.assertEqual(set(default[0]), {"title", "url", "snippet", "engine"})
        self.assertEqual([x["title"] for x in unique], ["First", "Other"])
        self.assertEqual(unique[0]["source_engines"], ["qwant", "yahoo"])
        self.assertEqual(unique[0]["source_matches"][0]["title"], "Alternate")
        self.assertEqual(unique[1]["source_matches"], [])
        self.assertEqual([x["title"] for x in ranked], ["First", "Other"])
        self.assertEqual(ranked[0]["source_count"], 2)

    def test_search_dedupe_invalid_options_do_not_contact_backend(self):
        cases = [
            {"deduplicate": 1},
            {"deduplicate": "true"},
            {"ranking": "not_real"},
            {"ranking": "source_count"},
            {"ranking": 2, "deduplicate": True},
        ]
        for options in cases:
            with self.subTest(options=options):
                with patch.object(self.module.httpx, "Client") as client:
                    with self.assertRaises(ValueError):
                        self.module.web_search("test", **options)
                    client.assert_not_called()

    def test_opt_in_publication_date_preserves_search_transport_and_cache(self):
        original = httpx.Client
        calls = []
        def handler(request):
            calls.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [
                {"title": "Published result", "url": "https://example.com/post",
                 "content": "Public information", "engine": "yahoo",
                 "publishedDate": "2024-10-23T12:00:38"},
                {"title": "Undated", "url": "https://example.com/other",
                 "content": "No upstream date", "engine": "yahoo",
                 "publishedDate": None},
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(self.module.httpx, "Client", side_effect=client):
            standard = self.module.web_search("publication sample")
            dated = self.module.web_search(
                "publication sample", include_published_date=True)
            cached = self.module.web_search(
                "publication sample", include_published_date=True)
            standard_cached = self.module.web_search("publication sample")
            with self.assertRaisesRegex(ValueError, "include_published_date"):
                self.module.web_search(
                    "publication sample", include_published_date=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls, [{"q": "publication sample", "format": "json"}] * 2)
        self.assertEqual(set(standard[0]), {"title", "url", "snippet", "engine"})
        self.assertEqual(standard_cached, standard)
        self.assertEqual(dated, cached)
        self.assertEqual(dated[0]["published_date"], "2024-10-23")
        self.assertEqual(dated[0]["date_source"], "searxng.publishedDate")
        self.assertNotIn("published_date", dated[1])


    def test_mixed_category_and_engine_selectors_fail_before_upstream(self):
        cases = [
            {"categories": "images", "engines": "bing images"},
            {"categories": "news", "engines": "bing news"},
            {"engines": "yahoo", "query": "!google private query"},
            {"engines": "yahoo", "query": "normal !images content"},
        ]
        for options in cases:
            with self.subTest(options=options), patch.object(
                    self.module.httpx, "Client") as client:
                with self.assertRaises(ValueError):
                    self.module.web_search(**({"query": "public info"} | options))
                client.assert_not_called()

    def test_engine_filter_requires_enabled_config_before_search(self):
        paths = []
        original = httpx.Client
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "bing images", "enabled": True,
                     "categories": ["images"]},
                    {"name": "google cse images", "enabled": False,
                     "categories": ["images"]},
                ]})
            return httpx.Response(200, json={"results": [
                {"url": "https://example.org/image", "title": "Allowed",
                 "content": "", "engine": "bing images"}
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            with self.assertRaisesRegex(ValueError, "unknown or disabled"):
                self.module.web_search("image check", engines="never-exists")
            with self.assertRaisesRegex(ValueError, "unknown or disabled"):
                self.module.web_search("image check", engines="google cse images")
            result = self.module.web_search(
                "image check", engines="bing images")
            cached = self.module.web_search(
                "image check", engines="bing images")
        self.assertEqual(result, cached)
        self.assertEqual(result[0]["engine"], "bing images")
        self.assertEqual(paths, ["/config", "/config", "/config", "/search"])

    def test_engine_filter_accepts_large_bounded_default_inventory(self):
        original = httpx.Client
        paths = []
        inventory = [
            {"name": f"engine-{n}", "enabled": False}
            for n in range(280)
        ] + [{"name": "bing images", "enabled": True}]
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": inventory})
            return httpx.Response(200, json={"results": [
                {"title": "Bing", "url": "https://example.com/image",
                 "engine": "bing images"}
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            result = self.module.web_search(
                "large inventory", engines="bing images")
        self.assertEqual(result[0]["engine"], "bing images")
        self.assertEqual(paths, ["/config", "/search"])

    def test_engine_filter_bad_inventory_fails_closed_without_search(self):
        original = httpx.Client
        samples = [
            httpx.Response(503, json={"detail": "backend unavailable"}),
            httpx.Response(200, json={"engines": {"wrong": True}}),
            httpx.Response(200, json={"engines": [
                {"name": "bing images", "enabled": "true"}]}),
            httpx.Response(200, content=b"x" * (self.module.MAX_SEARCH_CONFIG_BYTES + 1)),
        ]
        for response in samples:
            with self.subTest(status=response.status_code):
                paths = []
                def handler(request):
                    paths.append(request.url.path)
                    return response
                def client(**kwargs):
                    return original(transport=httpx.MockTransport(handler))
                with patch.object(self.module.httpx, "Client", side_effect=client):
                    result = self.module.web_search(
                        "config validation", engines="bing images", fresh=True)
                self.assertEqual(result["status"], "invalid_response")
                self.assertEqual(paths, ["/config"])

    def test_engine_filter_does_not_return_wrong_provider_or_cache(self):
        paths = []
        original = httpx.Client
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "bing images", "enabled": True}
                ]})
            return httpx.Response(200, json={"results": [
                {"title": "Wrong provider", "engine": "google cse images",
                 "url": "https://example.org/page"}]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            first = self.module.web_search("unexpected provider", engines="bing images")
            second = self.module.web_search("unexpected provider", engines="bing images")
        self.assertEqual(first["status"], "upstream_selection_mismatch")
        self.assertEqual(second["status"], "upstream_selection_mismatch")
        self.assertEqual(paths, ["/config", "/search"] * 2)
        self.assertFalse(self.module.request_cache._CACHE)

    def test_engine_selector_detects_unrequested_source_before_link_filter(self):
        # An unusable link does not undo evidence of an unintended provider:
        # a different engine still ran upstream and the selection failed.
        original = httpx.Client
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "qwant", "enabled": True}
                ]})
            return httpx.Response(200, json={"results": [
                {"engine": "google cse images", "url": "javascript:alert(1)"},
                {"engine": "qwant", "url": "https://example.org/valid",
                 "title": "Usable link"}
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            result = self.module.web_search("source isolation", engines="qwant")
        self.assertEqual(result["status"], "upstream_selection_mismatch")
        self.assertEqual(calls, ["/config", "/search"])
        self.assertNotIn("Usable link", str(result))
        self.assertFalse(self.module.request_cache._CACHE)

    def test_search_results_are_cached_per_filter_with_fresh_bypass(self):
        seen = []
        def handler(request):
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "qwant", "enabled": True, "categories": ["general"]}
                ]})
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"results": [
                {"title": "Result", "url": "https://example.com/a",
                 "content": "OK",
                 "engine": "qwant" if request.url.params.get("engines") else "mock"}
            ]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            self.assertEqual(len(self.module.web_search("cache probe")), 1)
            self.assertEqual(len(self.module.web_search("cache probe")), 1)
            self.assertEqual(len(seen), 1)
            self.module.web_search("cache probe", engines="qwant")
            self.assertEqual(len(seen), 2)
            self.module.web_search("cache probe", fresh=True)
            self.assertEqual(len(seen), 3)
            self.module.web_search("cache probe", max_results=2)
            self.assertEqual(len(seen), 4)
            self.module.web_search("cache probe", page=2)
            self.assertEqual(len(seen), 5)
            self.module.web_search("cache probe", language="en")
            self.module.web_search("cache probe", categories="news")
            self.module.web_search("cache probe", time_range="week")
            self.assertEqual(len(seen), 8)
            # Never retain obviously secret-bearing search queries.
            keys_before = set(self.module.request_cache._CACHE)
            self.module.web_search("password=secret", fresh=False)
            self.module.web_search("password=secret", fresh=False)
            self.assertEqual(len(seen), 10)
            self.assertEqual(set(self.module.request_cache._CACHE), keys_before)

    def test_post_fetch_authorization_is_read_only_and_requires_no_dns(self):
        import domain_policy as policy
        with patch.object(self.module.document_pages, "_CACHE", {}):
            policy.decide("example.org", "allow")
            token, _ = self.module.document_pages.save(
                "https://example.org/long", "READ_ONLY_PAGES" * 2000, {})
            def fake_fetch(url, _mode, _fresh):
                return {"url": url, "success": True, "markdown": "READ_ONLY_BATCH"}
            with (patch.object(policy.socket, "getaddrinfo",
                               side_effect=AssertionError("unexpected DNS lookup")),
                  patch.object(self.module, "web_fetch", side_effect=fake_fetch)):
                result = asyncio.run(self.module.web_fetch_batch(
                    ["https://example.org/one", "https://example.org/two"]))
                self.assertTrue(all(row["result"]["success"]
                                    for row in result["results"]))
                self.assertTrue(self.module.web_fetch_page(token, 2)["success"])
            entry = next(r for r in policy.entries() if r["domain"] == "example.org")
            self.assertEqual(entry["request_count"], 0)

    def test_expired_grant_redacts_failed_crawler_markdown_and_metadata(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns)):
            policy.decide("example.org", "allow", expires_minutes=60)
            clock[0] = "2026-10-17T12:59:59Z"

            def failed_crawler(_url, _mode):
                clock[0] = "2026-10-17T13:00:00Z"
                return {"url": "https://example.org/fail",
                        "success": False, "status": "crawl_failed",
                        "markdown": "FAILED_CRAWL_SECRET",
                        "metadata": {"title": "FAILED_CRAWL_TITLE"}}
            with patch.object(self.module, "_run_fetch", side_effect=failed_crawler):
                result = self.module.web_fetch("https://example.org/fail")
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("FAILED_CRAWL_SECRET", str(result))
            self.assertNotIn("FAILED_CRAWL_TITLE", str(result))

    def test_batch_redacts_expired_failed_result_with_content(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        early_done = Event()

        def fake_fetch(url, _mode, _fresh):
            if url.endswith("/failed"):
                early_done.set()
                return {"url": url, "success": False,
                        "status": "crawl_failed",
                        "markdown": "BATCH_FAILED_SECRET",
                        "metadata": {"title": "FAILED_TITLE"}}
            self.assertTrue(early_done.wait(5))
            clock[0] = "2026-10-17T13:00:00Z"
            return {"url": url, "success": True, "markdown": "SAFE_RESULT"}

        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns),
              patch.object(self.module, "web_fetch", side_effect=fake_fetch)):
            policy.decide("example.org", "allow", expires_minutes=60)
            policy.decide("example.net", "allow")
            clock[0] = "2026-10-17T12:59:59Z"
            result = asyncio.run(self.module.web_fetch_batch([
                "https://example.org/failed", "https://example.net/slow"
            ]))
            rows = [x["result"] for x in result["results"]]
            self.assertEqual(rows[0]["status"], "blocked")
            self.assertNotIn("BATCH_FAILED_SECRET", str(result))
            self.assertNotIn("FAILED_TITLE", str(result))
            self.assertEqual(rows[1]["markdown"], "SAFE_RESULT")

    def test_batch_wait_for_other_host_does_not_leak_expired_early_result(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        early_done = Event()

        def fake_fetch(url, _mode, _fresh):
            if url.endswith("/early"):
                early_done.set()
                return {"url": url, "success": True,
                        "markdown": "EARLY_TIMED_SECRET"}
            self.assertTrue(early_done.wait(5))
            clock[0] = "2026-10-17T13:00:00Z"
            return {"url": url, "success": True,
                    "markdown": "SAFE_SLOW_CONTENT"}

        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns),
              patch.object(self.module, "web_fetch", side_effect=fake_fetch)):
            policy.decide("example.org", "allow", scope="exact",
                          expires_minutes=60)
            policy.decide("example.net", "allow")
            clock[0] = "2026-10-17T12:59:59Z"
            result = asyncio.run(self.module.web_fetch_batch([
                "https://example.org/early", "https://example.net/slow"
            ]))
            rows = [x["result"] for x in result["results"]]
            self.assertEqual(rows[0]["status"], "blocked")
            self.assertNotIn("EARLY_TIMED_SECRET", str(result))
            self.assertEqual(rows[1]["markdown"], "SAFE_SLOW_CONTENT")

    def test_batch_redirect_final_host_expiry_redacts_paged_handle(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        early_done = Event()

        def fake_fetch(url, _mode, _fresh):
            if url.endswith("/start"):
                token, _ = self.module.document_pages.save(
                    "https://final.example.net/doc",
                    "REDIRECT_TOKEN_SECRET" * 2000, {},
                    source_url=url)
                early_done.set()
                return {"url": "https://final.example.net/doc",
                        "success": True, "markdown": "FIRST_PAGE_SECRET",
                        "truncated": True, "document_id": token}
            self.assertTrue(early_done.wait(5))
            clock[0] = "2026-10-17T13:00:00Z"
            return {"url": url, "success": True, "markdown": "SAFE_CONTENT"}

        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns),
              patch.object(self.module, "web_fetch", side_effect=fake_fetch),
              patch.object(self.module.document_pages, "_CACHE", {})):
            policy.decide("source.example.org", "allow")
            policy.decide("final.example.net", "allow", expires_minutes=60)
            policy.decide("safe.example.com", "allow")
            clock[0] = "2026-10-17T12:59:59Z"
            result = asyncio.run(self.module.web_fetch_batch([
                "https://source.example.org/start",
                "https://safe.example.com/slow"
            ]))
            rows = [x["result"] for x in result["results"]]
            self.assertEqual(rows[0]["status"], "blocked")
            self.assertNotIn("REDIRECT_TOKEN_SECRET", str(result))
            self.assertNotIn("FIRST_PAGE_SECRET", str(result))
            self.assertNotIn("document_id", str(rows[0]))
            self.assertEqual(rows[1]["markdown"], "SAFE_CONTENT")

    def test_document_pages_recheck_expiring_rule_and_reapproval(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        text = "PAGED_EXPIRED_CONTENT_" * 2000
        original = httpx.Client

        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.org/long", "success": True,
                "markdown": text
            }]})

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))

        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns),
              patch.object(self.module.httpx, "Client", side_effect=client),
              patch.object(self.module.document_pages, "_CACHE", {})):
            policy.decide("example.org", "allow", scope="exact",
                          expires_minutes=60)
            clock[0] = "2026-10-17T12:59:59Z"
            first = self.module.web_fetch("https://example.org/long")
            self.assertTrue(first["success"])
            token = first["document_id"]
            self.assertIn("PAGED_EXPIRED_CONTENT", self.module.web_fetch_page(
                token, 2)["markdown"])
            clock[0] = "2026-10-17T13:00:00Z"
            expired = self.module.web_fetch_page(token, 2)
            self.assertFalse(expired["success"])
            self.assertNotIn("PAGED_EXPIRED_CONTENT", str(expired))
            self.assertEqual(policy.request(
                "example.org", "https://example.org/long")[0], "pending")
            policy.clear_pending_requests()
            removed = self.module.web_fetch_page(token, 2)
            self.assertFalse(removed["success"])
            self.assertNotIn("PAGED_EXPIRED_CONTENT", str(removed))
            policy.decide("example.org", "allow", scope="exact")
            reapproved = self.module.web_fetch_page(token, 2)
            # Once the host is authorized again, the still-live process-local
            # page token works; no unnecessary second Crawl4AI crawl.
            self.assertTrue(reapproved["success"])
            self.assertIn("PAGED_EXPIRED_CONTENT", reapproved["markdown"])

    def test_document_redirect_rechecks_source_and_final_permission(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        text = "REDIRECT_PAGE_SECRET_" * 2000
        original = httpx.Client

        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://destination.example.net/long", "success": True,
                "markdown": text
            }]})

        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))

        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns),
              patch.object(self.module.httpx, "Client", side_effect=client),
              patch.object(self.module.document_pages, "_CACHE", {})):
            policy.decide("source.example.org", "allow", scope="exact",
                          expires_minutes=60)
            policy.decide("destination.example.net", "allow", scope="exact")
            clock[0] = "2026-10-17T12:59:59Z"
            initial = self.module.web_fetch("https://source.example.org/start")
            self.assertTrue(initial["success"])
            token = initial["document_id"]
            self.assertIn("REDIRECT_PAGE_SECRET",
                          self.module.web_fetch_page(token, 2)["markdown"])
            clock[0] = "2026-10-17T13:00:00Z"
            refused = self.module.web_fetch_page(token, 2)
            self.assertFalse(refused["success"])
            self.assertNotIn("REDIRECT_PAGE_SECRET", str(refused))
            # Final host remains authorized, but expired original host blocks.

    def test_unrelated_approval_preserves_document_but_source_deny_blocks(self):
        import domain_policy as policy
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        with (patch.object(policy.socket, "getaddrinfo", return_value=dns),
              patch.object(self.module.document_pages, "_CACHE", {})):
            policy.decide("example.org", "allow")
            token, _ = self.module.document_pages.save(
                "https://example.org/doc", "SENSITIVE" * 4000, {})
            self.assertTrue(self.module.web_fetch_page(token, 2)["success"])
            policy.decide("unrelated.net", "allow", scope="exact")
            self.assertTrue(self.module.web_fetch_page(token, 2)["success"])
            policy.decide("example.org", "deny")
            denied = self.module.web_fetch_page(token, 2)
            self.assertFalse(denied["success"])
            self.assertNotIn("SENSITIVE", str(denied))

    def test_inflight_fetch_started_before_expiry_cannot_release_content_after(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns)):
            policy.decide("example.org", "allow", expires_minutes=60)
            clock[0] = "2026-10-17T12:59:59Z"

            def slow_crawler(_url, _mode):
                clock[0] = "2026-10-17T13:00:00Z"
                return {"url": "https://child.example.org/data", "success": True,
                        "markdown": "EXPIRED_CONTENT_SENTINEL", "truncated": False}

            old_epoch = self.module.request_cache.policy_epoch()
            with patch.object(self.module, "_run_fetch", side_effect=slow_crawler):
                response = self.module.web_fetch("https://child.example.org/data")
            self.assertEqual(response["status"], "blocked")
            self.assertNotIn("EXPIRED_CONTENT_SENTINEL", str(response))
            self.assertEqual(self.module.request_cache.policy_epoch(), old_epoch)

    def test_expired_child_allow_cannot_bypass_active_parent_deny(self):
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns)):
            policy.decide("example.org", "deny")
            policy.decide("child.example.org", "allow", scope="exact",
                          expires_minutes=60)
            clock[0] = "2026-10-17T12:59:59Z"

            def slow_crawler(_url, _mode):
                clock[0] = "2026-10-17T13:00:00Z"
                return {"url": "https://child.example.org/page",
                        "success": True, "markdown": "DENIED_CONTENT_SENTINEL",
                        "truncated": False}

            with patch.object(self.module, "_run_fetch", side_effect=slow_crawler):
                result = self.module.web_fetch("https://child.example.org/page")
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("DENIED_CONTENT_SENTINEL", str(result))
            self.assertEqual(policy.request(
                "child.example.org", "https://child.example.org/page")[0],
                "denied")

    def test_clearing_expired_pending_blocks_inflight_fetch(self):
        """Clear pending must not erase the expiry evidence for an old crawl."""
        import domain_policy as policy
        clock = ["2026-10-17T12:00:00Z"]
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                ("8.8.8.8", 443))]
        crawled = Event()
        release = Event()

        def slow_crawler(_url, _mode):
            crawled.set()
            self.assertTrue(release.wait(timeout=5))
            return {"url": "https://example.org/page", "success": True,
                    "markdown": "EXPIRED_PENDING_SECRET", "truncated": False}

        with (patch.object(policy, "utcnow", side_effect=lambda: clock[0]),
              patch.object(policy.socket, "getaddrinfo", return_value=dns),
              patch.object(self.module, "_run_fetch", side_effect=slow_crawler)):
            policy.decide("example.org", "allow",
                          scope="exact", expires_minutes=60)
            clock[0] = "2026-10-17T12:59:59Z"
            before = self.module.request_cache.policy_epoch()
            with ThreadPoolExecutor(max_workers=1) as executor:
                waiting_fetch = executor.submit(
                    self.module.web_fetch, "https://example.org/page")
                try:
                    self.assertTrue(crawled.wait(timeout=5))
                    clock[0] = "2026-10-17T13:00:00Z"
                    self.assertEqual(policy.request(
                        "example.org", "https://example.org/page")[0], "pending")
                    self.assertEqual(self.module.request_cache.policy_epoch(), before)
                    self.assertEqual(policy.clear_pending_requests(), 1)
                    self.assertEqual(self.module.request_cache.policy_epoch(), before + 1)
                    self.assertNotIn(
                        "example.org", {row["domain"] for row in policy.entries()})
                finally:
                    release.set()
                result = waiting_fetch.result(timeout=5)

            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("EXPIRED_PENDING_SECRET", str(result))

    def test_cross_host_redirect_result_is_not_cached(self):
        final = {"url": "https://redirect.example.org/landing",
                 "success": True, "markdown": "public redirect content",
                 "truncated": False}
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module, "_run_fetch", return_value=final) as crawl):
            first = self.module.web_fetch("https://example.com/start")
            second = self.module.web_fetch("https://example.com/start")
            self.assertEqual(first, second)
            self.assertEqual(crawl.call_count, 2)
            self.assertFalse(self.module.request_cache._CACHE)

        # Same-host redirects remain cacheable because the requested host's
        # approval is rechecked before every cache hit.
        same_host = dict(final, url="https://example.com/landing")
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module, "_run_fetch", return_value=same_host) as crawl):
            self.module.web_fetch("https://example.com/start")
            self.module.web_fetch("https://example.com/start")
            self.assertEqual(crawl.call_count, 1)

    def test_fetch_cache_requires_policy_and_skips_sensitive_urls(self):
        counts = []
        def handler(request):
            counts.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [
                {"url": "https://example.com/page", "success": True, "markdown": "Hello"}
            ]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module.httpx, "Client", side_effect=client)):
            self.assertEqual(self.module.web_fetch("https://example.com/page")["markdown"], "Hello")
            self.assertEqual(self.module.web_fetch("https://example.com/page")["markdown"], "Hello")
            self.assertEqual(counts.count("/crawl"), 1)
            self.module.web_fetch("https://example.com/page", fresh=True)
            self.assertEqual(counts.count("/crawl"), 2)
            self.module.request_cache.invalidate_fetch()
            self.module.web_fetch("https://example.com/page")
            self.assertEqual(counts.count("/crawl"), 3)
            self.module.web_fetch("https://example.com/page?x=1")
            self.module.web_fetch("https://example.com/page?x=1")
            self.assertEqual(counts.count("/crawl"), 5)
            # An empty ?/# still changes the browser-visible URL; neither
            # may reuse /page's cached response or populate that cache key.
            for url in ("https://example.com/page?", "https://example.com/page#",
                        "https://example.com/page?#"):
                self.module.web_fetch(url)
                self.module.web_fetch(url)
            self.assertEqual(counts.count("/crawl"), 11)
            self.assertEqual(len(self.module.request_cache._CACHE), 1)
        with patch.object(self.module, "_check_public_target",
                          return_value={"success": False, "status": "denied"}), \
             patch.object(self.module.httpx, "Client") as backend:
            self.assertEqual(self.module.web_fetch("https://example.com/page")["status"], "denied")
            backend.assert_not_called()

    def test_search_missing_results_envelope_not_cached(self):
        seen = []
        original = httpx.Client
        def handler(request):
            seen.append(True)
            return httpx.Response(200, json={"unexpected": "response"})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            first = self.module.web_search("missing-results")
            second = self.module.web_search("missing-results")
        self.assertEqual(first["status"], "invalid_response")
        self.assertEqual(second["status"], "invalid_response")
        self.assertEqual(len(seen), 2)

    def test_all_malformed_search_rows_fail_explicitly_and_are_not_cached(self):
        for bad_rows in ([None], ["not-an-object", 17], [None, [], False]):
            self.module.request_cache.reset_for_test()
            seen = []
            original = httpx.Client
            def handler(request):
                seen.append(True)
                return httpx.Response(200, json={"results": bad_rows})
            def client(**kwargs):
                return original(transport=httpx.MockTransport(handler))
            with (self.subTest(bad_rows=bad_rows),
                  patch.object(self.module.httpx, "Client", side_effect=client)):
                first = self.module.web_search("malformed-results")
                second = self.module.web_search("malformed-results")
                self.assertEqual(first["status"], "invalid_response")
                self.assertEqual(first["backend"], "searxng")
                self.assertEqual(second["status"], "invalid_response")
                self.assertEqual(len(seen), 2)
                self.assertFalse(self.module.request_cache._CACHE)

    def test_search_invalid_upstream_links_are_skipped_before_result_limit(self):
        """Search must not give agents missing, unsafe or unencodable links."""
        original = httpx.Client
        rows = [
            {"title": "Missing URL", "content": "bad"},
            {"url": None, "title": "Null URL"},
            {"url": "javascript:alert(1)", "title": "Executable link"},
            {"url": "https://user:secret@example.org/", "title": "Credentials"},
            {"url": "https://example.org/\ud800", "title": "Broken Unicode"},
            {"url": "https://example.org/\ninvalid", "title": "Newline"},
            {"url": "https://example.org/a\x00b", "title": "NUL"},
            {"url": "https://example.org/a\x7fb", "title": "DEL"},
            {"url": "https://example.org:8080/", "title": "Unsupported port"},
            {"url": "https://bad host.example.org/", "title": "Hostname space"},
            {"url": "https://host<angle>.example.org/", "title": "Hostname angle brackets"},
            {"url": "https://host>angle.example.org/", "title": "Hostname greater than"},
            {"url": "https://bad_name.example.org/", "title": "Underscore in hostname"},
            {"url": "https://localhost/", "title": "Local-only hostname"},
            {"url": "http://127.0.0.1/", "title": "IP address"},
            {"url": "https://co.uk/", "title": "Bare public suffix"},
            {"url": "https://faß.de/", "title": "Ambiguous IDN"},
            {"url": "https://example.org/leading ", "title": "Whitespace"},
            {"url": "x" * 3000, "title": "Too long"},
            {"url": "https://example.org/first", "title": "Valid first"},
            {"url": "https://example.org/second", "title": "Valid second"},
        ]
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(
                200, content=json.dumps({"results": rows}, ensure_ascii=True).encode("utf-8"),
                headers={"content-type": "application/json"})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            results = self.module.web_search("search-link-validation", max_results=2)
            self.assertEqual([row["title"] for row in results],
                             ["Valid first", "Valid second"])
            self.assertEqual(len(calls), 1)

    def test_search_domain_policy_filter_is_pure_without_approval_or_dns(self):
        """Search results must match fetch host validation without changing policy."""
        original = httpx.Client
        rows = [
            {"url": "https://bad host.example.org/", "title": "Bad host"},
            {"url": "https://faß.de/", "title": "Ambiguous IDN"},
            {"url": "https://example.org:443/ok", "title": "Valid"},
        ]
        def handler(request):
            return httpx.Response(200, json={"results": rows})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with (patch.object(self.module.httpx, "Client", side_effect=client),
              patch.object(domain_policy, "request",
                           side_effect=AssertionError("search must not queue domains")),
              patch.object(domain_policy.socket, "getaddrinfo",
                           side_effect=AssertionError("search must not resolve DNS"))):
            results = self.module.web_search("search policy is read only", fresh=True)
        self.assertEqual(results, [{
            "title": "Valid", "url": "https://example.org:443/ok",
            "snippet": "", "engine": "",
        }])

    def test_search_all_unusable_links_return_uncached_failure(self):
        original = httpx.Client
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"results": [
                {"url": "file:///etc/passwd", "title": "Not a web page"},
                {"url": None, "title": "No link"},
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            first = self.module.web_search("all-invalid-links")
            second = self.module.web_search("all-invalid-links")
        self.assertEqual(first["status"], "invalid_response")
        self.assertEqual(second["status"], "invalid_response")
        self.assertEqual(len(calls), 2)  # Do not cache a broken search.

    def test_schemeless_urls_bypass_private_search_cache(self):
        hits = []
        original = httpx.Client
        def handler(request):
            hits.append(True)
            return httpx.Response(200, json={"results": []})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            self.module.web_search("example.com/private")
            self.module.web_search("example.com/private")
        self.assertEqual(len(hits), 2)
        self.assertFalse(self.module.request_cache._CACHE)

    def test_invalid_crawl_envelopes_are_never_cached(self):
        for malformed in ({"success": True, "results": []},
                          {"success": True, "results": {}},
                          {"results": [{}]}, {"results": []}):
            self.module.request_cache.reset_for_test()
            hits = []
            original = httpx.Client
            def handler(request):
                if request.url.path == "/health":
                    return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
                hits.append(True)
                return httpx.Response(200, json=malformed)
            def client(**kwargs):
                return original(transport=httpx.MockTransport(handler))
            with self.subTest(malformed=malformed), \
                 patch.object(self.module, "_check_public_target", return_value=None), \
                 patch.object(self.module.httpx, "Client", side_effect=client):
                one = self.module.web_fetch("https://example.com/page")
                two = self.module.web_fetch("https://example.com/page")
            self.assertEqual(one["status"], "invalid_response")
            self.assertEqual(two["status"], "invalid_response")
            self.assertEqual(hits, [True, True])

    def test_http_502_crawl_post_gets_one_bounded_retry(self):
        events = []
        original = httpx.Client
        def handler(request):
            events.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            if events.count("/crawl") == 1:
                return httpx.Response(502, json={"error": "unavailable"})
            return httpx.Response(200, json={"results": [
                {"url": "https://example.com/a", "success": True, "markdown": "ok"}
            ]})
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client), \
             patch.object(self.module.time, "sleep", return_value=None):
            result = self.module.web_fetch("https://example.com/a")
        self.assertTrue(result["success"])
        self.assertEqual(events, ["/health", "/crawl", "/crawl"])

    def test_searxng_empty_unresponsive_engines_are_not_cached(self):
        hits = []
        original = httpx.Client
        def handler(request):
            hits.append(True)
            return httpx.Response(200, json={
                "results": [],
                "unresponsive_engines": [{"engine": "qwant", "error": "Timeout"}],
            })
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            a = self.module.web_search("broken engines")
            b = self.module.web_search("broken engines")
        self.assertEqual(a["status"], "upstream_unavailable")
        self.assertEqual(b["status"], "upstream_unavailable")
        self.assertEqual(len(hits), 2)

    def test_upstream_transient_errors_are_structured_and_retry_bounded(self):
        seen = []
        def search_handler(request):
            seen.append(request.url.path)
            return httpx.Response(429, json={"private": "not returned"})
        original = httpx.Client
        def client_search(**kwargs):
            return original(transport=httpx.MockTransport(search_handler))
        with patch.object(self.module.httpx, "Client", side_effect=client_search), \
             patch.object(self.module.time, "sleep", return_value=None):
            failed = self.module.web_search("retry-case")
        self.assertEqual(failed["status"], "rate_limited")
        self.assertEqual(failed["http_status"], 429)
        self.assertEqual(seen, ["/search", "/search"])
        attempts = []
        def fetch_handler(request):
            attempts.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(503, json={"private": "not returned"})
        def client_fetch(**kwargs):
            return original(transport=httpx.MockTransport(fetch_handler))
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client_fetch), \
             patch.object(self.module.time, "sleep", return_value=None):
            failure = self.module.web_fetch("https://example.com/page")
        self.assertEqual(failure["status"], "upstream_unavailable")
        self.assertEqual(attempts, ["/health", "/crawl", "/crawl"])
        self.assertNotIn("private", str(failure))
        # A failed call was not cached.
        self.assertFalse(self.module.request_cache._CACHE)

    def test_crawl_read_timeout_does_not_retry_expensive_post(self):
        attempts = []
        original = httpx.Client
        def handler(request):
            attempts.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            raise httpx.ReadTimeout("Slow crawl response", request=request)
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client):
            response = self.module.web_fetch("https://example.com/slow")
        self.assertEqual(response["status"], "upstream_unavailable")
        self.assertEqual(attempts, ["/health", "/crawl"])

    def test_crawl4ai_health_retries_once_then_validates_version(self):
        seen = []
        def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/health" and seen.count("/health") == 1:
                return httpx.Response(503, json={"status": "unhealthy"})
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [
                {"url": "https://example.com/health", "success": True, "markdown": "OK"}
            ]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module.httpx, "Client", side_effect=client),
              patch.object(self.module.time, "sleep", return_value=None)):
            result = self.module.web_fetch("https://example.com/health")
        self.assertTrue(result["success"])
        self.assertEqual(seen, ["/health", "/health", "/crawl"])

    def test_policy_epoch_change_during_crawl_fails_closed(self):
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            self.module.request_cache.invalidate_fetch()
            return httpx.Response(200, json={"results": [
                {"url": "https://example.com/a", "success": True, "markdown": "secret"}
            ]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client):
            result = self.module.web_fetch("https://example.com/a")
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("secret", str(result))
        self.assertFalse(self.module.request_cache._CACHE)

    def test_search_old_signature_preserves_default_request_and_shape(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"results": [
                {"title": "A", "url": "https://example.com", "content": "Snippet", "engine": "bing"},
                {"title": "B", "url": "https://example.net", "content": "Text", "engine": "qwant"},
            ]})
        original = httpx.Client
        def client(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            results = self.module.web_search("  query  ", 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], {"title": "A", "url": "https://example.com",
                                      "snippet": "Snippet", "engine": "bing"})
        self.assertEqual(calls[0].url.path, "/search")
        self.assertEqual(dict(calls[0].url.params), {"q": "query", "format": "json"})

    def test_search_options_map_to_local_searxng_with_bounded_results(self):
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "qwant", "enabled": True, "categories": ["general"]},
                    {"name": "yahoo", "enabled": True, "categories": ["general"]},
                ]})
            return httpx.Response(200, json={"results": [
                {"title": f"Result {i}", "url": f"https://example.org/{i}",
                 "content": "S" * 1500, "engine": "qwant"} for i in range(20)
            ]})
        original = httpx.Client
        def client(**kwargs): return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            results = self.module.web_search(
                "sample", 20, language="en-US", time_range="month",
                engines="qwant, yahoo", page=3,
            )
        self.assertEqual(len(results), 12)
        self.assertEqual(len(results[0]["snippet"]), 1100)
        self.assertEqual([call.url.path for call in calls], ["/config", "/search"])
        self.assertEqual(dict(calls[-1].url.params), {
            "q": "sample", "format": "json", "language": "en-US",
            "time_range": "month", "engines": "qwant,yahoo", "pageno": "3",
        })
        self.assertEqual(calls[-1].url.host, httpx.URL(self.module.SEARXNG_URL).host)

    def test_searxng_engine_dots_and_language_aliases(self):
        seen = []
        def handler(request):
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": x, "enabled": True, "categories": ["general"]}
                    for x in ("brave.news", "crates.io", "pkg.go.dev")
                ]})
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"results": []})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            self.module.web_search("q", engines="brave.news,crates.io", language="auto")
            self.module.web_search("q", engines="pkg.go.dev", language="fr-CA")
            self.module.web_search("q", language="all")
        self.assertEqual(seen[0]["engines"], "brave.news,crates.io")
        self.assertEqual(seen[0]["language"], "auto")
        self.assertEqual(seen[1]["engines"], "pkg.go.dev")
        self.assertEqual(seen[1]["language"], "fr-CA")
        self.assertEqual(seen[2]["language"], "all")

    def test_invalid_search_options_do_not_contact_upstream(self):
        cases = (
            {"language": "en&format=html"},
            {"language": "en_US"},
            {"language": "EN"},
            {"language": "zh-Hant-TW"},
            {"language": "es-419"},  # Upstream VALID_LANGUAGE_CODE rejects numeric regions.
            {"language": "autox"},
            {"categories": "news,,images"},
            {"categories": "news,news"},
            {"categories": "malicious;url"},
            {"engines": "qwant,google!"},
            {"engines": "e" * 41},
            {"time_range": "forever"},
            {"page": 0},
            {"page": 11},
            {"page": True},
            {"page": "2"},
            {"max_results": "12"},
            {"query": ""},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), \
                 patch.object(self.module.httpx, "Client") as client:
                with self.assertRaises(ValueError):
                    self.module.web_search(**({"query": "hello"} | kwargs))
                client.assert_not_called()

    def test_search_backend_bad_filter_response_is_explicit(self):
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(
                lambda request: httpx.Response(422, json={"detail": "Unsupported filter"})
            ))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            with self.assertRaisesRegex(ValueError, "rejected the requested filters"):
                self.module.web_search("test", categories="custom")

    def test_search_handles_malformed_result_rows_without_crashing(self):
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"results": [
                    None, {"title": "Valid", "url": "https://example.com", "content": "ok"},
                    None, {"title": "Also valid", "url": "https://example.net", "content": "ok"}
                ]})
            ))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            results = self.module.web_search("test", max_results=2)
        self.assertEqual([r["title"] for r in results], ["Valid", "Also valid"])

    def test_web_find_in_existing_document_requires_no_crawler(self):
        from collections import OrderedDict
        import domain_policy as policy
        text = ("a" * (self.module.document_pages.PAGE_CHARS - 6) +
                "PUBLIC CALL" + "z" * self.module.document_pages.PAGE_CHARS)
        with (patch.object(self.module.document_pages, "_CACHE", OrderedDict()),
              patch.object(self.module.httpx, "Client",
                           side_effect=AssertionError("find must not crawl")),
              patch.object(policy.socket, "getaddrinfo",
                           side_effect=AssertionError("find must not use DNS"))):
            policy.decide("example.org", "allow", scope="exact")
            token, _ = self.module.document_pages.save(
                "https://example.org/long", text, {"title": "Public call"})
            result = self.module.web_find(token, "public call")
            self.assertTrue(result["success"])
            self.assertEqual(result["match_count"], 1)
            self.assertEqual(result["matches"][0]["page"], 1)
            self.assertIn("PUBLIC CALL", result["matches"][0]["excerpt"])
            self.assertEqual(result["page_count"], 3)
            self.assertTrue(self.module.web_fetch_page(token, 2)["success"])
            self.assertFalse(self.module.web_find(token, "nothing")["has_more"])

    def test_web_find_revalidates_revocation_and_redirect_domains(self):
        from collections import OrderedDict
        import domain_policy as policy
        text = "SECRET_FIND_MATCH_" * 2000
        with (patch.object(self.module.document_pages, "_CACHE", OrderedDict()),
              patch.object(self.module.httpx, "Client",
                           side_effect=AssertionError("no network"))):
            policy.decide("source.example.org", "allow", scope="exact")
            policy.decide("target.example.net", "allow", scope="exact")
            token, _ = self.module.document_pages.save(
                "https://target.example.net/doc", text, {},
                source_url="https://source.example.org/start")
            self.assertTrue(self.module.web_find(token, "SECRET_FIND_MATCH")["success"])
            policy.decide("unrelated.example.org", "allow", scope="exact")
            self.assertTrue(self.module.web_find(token, "SECRET_FIND_MATCH")["success"])
            policy.decide("target.example.net", "deny", scope="exact")
            refused = self.module.web_find(token, "SECRET_FIND_MATCH")
            self.assertEqual(refused["status"], "blocked")
            self.assertNotIn("SECRET_FIND_MATCH", str(refused))
            policy.decide("target.example.net", "allow", scope="exact")
            policy.decide("source.example.org", "deny", scope="exact")
            refused = self.module.web_find(token, "SECRET_FIND_MATCH")
            self.assertEqual(refused["status"], "blocked")
            self.assertNotIn("SECRET_FIND_MATCH", str(refused))

    def test_web_find_consumed_once_proof_is_rechecked_not_reused(self):
        from collections import OrderedDict
        import domain_policy as policy
        secret = "ONCE_ONLY_EXCERPT_" * 1600
        with (patch.object(self.module.document_pages, "_CACHE", OrderedDict()),
              patch.object(self.module.httpx, "Client",
                           side_effect=AssertionError("no network")),
              patch.object(policy, "_once_proof_current", return_value=True) as proof):
            token, _ = self.module.document_pages.save(
                "https://example.org/doc", secret, {},
                once_proof=("example.org", "a" * 32))
            valid = self.module.web_find(token, "ONCE_ONLY_EXCERPT")
            self.assertTrue(valid["success"])
            self.assertEqual(proof.call_count, 1)
            proof.return_value = False
            expired = self.module.web_find(token, "ONCE_ONLY_EXCERPT")
            self.assertEqual(expired["status"], "blocked")
            self.assertNotIn("ONCE_ONLY_EXCERPT", str(expired))
            # There is no new one-use grant consumption or approval request.
            self.assertEqual(proof.call_count, 2)

    def test_web_find_blocks_mid_search_policy_epoch_change(self):
        from collections import OrderedDict
        import domain_policy as policy
        text = "SENSITIVE_MATCH_" * 2000
        with patch.object(self.module.document_pages, "_CACHE", OrderedDict()):
            policy.decide("example.org", "allow", scope="exact")
            token, _ = self.module.document_pages.save(
                "https://example.org/doc", text, {})
            actual = self.module.document_pages.find_with_security_context
            def policy_changes(*args):
                result = actual(*args)
                policy.decide("unrelated.example.org", "allow", scope="exact")
                return result
            with patch.object(self.module.document_pages, "find_with_security_context",
                              side_effect=policy_changes):
                blocked = self.module.web_find(token, "SENSITIVE_MATCH")
            self.assertEqual(blocked["status"], "blocked")
            self.assertNotIn("SENSITIVE_MATCH", str(blocked))

    def test_web_find_audit_never_persists_bearer_handle_or_query(self):
        from collections import OrderedDict
        import history_log
        import domain_policy as policy
        with (patch.object(self.module.document_pages, "_CACHE", OrderedDict()),
              patch.object(history_log, "append_history") as audit):
            policy.decide("example.org", "allow", scope="exact")
            token, _ = self.module.document_pages.save(
                "https://example.org/doc", "PRIVATE_TERM_" * 3000, {})
            answer = self.module.web_find(token, "PRIVATE_TERM")
            self.assertTrue(answer["success"])
            event = audit.call_args.args[0]
            self.assertEqual(event["kind"], "page")
            self.assertEqual(event["action"], "find")
            self.assertNotIn("page", event)
            self.assertNotIn(token, str(event))
            self.assertNotIn("PRIVATE_TERM", str(event))

    def test_long_approved_page_can_be_read_without_second_crawl(self):
        from collections import OrderedDict
        content = "a" * 24000 + "b" * 24000 + "tail"
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/long", "success": True,
                "metadata": {"title": "A long document",
                             "canonical_url": "https://example.com/long?secret=yes",
                             "description": "Summary"},
                "markdown": {"raw_markdown": content, "fit_markdown": "fit content"}
            }]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client), \
             patch.object(self.module.document_pages, "_CACHE", OrderedDict()):
            result = self.module.web_fetch("https://example.com/long")
            self.assertEqual(result["markdown"], "a" * 24000)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["page_count"], 3)
            self.assertEqual(result["next_page"], 2)
            self.assertEqual(result["metadata"]["canonical_url"], "https://example.com/long")
            self.assertEqual(len(result["document_id"]), 32)
            second = self.module.web_fetch_page(result["document_id"], page=2)
            third = self.module.web_fetch_page(result["document_id"], page=3)
            self.assertEqual(result["markdown"] + second["markdown"] + third["markdown"], content)
            self.assertFalse(third["truncated"])
            self.assertIsNone(third["next_page"])
            self.assertEqual(calls, ["/health", "/crawl"])

    def test_extraction_modes_empty_fit_and_oversized_content(self):
        from collections import OrderedDict
        selected = ["raw"]
        request_configs = []
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            import json
            request_configs.append(json.loads(request.content)["crawler_config"]["params"])
            obj = {"raw_markdown": "Raw text", "fit_markdown": "Fit text"} if selected[0] == "raw" else {
                "raw_markdown": "Z" * (self.module.document_pages.MAX_DOCUMENT_CHARS + 1)}
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/test", "success": True,
                "markdown": obj,
                "metadata": {"canonical_url": "file:///etc/passwd", "title": "<untrusted>"}
            }]})
        original = httpx.Client
        def client(**kwargs): return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client), \
             patch.object(self.module.document_pages, "_CACHE", OrderedDict()):
            self.assertEqual(self.module.web_fetch("https://example.com/test")["markdown"], "Raw text")
            fitted = self.module.web_fetch("https://example.com/test", mode="fit")
            self.assertEqual(fitted["markdown"], "Fit text")
            self.assertEqual(fitted["metadata"]["title"], "<untrusted>")
            self.assertNotIn("canonical_url", fitted["metadata"])
            self.assertEqual(request_configs[0], {"stream": False})
            generator = request_configs[1]["markdown_generator"]
            self.assertEqual(generator["type"], "DefaultMarkdownGenerator")
            self.assertEqual(generator["params"]["content_filter"]["type"],
                             "PruningContentFilterLXML")
            selected[0] = "huge"
            # A changed mocked upstream must bypass the intentional short-page
            # fetch cache. Callers can use fresh=True for this exact purpose.
            huge = self.module.web_fetch("https://example.com/test", fresh=True)
            self.assertEqual(huge["status"], "document_too_large")
            self.assertNotIn("markdown", huge)
            missing = self.module.web_fetch("https://example.com/test", mode="fit", fresh=True)
            self.assertEqual(missing["status"], "unavailable_extraction")
        with patch.object(self.module, "_check_public_target") as target:
            with self.assertRaisesRegex(ValueError, "mode must be"):
                self.module.web_fetch("https://example.com", mode="not-real")
            target.assert_not_called()

    def test_metadata_falls_back_when_nested_title_is_unusable(self):
        metadata = self.module._crawl_metadata({
            "metadata": {"title": "", "description": None},
            "title": "Top level title",
            "description": "Top level description",
        }, "https://example.com/")
        self.assertEqual(metadata["title"], "Top level title")
        self.assertEqual(metadata["description"], "Top level description")

    def test_oversized_metadata_is_bounded_before_serialization(self):
        import json
        metadata = self.module._crawl_metadata({
            "metadata": {
                "title": "  " + "x" * 239 + "\ud800" + "tail" * 100_000,
                "description": "z" * 999 + "\udfff" + "tail" * 100_000,
            }
        }, "https://example.com/long")
        self.assertEqual(metadata["title"], "x" * 239 + "?")
        self.assertEqual(metadata["description"], "z" * 999 + "?")
        self.assertEqual(len(metadata["title"]), 240)
        self.assertEqual(len(metadata["description"]), 1000)
        json.dumps(metadata, ensure_ascii=False).encode("utf-8")

    def test_unpaired_unicode_metadata_serializes_for_initial_and_later_pages(self):
        import json
        from collections import OrderedDict

        # JSON escaping carries lone UTF-16 surrogates through Crawl4AI JSON;
        # their presence in an MCP response can break UTF-8 serialization.
        page = {"url": "https://example.com/long", "success": True,
                "markdown": "safe content " * 3000,
                "metadata": {"title": "Bad\ud800title",
                             "description": "Bad\udfffdescription",
                             "canonical_url": "https://example.com/path\ud800"}}
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            # Encode escaped surrogate sequences as ASCII JSON, exactly as a
            # permissive upstream decoder can expose them to the bridge.
            data = json.dumps({"results": [page]}, ensure_ascii=True).encode("ascii")
            return httpx.Response(200, content=data, headers={"content-type": "application/json"})

        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with (patch.object(self.module, "_check_public_target", return_value=None),
              patch.object(self.module.httpx, "Client", side_effect=client),
              patch.object(self.module.document_pages, "_CACHE", OrderedDict())):
            first = self.module.web_fetch("https://example.com/long")
            self.assertTrue(first["success"])
            self.assertTrue(first["truncated"])
            self.assertEqual(first["metadata"]["title"], "Bad?title")
            self.assertEqual(first["metadata"]["description"], "Bad?description")
            self.assertNotIn("canonical_url", first["metadata"])
            next_page = self.module.web_fetch_page(first["document_id"], 2)
            self.assertEqual(next_page["metadata"], first["metadata"])
            for response in (first, next_page):
                json.dumps(response, ensure_ascii=False).encode("utf-8")

        fallback = self.module._crawl_metadata(
            {"metadata": {"title": None},
             "title": "Fallback\ud800title"},
            "https://example.com")
        self.assertEqual(fallback["title"], "Fallback?title")

    def test_long_unapproved_redirect_is_never_cached(self):
        from collections import OrderedDict
        content = "private" * 6000
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com", "redirected_url": "https://internal.example.org/",
                "success": True, "markdown": content
            }]})
        original = httpx.Client
        def client(**kwargs): return original(transport=httpx.MockTransport(handler))
        def check(url):
            return {"status": "approval_required"} if "internal.example.org" in url else None
        with patch.object(self.module, "_check_public_target", side_effect=check), \
             patch.object(self.module.httpx, "Client", side_effect=client), \
             patch.object(self.module.document_pages, "_CACHE", OrderedDict()) as cache:
            result = self.module.web_fetch("https://example.com")
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("markdown", result)
            self.assertFalse(cache)

    def test_no_crawl_when_backend_old(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            self.assertEqual(request.url.path,'/health')
            return httpx.Response(200,json={'status':'ok','version':'0.9.4'})
        orig_client=httpx.Client
        def client(**kwargs):return orig_client(transport=httpx.MockTransport(handler))
        with patch.object(self.module, '_check_public_target',return_value=None),patch.object(self.module.httpx,'Client',side_effect=client):
            result=self.module.web_fetch('https://example.com')
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(calls,['GET'])

    def test_http_error_page_is_not_a_successful_source_or_cached(self):
        # Crawl4AI can successfully extract an HTTP error page: its 'success'
        # flag reports crawler completion, not HTTP status.
        for code in (404, 410, 429, 500):
            with self.subTest(code=code):
                self.module.request_cache.reset_for_test()
                posts = []
                def handler(request):
                    if request.url.path == "/health":
                        return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
                    posts.append(request.url.path)
                    return httpx.Response(200, json={"results": [{
                        "url": "https://example.com/notfound",
                        "success": True, "status_code": code,
                        "markdown": "MISLEADING_ERROR_PAGE",
                        "metadata": {"title": "Page not found"}
                    }]})
                original = httpx.Client
                def client(**kwargs):
                    return original(transport=httpx.MockTransport(handler))
                with patch.object(self.module, "_check_public_target", return_value=None), \
                     patch.object(self.module.httpx, "Client", side_effect=client):
                    first = self.module.web_fetch("https://example.com/notfound")
                    second = self.module.web_fetch("https://example.com/notfound")
                for response in (first, second):
                    self.assertFalse(response["success"])
                    self.assertEqual(response["status"], "http_error")
                    self.assertEqual(response["http_status"], code)
                    self.assertNotIn("markdown", response)
                    self.assertNotIn("metadata", response)
                    self.assertNotIn("document_id", response)
                self.assertEqual(posts, ["/crawl", "/crawl"])

    def test_redirect_http_status_uses_final_destination(self):
        for final_code, should_pass in ((200, True), (404, False)):
            with self.subTest(final_code=final_code):
                self.module.request_cache.reset_for_test()
                def handler(request):
                    if request.url.path == "/health":
                        return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
                    return httpx.Response(200, json={"results": [{
                        "url": "https://example.com/start",
                        "redirected_url": "https://example.com/finish",
                        "success": True, "status_code": 302,
                        "redirected_status_code": final_code,
                        "markdown": "FINAL_PAGE_BODY",
                    }]})
                original = httpx.Client
                def client(**kwargs):
                    return original(transport=httpx.MockTransport(handler))
                with patch.object(self.module, "_check_public_target", return_value=None), \
                     patch.object(self.module.httpx, "Client", side_effect=client):
                    answer = self.module.web_fetch("https://example.com/start")
                self.assertEqual(answer["success"], should_pass)
                self.assertEqual(answer["url"], "https://example.com/finish")
                if should_pass:
                    self.assertEqual(answer["markdown"], "FINAL_PAGE_BODY")
                    self.assertNotIn("http_status", answer)
                else:
                    self.assertEqual(answer["status"], "http_error")
                    self.assertEqual(answer["http_status"], 404)
                    self.assertNotIn("markdown", answer)

    def test_malformed_http_status_is_not_trusted_as_an_error_code(self):
        for raw_code in ("404", True, {"status": 404}, [404]):
            with self.subTest(raw_code=raw_code):
                self.module.request_cache.reset_for_test()
                def handler(request):
                    if request.url.path == "/health":
                        return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
                    return httpx.Response(200, json={"results": [{
                        "url": "https://example.com/legacy",
                        "success": True,
                        "status_code": raw_code,
                        "markdown": "Legacy page",
                    }]})
                original = httpx.Client
                def client(**kwargs):
                    return original(transport=httpx.MockTransport(handler))
                with patch.object(self.module, "_check_public_target", return_value=None), \
                     patch.object(self.module.httpx, "Client", side_effect=client):
                    answer = self.module.web_fetch("https://example.com/legacy")
                self.assertTrue(answer["success"])
                self.assertEqual(answer["markdown"], "Legacy page")
                self.assertNotIn("http_status", answer)

    def test_crawler_failure_has_safe_reason_and_no_partial_content(self):
        self.module.request_cache.reset_for_test()
        calls = []
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            calls.append("crawl")
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com/document.pdf", "success": False,
                "status_code": None, "error_message": "SECRET_HTTP_TOKEN=private",
                "markdown": "PARTIAL_DOCUMENT_SECRET",
                "metadata": {"title": "Misleading failed page"}
            }]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client):
            first = self.module.web_fetch("https://example.com/document.pdf")
            second = self.module.web_fetch("https://example.com/document.pdf")
        self.assertEqual(calls, ["crawl", "crawl"])
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "crawl_failed")
        self.assertFalse(first["success"])
        self.assertIn("Crawl4AI", first["reason"])
        self.assertNotIn("SECRET", str(first))
        self.assertNotIn("markdown", first)
        self.assertNotIn("metadata", first)

    def test_safe_backend_crawls_and_parses_markdown(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            if request.method=='GET':return httpx.Response(200,json={'status':'ok','version':'0.9.5'})
            return httpx.Response(200,json={'results':[{'url':'https://example.com', 'success':True,'markdown':'Hello'}]})
        orig_client=httpx.Client
        def client(**kwargs):return orig_client(transport=httpx.MockTransport(handler))
        with patch.object(self.module, '_check_public_target',return_value=None),patch.object(self.module.httpx,'Client',side_effect=client):
            result=self.module.web_fetch('https://example.com')
        self.assertTrue(result['success'])
        self.assertEqual(result['markdown'],'Hello')
        self.assertEqual(calls,['GET','POST'])

    def test_unapproved_redirect_content_not_returned(self):
        def handler(request):
            if request.method=='GET':return httpx.Response(200,json={'status':'ok','version':'0.9.5'})
            # Crawl4AI keeps the requested URL in "url" and puts its final
            # navigation (including JS redirects) in "redirected_url".
            return httpx.Response(200,json={'results':[{'url':'https://example.com',
                'redirected_url':'https://elsewhere.net/secret','success':True,'markdown':'sensitive'}]})
        orig_client=httpx.Client
        def client(**kwargs):return orig_client(transport=httpx.MockTransport(handler))
        def check(url):return {'status':'approval_required'} if 'elsewhere.net' in url else None
        with patch.object(self.module,'_check_public_target',side_effect=check),patch.object(self.module.httpx,'Client',side_effect=client):
            result=self.module.web_fetch('https://example.com')
        self.assertEqual(result['status'],'blocked')
        self.assertNotIn('markdown',result)

    def test_approved_redirect_is_returned_as_final_url(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com", "redirected_url": "https://docs.python.org/page",
                "success": True, "markdown": "approved"
            }]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        checked = []
        def check(url):
            checked.append(url)
            return None
        with patch.object(self.module, "_check_public_target", side_effect=check), \
             patch.object(self.module.httpx, "Client", side_effect=client):
            result = self.module.web_fetch("https://example.com")
        self.assertEqual(result["url"], "https://docs.python.org/page")
        self.assertEqual(result["markdown"], "approved")
        self.assertEqual(checked, ["https://example.com", "https://docs.python.org/page"])

    def test_non_string_redirected_url_fails_closed(self):
        for value in ({"href": "https://example.com"}, ["https://example.com"], 42, ""):
            def handler(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
                return httpx.Response(200, json={"results": [{
                    "url": "https://example.com", "redirected_url": value,
                    "success": True, "markdown": "do not expose"
                }]})
            original = httpx.Client
            def client(**kwargs):
                return original(transport=httpx.MockTransport(handler), trust_env=False)
            with self.subTest(redirected_url=value), \
                 patch.object(self.module, "_check_public_target", return_value=None), \
                 patch.object(self.module.httpx, "Client", side_effect=client):
                result = self.module.web_fetch("https://example.com")
                self.assertEqual(result["status"], "blocked")
                self.assertNotIn("markdown", result)

    def test_approved_nonredirected_page_remains_compatible(self):
        """Older Crawl4AI responses without redirected_url still work."""
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [{
                "url": "https://example.com", "success": True, "markdown": "normal"
            }]})
        original = httpx.Client
        def client(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(self.module, "_check_public_target", return_value=None), \
             patch.object(self.module.httpx, "Client", side_effect=client):
            result = self.module.web_fetch("https://example.com")
        self.assertTrue(result["success"])
        self.assertEqual(result["url"], "https://example.com")
        self.assertEqual(result["markdown"], "normal")

if __name__=='__main__':unittest.main()
