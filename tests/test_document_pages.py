"""Long-document continuation cache regressions (stdlib only)."""
from collections import OrderedDict
import unittest
from unittest.mock import patch

import document_pages as pages


class DocumentPageTests(unittest.TestCase):
    def setUp(self):
        self.cache_patch = patch.object(pages, "_CACHE", OrderedDict())
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def test_exact_boundaries_reconstruct_document_without_lost_text(self):
        content = "a" * pages.PAGE_CHARS + "b" * pages.PAGE_CHARS + "tail"
        handle, count = pages.save("https://example.com/long", content, {"title": "Long"})
        self.assertEqual(count, 3)
        second = pages.read(handle, 2)
        third = pages.read(handle, 3)
        self.assertEqual(second["markdown"], "b" * pages.PAGE_CHARS)
        self.assertEqual(second["next_page"], 3)
        self.assertTrue(second["truncated"])
        self.assertEqual(third["markdown"], "tail")
        self.assertIsNone(third["next_page"])
        self.assertFalse(third["truncated"])
        self.assertEqual(third["metadata"]["title"], "Long")
        self.assertEqual("a" * pages.PAGE_CHARS + second["markdown"] + third["markdown"],
                         content)

    def test_unknown_expired_and_invalid_handles_do_not_disclose_content(self):
        content = "SENSITIVE" * 4000
        with patch.object(pages.time, "monotonic", return_value=100):
            handle, _ = pages.save("https://example.com/a", content, {})
        for bad in ("../../etc/passwd", "", "!" * 32, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pages.read(bad, 2)
        with patch.object(pages.time, "monotonic", return_value=101):
            with self.assertRaises(ValueError):
                pages.read(handle, 1)
            with self.assertRaises(ValueError):
                pages.read(handle, True)
            with self.assertRaises(ValueError):
                pages.read(handle, 100)
            self.assertEqual(pages.read(handle, 2)["markdown"], content[pages.PAGE_CHARS:])
        with patch.object(pages.time, "monotonic", return_value=100 + pages.TTL_SECONDS):
            expired = pages.read(handle, 2)
        self.assertEqual(expired["status"], "expired_or_unknown")
        self.assertNotIn("SENSITIVE", str(expired))
        self.assertEqual(pages._CACHE, OrderedDict())

    def test_oldest_documents_evicted_at_capacity(self):
        content = "x" * (pages.PAGE_CHARS + 1)
        tokens = []
        with patch.object(pages, "MAX_DOCUMENTS", 2):
            for i in range(3):
                token, _ = pages.save(f"https://example.com/{i}", content, {})
                tokens.append(token)
        self.assertEqual(len(pages._CACHE), 2)
        self.assertEqual(pages.read(tokens[0], 2)["status"], "expired_or_unknown")
        self.assertEqual(pages.read(tokens[1], 2)["markdown"], "x")
        self.assertEqual(pages.read(tokens[2], 2)["markdown"], "x")

    def test_case_insensitive_http_scheme_preserves_page_content(self):
        document = "A" * (pages.PAGE_CHARS + 5)
        handle, count = pages.save("HTTPS://example.com/article", document, {})
        self.assertEqual(count, 2)
        followup = pages.read(handle, 2)
        self.assertEqual(followup["url"], "HTTPS://example.com/article")
        self.assertEqual(followup["markdown"], "A" * 5)

    def test_total_memory_limit_and_document_limit_fail_closed(self):
        with patch.object(pages, "MAX_TOTAL_CHARS", pages.PAGE_CHARS + 20):
            first, _ = pages.save("https://example.com/first", "x" * (pages.PAGE_CHARS + 10), {})
            second, _ = pages.save("https://example.com/second", "y" * (pages.PAGE_CHARS + 10), {})
        self.assertEqual(pages.read(first, 2)["status"], "expired_or_unknown")
        self.assertEqual(pages.read(second, 2)["markdown"], "y" * 10)
        with self.assertRaises(ValueError):
            pages.save("file:///etc/passwd", "x" * (pages.PAGE_CHARS + 1), {})
        with self.assertRaises(ValueError):
            pages.save("https://example.com/a", "x" * (pages.MAX_DOCUMENT_CHARS + 1), {})


if __name__ == "__main__":
    unittest.main()
