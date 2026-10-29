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

    def test_literal_find_crosses_page_boundary_with_correct_source_offset(self):
        content = "x" * (pages.PAGE_CHARS - 3) + "Grant Deadline" + "z" * pages.PAGE_CHARS
        handle, _ = pages.save("https://example.com/long", content, {"title": "Long"})
        result, source, proof = pages.find_with_security_context(handle, "GRANT deadline")
        self.assertTrue(result["success"])
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["page"], 1)
        self.assertEqual(result["matches"][0]["offset"], pages.PAGE_CHARS - 3)
        self.assertIn("Grant Deadline", result["matches"][0]["excerpt"])
        self.assertEqual(result["page_count"], 3)
        self.assertFalse(result["has_more"])
        self.assertEqual(source, "https://example.com/long")
        self.assertIsNone(proof)
        # A match beginning on the second page is assigned to that page.
        second, _, _ = pages.find_with_security_context(handle, "zzzz")
        self.assertEqual(second["matches"][0]["page"], 2)

    def test_finds_literal_regex_chars_unicode_and_overlapping_matches(self):
        text = ("START " + "aAaA" + " literal .*[] " + "ŽIVOT " +
                "q" * (pages.PAGE_CHARS + 1))
        handle, _ = pages.save("https://example.com/doc", text, {})
        repeated, _, _ = pages.find_with_security_context(handle, "AA", 4)
        self.assertEqual([m["offset"] for m in repeated["matches"]], [6, 7, 8])
        self.assertFalse(repeated["has_more"])
        literal, _, _ = pages.find_with_security_context(handle, ".*[]")
        self.assertEqual(literal["match_count"], 1)
        non_ascii, _, _ = pages.find_with_security_context(handle, "život")
        self.assertEqual(non_ascii["match_count"], 1)

    def test_finds_are_bounded_with_many_matches_and_no_result(self):
        content = "a" * (pages.PAGE_CHARS + 600)
        handle, _ = pages.save("https://example.com/large", content, {})
        result, _, _ = pages.find_with_security_context(handle, "a", 20)
        self.assertEqual(result["match_count"], 20)
        self.assertTrue(result["has_more"])
        self.assertEqual(len(result["matches"]), 20)
        self.assertTrue(all(len(row["excerpt"]) <= 221 for row in result["matches"]))
        nohit, _, _ = pages.find_with_security_context(handle, "absent")
        self.assertEqual(nohit["matches"], [])
        self.assertFalse(nohit["has_more"])

    def test_find_pagination_reconstructs_every_overlapping_hit(self):
        # The first hit begins immediately before a 24K page boundary; Unicode
        # before the hits proves offsets count characters rather than UTF-8 bytes.
        beginning = "🧭" + "x" * (pages.PAGE_CHARS - 2)
        content = beginning + "a" * 67 + "z" * pages.PAGE_CHARS
        handle, _ = pages.save("https://example.com/long", content, {})
        expected = list(range(len(beginning), len(beginning) + 66))
        actual = []
        offset = 0
        while True:
            result, source, _ = pages.find_with_security_context(
                handle, "AA", max_matches=7, start_offset=offset)
            self.assertTrue(result["success"])
            self.assertLessEqual(result["match_count"], 7)
            self.assertEqual(source, "https://example.com/long")
            self.assertTrue(all(len(m["excerpt"]) <= 222 for m in result["matches"]))
            actual.extend(m["offset"] for m in result["matches"])
            if not result["has_more"]:
                self.assertIsNone(result["next_offset"])
                break
            self.assertIsInstance(result["next_offset"], int)
            self.assertGreater(result["next_offset"], offset)
            self.assertEqual(result["next_offset"],
                             result["matches"][-1]["offset"] + 1)
            offset = result["next_offset"]
        self.assertEqual(actual, expected)
        self.assertEqual(actual[0], pages.PAGE_CHARS - 1)
        page_crossing, _, _ = pages.find_with_security_context(
            handle, "aa", max_matches=1, start_offset=pages.PAGE_CHARS - 1)
        self.assertEqual(page_crossing["matches"][0]["page"], 1)
        self.assertEqual(page_crossing["next_offset"], pages.PAGE_CHARS)

    def test_find_pagination_exact_end_empty_and_unicode_offsets(self):
        content = "🧭Až ŽiVOt aAa" + "q" * (pages.PAGE_CHARS + 1)
        handle, _ = pages.save("https://example.com/doc", content, {})
        first, _, _ = pages.find_with_security_context(
            handle, "aA", max_matches=1)
        self.assertTrue(first["has_more"])
        nxt, _, _ = pages.find_with_security_context(
            handle, "AA", max_matches=1,
            start_offset=first["next_offset"])
        self.assertFalse(nxt["has_more"])
        self.assertIsNone(nxt["next_offset"])
        self.assertEqual([first["matches"][0]["offset"],
                          nxt["matches"][0]["offset"]], [10, 11])
        unicode, _, _ = pages.find_with_security_context(handle, "život")
        self.assertEqual(unicode["matches"][0]["offset"], 4)
        nohit, _, _ = pages.find_with_security_context(
            handle, "aA", start_offset=len(content))
        self.assertEqual(nohit["match_count"], 0)
        self.assertFalse(nohit["has_more"])
        self.assertIsNone(nohit["next_offset"])
        with self.assertRaises(ValueError):
            pages.find_with_security_context(
                handle, "aA", start_offset=len(content) + 1)

    def test_find_pagination_rejects_invalid_offsets_without_disclosure(self):
        content = "SECRET" + "x" * pages.PAGE_CHARS
        with patch.object(pages.time, "monotonic", return_value=50):
            handle, _ = pages.save("https://example.com/page", content, {})
        for invalid in (-1, True, 1.0, "0", None,
                        pages.MAX_DOCUMENT_CHARS + 1):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                pages.find_with_security_context(
                    handle, "SECRET", start_offset=invalid)
        with patch.object(pages.time, "monotonic",
                          return_value=50 + pages.TTL_SECONDS):
            expired, src, proof = pages.find_with_security_context(
                handle, "SECRET", start_offset=1)
        self.assertEqual(expired["status"], "expired_or_unknown")
        self.assertNotIn("SECRET", str(expired))
        self.assertIsNone(src)
        self.assertIsNone(proof)

    def test_find_expiry_eviction_and_invalid_parameters_are_safe(self):
        content = "SECRET PASSAGE " + "x" * pages.PAGE_CHARS
        with patch.object(pages.time, "monotonic", return_value=200):
            handle, _ = pages.save("https://example.com/long", content, {})
        for bad in ("", " " * 5, "a" * 161, "\n", "\x00", "\ud800",
                    "\u200b", "\u2028", "\u202e", 12, None):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                pages.find_with_security_context(handle, bad)
        for bad in (0, -1, 21, 1.0, True, "10"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pages.find_with_security_context(handle, "secret", bad)
        for bad in ("../../etc/passwd", "!" * 32, None):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError):
                pages.find_with_security_context(bad, "secret")
        with patch.object(pages.time, "monotonic",
                          return_value=200 + pages.TTL_SECONDS):
            expired, source, proof = pages.find_with_security_context(handle, "secret")
        self.assertEqual(expired["status"], "expired_or_unknown")
        self.assertNotIn("SECRET PASSAGE", str(expired))
        self.assertIsNone(source)
        self.assertIsNone(proof)
        # Eviction also invalidates the search capability.
        other, _ = pages.save("https://example.com/other",
                              "b" * (pages.PAGE_CHARS + 1), {})
        with patch.object(pages, "MAX_DOCUMENTS", 1):
            pages.save("https://example.com/new", "c" * (pages.PAGE_CHARS + 1), {})
        missing, _, _ = pages.find_with_security_context(other, "b")
        self.assertEqual(missing["status"], "expired_or_unknown")

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
