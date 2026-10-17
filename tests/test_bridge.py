"""Mocks the upstream HTTP API to prove unsafe crawls never reach POST."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import httpx

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
            return httpx.Response(200, json={"results": [
                {"title": f"Result {i}", "url": f"https://example.org/{i}",
                 "content": "S" * 1500, "engine": "qwant"} for i in range(20)
            ]})
        original = httpx.Client
        def client(**kwargs): return original(transport=httpx.MockTransport(handler))
        with patch.object(self.module.httpx, "Client", side_effect=client):
            results = self.module.web_search(
                "sample", 20, language="en-US", categories="news, science",
                time_range="month", engines="qwant, yahoo", page=3,
            )
        self.assertEqual(len(results), 12)
        self.assertEqual(len(results[0]["snippet"]), 1100)
        self.assertEqual(dict(calls[0].url.params), {
            "q": "sample", "format": "json", "language": "en-US",
            "categories": "news,science", "time_range": "month",
            "engines": "qwant,yahoo", "pageno": "3",
        })
        self.assertEqual(calls[0].url.host, httpx.URL(self.module.SEARXNG_URL).host)

    def test_searxng_engine_dots_and_language_aliases(self):
        seen = []
        def handler(request):
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
            huge = self.module.web_fetch("https://example.com/test")
            self.assertEqual(huge["status"], "document_too_large")
            self.assertNotIn("markdown", huge)
            missing = self.module.web_fetch("https://example.com/test", mode="fit")
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

    def test_unpaired_unicode_metadata_serializes_for_initial_and_later_pages(self):
        import json
        from collections import OrderedDict

        # JSON escaping carries lone UTF-16 surrogates through Crawl4AI JSON;
        # their presence in an MCP response can break UTF-8 serialization.
        page = {"url": "https://example.com/long", "success": True,
                "markdown": "safe content " * 3000,
                "metadata": {"title": "Bad\\ud800title",
                             "description": "Bad\\udfffdescription",
                             "canonical_url": "https://example.com/path\\ud800"}}
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            return httpx.Response(200, json={"results": [page]})

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
             "title": "Fallback\\ud800title"},
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
