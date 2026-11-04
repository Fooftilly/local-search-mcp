"""Opt-in bounded PDF text extraction through the existing guarded fetch path.

All HTTP is mocked; tests never download a PDF or authorize a domain.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import unittest
from unittest.mock import patch

import httpx

import bridge
import document_pages
import history_log
import request_cache


class PdfTextOptInTests(unittest.TestCase):
    def setUp(self):
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        self.payloads = []
        self.pages = {}
        self.original_client = httpx.Client
        for obj, name, value in (
            (history_log, "append_history", lambda event: None),
            (bridge, "_check_public_target", lambda url: None),
            (bridge, "CRAWLER_SECURITY_WARNING", None),
            (bridge, "expired_grant_without_active_fallback",
             lambda name: False),
            (bridge.on_demand, "lease", lambda name: nullcontext()),
        ):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def handler(self, req):
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})
        self.assertEqual(req.url.path, "/crawl")
        payload = json.loads(req.content)
        self.payloads.append(payload)
        url = payload["urls"][0]
        page = self.pages.get(url, {
            "url": url, "success": True, "status_code": 200,
            "markdown": {"raw_markdown": "# Sample document\n\nPDF content",
                         "fit_markdown": ""},
        })
        return httpx.Response(200, json={"results": [page]})

    def client_factory(self, **kwargs):
        self.assertFalse(kwargs.get("trust_env", True))
        return self.original_client(
            trust_env=False, transport=httpx.MockTransport(self.handler))

    def test_explicit_pdf_request_uses_strict_lower_resource_limits(self):
        url = "https://example.org/approved/file.PDF?version=1"
        with patch.object(bridge.httpx, "Client", side_effect=self.client_factory):
            result = bridge.web_fetch(url, fresh=True, extract_pdf=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["extraction_format"], "pdf_text")
        self.assertIn("PDF content", result["markdown"])
        self.assertEqual(len(self.payloads), 1)
        config = self.payloads[0]["crawler_config"]["params"]
        self.assertEqual(config["stream"], False)
        self.assertEqual(config["scraping_strategy"], {
            "type": "PDFContentScrapingStrategy",
            "params": {
                "extract_images": False, "save_images_locally": False,
                "batch_size": 2, "max_pdf_bytes": 16 * 1024 * 1024,
                "max_pdf_pages": 150,
            },
        })
        self.assertNotIn("image_save_dir", json.dumps(config))
        self.assertEqual(self.payloads[0]["urls"], [url])

    def test_default_pdf_url_does_not_change_original_fetch_behavior(self):
        with patch.object(bridge.httpx, "Client", side_effect=self.client_factory):
            result = bridge.web_fetch(
                "https://example.org/approved/manual.pdf", fresh=True)
        self.assertTrue(result["success"])
        self.assertNotIn("extraction_format", result)
        self.assertNotIn("scraping_strategy",
                         self.payloads[0]["crawler_config"]["params"])

    def test_pdf_validation_precedes_policy_and_crawler_side_effects(self):
        with patch.object(
            bridge, "_check_public_target",
            side_effect=AssertionError("must not check authorization yet")
        ), patch.object(
            bridge, "_run_fetch", side_effect=AssertionError("no crawl")
        ):
            for kwargs in (
                {"extract_pdf": "yes"},
                {"extract_pdf": True, "mode": "fit"},
                {"extract_pdf": True, "include_links": True},
                {"extract_pdf": True, "include_tables": True},
                {"extract_pdf": True, "include_alternatives": True},
                {"extract_pdf": True, "include_jsonld": True},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    bridge.web_fetch("https://example.org/file.pdf", **kwargs)
            for path in (
                "https://example.org/file.txt?format=.pdf",
                "https://example.org/file.pdf.html",
                "https://example.org/file",
            ):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    bridge.web_fetch(path, extract_pdf=True)

    def test_placeholder_and_blank_pdf_do_not_claim_success(self):
        url = "https://example.org/document.pdf"
        for markdown in ("", "Scraper will handle the real work"):
            self.pages[url] = {
                "url": url, "success": True, "status_code": 200,
                "markdown": markdown,
            }
            with self.subTest(markdown=markdown), patch.object(
                bridge.httpx, "Client", side_effect=self.client_factory
            ):
                result = bridge.web_fetch(url, extract_pdf=True, fresh=True)
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "extraction_failed")
            self.assertNotIn("markdown", result)
            self.assertNotIn("Scraper will handle", repr(result))

    def test_pdf_binary_media_and_oversize_text_are_not_returned(self):
        url = "https://example.org/data.pdf"
        cases = [
            ("# Image\n\n![x](data:image/png;base64,SECRET)",
             "invalid_extraction"),
            ("# PDF\n\ndata:application/pdf;base64,SECRET",
             "invalid_extraction"),
            ("x" * (document_pages.MAX_DOCUMENT_CHARS + 1),
             "document_too_large"),
        ]
        for body, expected in cases:
            self.pages[url] = {"url": url, "success": True,
                               "status_code": 200, "markdown": body}
            with self.subTest(expected=expected), patch.object(
                bridge.httpx, "Client", side_effect=self.client_factory
            ):
                result = bridge.web_fetch(url, extract_pdf=True, fresh=True)
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["success"])
            self.assertNotIn("markdown", result)
            self.assertNotIn("SECRET", repr(result))

    def test_unapproved_pdf_redirect_never_releases_extracted_text(self):
        source = "https://example.org/approved.pdf"
        target = "https://unapproved.invalid/download"
        self.pages[source] = {
            "url": source, "redirected_url": target, "success": True,
            "status_code": 302, "redirected_status_code": 200,
            "markdown": "PRIVATE-PDF-TEXT",
        }
        def target_policy(url):
            if url == target:
                return {"url": url, "success": False, "status": "denied"}
            return None
        with (patch.object(bridge, "_check_public_target",
                           side_effect=target_policy),
              patch.object(bridge.httpx, "Client",
                           side_effect=self.client_factory)):
            result = bridge.web_fetch(source, extract_pdf=True, fresh=True)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("PRIVATE-PDF-TEXT", repr(result))

    def test_mixed_batch_applies_pdf_strategy_only_to_pdf_urls(self):
        urls = [
            "https://example.org/page.html",
            "https://example.org/manual.pdf",
        ]
        with (patch.object(bridge.httpx, "Client",
                           side_effect=self.client_factory),
              patch.object(bridge, "_batch_currently_authorized",
                           return_value=True)):
            result = asyncio.run(bridge.web_fetch_batch(
                urls, fresh=True, extract_pdf=True))
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [x["result"]["success"] for x in result["results"]],
            [True, True])
        by_url = {x["urls"][0]: x["crawler_config"]["params"]
                  for x in self.payloads}
        self.assertNotIn("scraping_strategy", by_url[urls[0]])
        self.assertIn("scraping_strategy", by_url[urls[1]])
        self.assertEqual(
            result["results"][1]["result"]["extraction_format"], "pdf_text")

    def test_batch_validation_does_not_submit_any_work(self):
        with patch.object(
            bridge, "_batch_fetch_without_once",
            side_effect=AssertionError("no workers")
        ):
            for flags in (
                {"extract_pdf": "true"},
                {"extract_pdf": True, "mode": "fit"},
                {"extract_pdf": True, "include_tables": True},
            ):
                with self.subTest(flags=flags), self.assertRaises(ValueError):
                    asyncio.run(bridge.web_fetch_batch(
                        ["https://example.org/file.pdf"], **flags))


if __name__ == "__main__":
    unittest.main()
