"""Bounded JSON-LD extraction: corpus, HTML parser, and real FastMCP contract."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
import httpx

import bridge
import domain_policy
import jsonld_extraction as ld


def html_script(payload, *, extra=""):
    return ('<html><head><script type="application/ld+json">' +
            json.dumps(payload, ensure_ascii=False) +
            '</script>' + extra + '</head><body>Visible article</body></html>')


class JSONLDUnitTests(unittest.TestCase):
    def test_article_product_offer_and_faq_exact_fields(self):
        document = {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "Article", "headline": "Public funding programme",
                 "datePublished": "2026-10-30", "author": {"name": "Public agency"},
                 "url": "https://private.example/secret",
                 "description": "Eligible applicants"},
                {"@type": "Product", "name": "Laptop", "sku": "A-1",
                 "brand": {"name": "Brand Name"},
                 "offers": {"@type": "Offer", "price": "499.90",
                            "priceCurrency": "EUR", "availability": "https://schema.org/InStock",
                            "url": "javascript:alert(1)"}},
                {"@type": "FAQPage", "mainEntity": [{
                    "@type": "Question", "name": "What is required?",
                    "acceptedAnswer": {"@type": "Answer",
                                       "text": "An eligible public applicant."}
                }]},
            ],
        }
        result = ld.extract({"html": html_script(document)})
        self.assertEqual(result["jsonld_info"]["status"], "available")
        self.assertEqual([r["type"] for r in result["jsonld"]],
                         ["Article", "Product", "FAQPage"])
        self.assertEqual(result["jsonld"][0]["fields"]["author"], "Public agency")
        self.assertEqual(result["jsonld"][1]["fields"]["offers"],
                         {"price": "499.90", "priceCurrency": "EUR",
                          "availability": "InStock"})
        self.assertEqual(result["jsonld"][2]["fields"]["questions"][0]["answer"],
                         "An eligible public applicant.")
        self.assertNotIn("javascript:", json.dumps(result))
        self.assertNotIn("private.example", json.dumps(result))
        self.assertTrue(all(not r["publisher_claims_verified"] for r in result["jsonld"]))

    def test_untrusted_wrong_context_types_and_scripts_never_execute_or_leak(self):
        payloads = [
            {"@context": "https://not-schema.example", "@type": "Article",
             "headline": "UNTRUSTED_FALSE_SCHEMA"},
            {"@context": "https://schema.org", "@type": "WebPage",
             "description": "NO_UNBOUNDED_DATA"},
            {"@context": "https://schema.org", "@type": "Article",
             "headline": '<img src=x onerror=alert("SECRET_XSS")>',
             "description": "Safe plain text"},
            {"@context": "https://schema.org", "@type": "Article",
             "headline": "News", "url": "https://private.example/path",
             "extra": {"password": "SECRET_PASSWORD"}},
        ]
        scripts = "".join("<script type='application/ld+json'>" +
                          json.dumps(x) + "</script>" for x in payloads)
        page = ld.extract({"html": "<html>" + scripts + "</html>"})
        titles = [x["fields"].get("headline") for x in page["jsonld"]]
        self.assertNotIn("UNTRUSTED_FALSE_SCHEMA", titles)
        self.assertEqual(len(page["jsonld"]), 2)
        all_text = json.dumps(page)
        for sentinel in ("SECRET_XSS", "SECRET_PASSWORD", "private.example",
                         "NO_UNBOUNDED_DATA", "onerror"):
            self.assertNotIn(sentinel, all_text)

    def test_missing_html_malformed_json_and_bounded_scans(self):
        absent = ld.extract({"cleaned_html": "<script>SECRET</script>"})
        self.assertEqual(absent["jsonld_info"]["status"], "unavailable")
        self.assertNotIn("SECRET", str(absent))
        self.assertEqual(ld.extract({"html": ""})["jsonld_info"]["status"], "unavailable")
        self.assertEqual(ld.extract({"html": "   "})["jsonld_info"]["status"], "unavailable")
        invalid = ld.extract({"html": '<script type="application/ld+json">{broken</script>'})
        self.assertEqual(invalid["jsonld_info"]["status"], "none_found")
        self.assertEqual(invalid["jsonld_info"]["invalid_scripts"], 1)
        huge = ld.extract({"html": "<p>" + "a" * (ld.MAX_HTML_SCAN + 16) +
                           html_script({"@context": "https://schema.org",
                                        "@type": "Product", "name": "Beyond cap"})})
        self.assertEqual(huge["jsonld"], [])
        self.assertTrue(huge["jsonld_info"]["source_html_scan_truncated"])
        oversized_script = ld.extract({"html":
            '<script type="application/ld+json">' +
            "x" * (ld.MAX_SCRIPT_CHARS + 1) + "</script>"})
        self.assertEqual(oversized_script["jsonld"], [])
        self.assertTrue(oversized_script["jsonld_info"]["limits_applied"])

    def test_top_level_array_cap_reports_missing_later_records(self):
        items = [{"@context": "https://schema.org", "@type": "Unrelated",
                  "name": "Ignored"} for _ in range(ld.MAX_JSON_NODES)]
        items.append({"@context": "https://schema.org", "@type": "Article",
                      "headline": "Beyond the input cap"})
        result = ld.extract({"html": html_script(items)})
        self.assertEqual(result["jsonld"], [])
        self.assertTrue(result["jsonld_info"]["limits_applied"])
        self.assertEqual(result["jsonld_info"]["status"], "none_found")

    def test_mixed_schema_types_respect_context_and_graph_override(self):
        page = {"@context": "https://schema.org", "@graph": [
            {"@context": "https://not-schema.example",
             "@type": ["Article", "https://schema.org/Event"],
             "headline": "Wrong headline", "name": "Public meeting"},
            {"@context": "https://other.example",
             "@type": "Article", "headline": "Do not accept"},
            {"@type": "Article", "headline": "Inherited schema article"},
        ]}
        result = ld.extract({"html": html_script(page)})
        self.assertEqual([item["type"] for item in result["jsonld"]],
                         ["Event", "Article"])
        self.assertEqual(result["jsonld"][0]["fields"]["name"], "Public meeting")
        self.assertEqual(result["jsonld"][1]["fields"]["headline"],
                         "Inherited schema article")

    def test_unsafe_embedded_https_urls_never_leave_scalar_fields(self):
        payload = {"@context": "https://schema.org", "@type": "Article",
                   "headline": "Official notice",
                   "description": "Contact https://example.com/key?token=SECRET_ACCESS",
                   "author": {"name": "www.example.com?private=SECRET_AUTHOR"}}
        result = ld.extract({"html": html_script(payload)})
        self.assertEqual(result["jsonld"][0]["fields"], {"headline": "Official notice"})
        self.assertNotIn("SECRET_", json.dumps(result))
        product = {"@context": "https://schema.org", "@type": "Product",
                   "name": "Safe device", "offers": {
                       "price": "https://example.com?token=SECRET_PRICE", "priceCurrency": "EUR"}}
        product_result = ld.extract({"html": html_script(product)})
        self.assertEqual(product_result["jsonld"][0]["fields"]["offers"],
                         {"priceCurrency": "EUR"})

    def test_deep_nested_untrusted_fields_never_abort_valid_fetch(self):
        deep_author = "Do not extract"
        deep_context = "https://schema.org"
        for _ in range(550):
            deep_author = [deep_author]
            deep_context = [deep_context]
        valid_context_page = {
            "@context": "https://schema.org", "@type": "Article",
            "headline": "Valid headline", "author": deep_author,
        }
        result = ld.extract({"html": html_script(valid_context_page)})
        self.assertEqual(result["jsonld"][0]["fields"],
                         {"headline": "Valid headline"})
        nested_context_page = {
            "@context": deep_context, "@type": "Article",
            "headline": "Should not trust deeply nested context",
        }
        result = ld.extract({"html": html_script(nested_context_page)})
        self.assertEqual(result["jsonld"], [])
        self.assertEqual(result["jsonld_info"]["status"], "none_found")
        self.assertTrue(result["jsonld_info"]["limits_applied"])

    def test_many_graph_nodes_and_scripts_return_only_bounded_records(self):
        node = {"@context": "https://schema.org", "@type": "Event",
                "name": "Public event", "startDate": "2026-11-01"}
        data = {"@context": "https://schema.org",
                "@graph": [{k: v for k, v in node.items() if k != "@context"}
                           for _ in range(100)]}
        html = "".join(html_script(data) for _ in range(18))
        answer = ld.extract({"html": html})
        self.assertLessEqual(len(answer["jsonld"]), ld.MAX_RECORDS)
        self.assertLessEqual(answer["jsonld_info"]["candidate_scripts_observed"], ld.MAX_SCRIPTS + 1)
        self.assertTrue(answer["jsonld_info"]["limits_applied"])
        self.assertLessEqual(len(json.dumps(answer["jsonld"]).encode()), ld.MAX_OUTPUT_BYTES)


class JSONLDMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environment = patch.dict(os.environ, {
            "XDG_STATE_HOME": tmp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(tmp.name) / "history.jsonl"),
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.temp_root = Path(tmp.name)
        bridge.request_cache.reset_for_test()
        self.addCleanup(bridge.request_cache.reset_for_test)

    async def test_opt_in_uses_existing_guarded_crawl_and_default_shape_unchanged(self):
        url = "https://example.com/article"
        payload = {"@context": "https://schema.org", "@type": "Article",
                   "headline": "Official notice", "datePublished": "2026-10-31"}
        html = html_script(payload, extra="<script>SECRET_RAW_JS</script>")
        upstream = []
        original = httpx.Client

        def handler(request):
            upstream.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            self.assertEqual(request.url.path, "/crawl")
            self.assertEqual(json.loads(request.content)["urls"], [url])
            return httpx.Response(200, json={"results": [{
                "url": url, "success": True, "status_code": 200,
                "markdown": {"raw_markdown": "# Official notice"},
                "html": html, "cleaned_html": "<p>Visible article</p>",
            }]})

        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)

        domain_policy.decide("example.com", "allow", scope="exact")
        deny_now = {"value": False}
        def mock_policy(current_url):
            return ({"url": current_url, "success": False, "status": "denied",
                     "reason": "Operator denied the hostname"}
                    if deny_now["value"] else None)
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge, "_check_public_target", side_effect=mock_policy),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext())):
            async with Client(bridge.mcp) as client:
                tools = {x.name: x for x in await client.list_tools()}
                self.assertIn("include_jsonld", tools["web_fetch"].inputSchema["properties"])
                before = await client.call_tool("web_fetch", {"url": url})
                self.assertFalse(before.is_error)
                self.assertTrue(before.data["success"])
                self.assertNotIn("jsonld", before.data)
                self.assertNotIn("jsonld_info", before.data)
                enhanced = await client.call_tool("web_fetch", {"url": url, "include_jsonld": True})
                self.assertFalse(enhanced.is_error)
                self.assertEqual(enhanced.data["jsonld"][0]["fields"]["headline"],
                                 "Official notice")
                self.assertEqual(enhanced.data["jsonld_info"]["source"], "crawlresult.html")
                self.assertNotIn("SECRET_RAW_JS", str(enhanced.data))
                self.assertNotIn("cleaned_html", str(enhanced.data))
                self.assertEqual(upstream, ["/health", "/crawl", "/health", "/crawl"])
                domain_policy.decide("example.com", "deny", scope="exact")
                deny_now["value"] = True
                denied = await client.call_tool("web_fetch", {"url": url, "include_jsonld": True})
                self.assertFalse(denied.data["success"])
                self.assertNotIn("jsonld", denied.data)
                self.assertEqual(upstream, ["/health", "/crawl", "/health", "/crawl"])
        audit = (self.temp_root / "history.jsonl").read_text()
        self.assertNotIn("Official notice", audit)
        self.assertNotIn("SECRET_RAW_JS", audit)

    async def test_missing_html_explicit_and_raw_markdown_still_available(self):
        url = "https://example.com/item"
        original = httpx.Client
        def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
            self.assertEqual(request.url.path, "/crawl")
            return httpx.Response(200, json={"results": [{
                "url": url, "success": True, "markdown": "Still valid Markdown",
                "cleaned_html": "<p>Markdown is accessible but raw HTML is not</p>",
            }]})
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        domain_policy.decide("example.com", "allow", scope="exact")
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge, "_check_public_target", return_value=None),
              patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext())):
            async with Client(bridge.mcp) as client:
                response = await client.call_tool("web_fetch", {
                    "url": url, "include_jsonld": True})
        self.assertFalse(response.is_error)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["markdown"], "Still valid Markdown")
        self.assertEqual(response.data["jsonld"], [])
        self.assertEqual(response.data["jsonld_info"]["status"], "unavailable")
        self.assertNotIn("cleaned_html", str(response.data))


if __name__ == "__main__":
    unittest.main()
