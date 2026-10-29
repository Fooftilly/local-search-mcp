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

    def test_compact_first_page_reconstructs_short_and_long_unicode_documents(self):
        for size in (2048, 4096, pages.PAGE_CHARS):
            for extra in (1, 2500, pages.PAGE_CHARS, pages.PAGE_CHARS + 11):
                with self.subTest(first=size, extra=extra):
                    content = "🧭" * 12 + "a" * (size + extra - 24)
                    token, count = pages.save(
                        "https://example.com/p", content, {"title": "P"},
                        first_page_chars=size)
                    self.assertEqual(count, pages._page_count(len(content), size))
                    pieces = [content[:size]]
                    for page in range(2, count + 1):
                        value = pages.read(token, page)
                        self.assertEqual(value["page"], page)
                        self.assertEqual(value["page_count"], count)
                        self.assertEqual(value["next_page"],
                                         page + 1 if page < count else None)
                        pieces.append(value["markdown"])
                    self.assertEqual("".join(pieces), content)
                    self.assertLessEqual(max(map(len, pieces[1:])), pages.PAGE_CHARS)
                    self.assertEqual(pages.page_for_offset(size - 1, size), 1)
                    self.assertEqual(pages.page_for_offset(size, size), 2)
                    self.assertEqual(pages.page_for_offset(size + pages.PAGE_CHARS, size), 3)

    def test_compact_find_and_outline_pages_match_saved_boundaries(self):
        size = 4096
        content = ("# Intro\n" + "x" * (size - len("# Intro\n")) +
                   "\n## Second\nNeedle\n" + "z" * pages.PAGE_CHARS)
        token, count = pages.save("https://example.com/a", content, {},
                                  first_page_chars=size)
        self.assertEqual(count, 3)
        matches, _, _ = pages.find_with_security_context(token, "Needle")
        position = content.index("Needle")
        self.assertEqual(matches["matches"][0]["offset"], position)
        self.assertEqual(matches["matches"][0]["page"], 2)
        self.assertEqual(matches["page_count"], 3)
        outline = pages.build_outline(content, first_page_chars=size)
        self.assertEqual(outline["outline"][0]["page"], 1)
        self.assertEqual(outline["outline"][1]["page"], 2)
        self.assertEqual(outline["outline"][1]["offset"], content.index("## Second"))
        self.assertEqual(content[:size] + pages.read(token, 2)["markdown"] +
                         pages.read(token, 3)["markdown"], content)

    def test_compact_handles_respect_ttl_eviction_and_reject_invalid_sizes(self):
        content = "P" * 5000
        for invalid in (None, True, False, 2047, 24001, "4096", 1.5):
            with self.subTest(size=repr(invalid)), self.assertRaises(ValueError):
                pages.save("https://example.com/a", content, {},
                           first_page_chars=invalid)
        with self.assertRaises(ValueError):
            pages.save("https://example.com/a", "A" * 2048, {},
                       first_page_chars=2048)
        with patch.object(pages.time, "monotonic", return_value=30):
            token, count = pages.save("https://example.com/a", content, {},
                                      first_page_chars=2048)
        self.assertEqual(count, 2)
        with patch.object(pages.time, "monotonic", return_value=30 + pages.TTL_SECONDS):
            expired = pages.read(token, 2)
        self.assertEqual(expired["status"], "expired_or_unknown")
        self.assertNotIn("P" * 32, str(expired))
        with patch.object(pages, "MAX_DOCUMENTS", 1):
            first, _ = pages.save("https://example.com/first", content, {},
                                  first_page_chars=2048)
            second, _ = pages.save("https://example.com/second", content, {},
                                   first_page_chars=2048)
            self.assertEqual(pages.read(first, 2)["status"], "expired_or_unknown")
            self.assertEqual(pages.read(second, 2)["markdown"], content[2048:])

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

    def test_outline_multipage_headings_unicode_setext_and_code_fences(self):
        prefix = "# Intro 🧭\n" + "x" * (pages.PAGE_CHARS - len("# Intro 🧭\n"))
        markdown = (prefix + "\n## Second section\n" +
                    "This is a section\n---------------\n" +
                    "```python\n# fake heading\n```\n" +
                    "~~~\n# also fake\n~~~\n" +
                    "## Final\n")
        outline = pages.build_outline(markdown)
        titles = [x["heading"] for x in outline["outline"]]
        self.assertEqual(titles, ["Intro 🧭", "Second section",
                                  "This is a section", "Final"])
        self.assertFalse(outline["outline_truncated"])
        self.assertEqual(outline["outline"][0]["offset"], 0)
        self.assertEqual(outline["outline"][1]["offset"], pages.PAGE_CHARS + 1)
        self.assertEqual(outline["outline"][1]["page"], 2)
        for row in outline["outline"]:
            self.assertIn(row["heading"], markdown[row["offset"]:row["offset"] + 200])

    def test_long_setext_title_keeps_bounded_prefix_and_original_page(self):
        title = "A" * 481
        page_prefix = "before\n" + "x" * (pages.PAGE_CHARS - 250 - len("before\n")) + "\n"
        markdown = page_prefix + title + "\n---\n"
        outline = pages.build_outline(markdown)
        self.assertEqual(len(outline["outline"]), 1)
        heading = outline["outline"][0]
        self.assertEqual(heading["heading"], "A" * pages.MAX_OUTLINE_HEADING_CHARS)
        self.assertEqual(heading["level"], 2)
        self.assertEqual(heading["offset"], len(page_prefix))
        self.assertEqual(heading["page"], 1)
        self.assertGreater(len(page_prefix) + len(title), pages.PAGE_CHARS)
        self.assertFalse(outline["outline_truncated"])

        next_page = "x" * pages.PAGE_CHARS + "\n" + title + "\n===\n"
        outline_next = pages.build_outline(next_page)["outline"]
        self.assertEqual(len(outline_next), 1)
        self.assertEqual(outline_next[0]["heading"], "A" * 160)
        self.assertEqual(outline_next[0]["page"], 2)
        self.assertEqual(outline_next[0]["offset"], pages.PAGE_CHARS + 1)
        self.assertEqual(outline_next[0]["level"], 1)

    def test_indented_code_never_creates_setext_heading(self):
        for code_line in (
            "    print('hello')",
            "\tprint('hello')",
            " \tprint('hello')",
            "   \tprint('hello')",
        ):
            with self.subTest(code=repr(code_line)):
                result = pages.build_outline(code_line + "\n---\n")
                self.assertEqual(result["outline"], [])
        real = pages.build_outline("   An actual section\n---\n")
        self.assertEqual([h["heading"] for h in real["outline"]],
                         ["An actual section"])
        self.assertEqual(real["outline"][0]["level"], 2)

    def test_outline_bounded_and_safe_on_untrusted_headings(self):
        lines = "\n".join(f"### Section {i}" for i in range(60))
        result = pages.build_outline(lines)
        self.assertEqual(len(result["outline"]), pages.MAX_OUTLINE_HEADINGS)
        self.assertTrue(result["outline_truncated"])
        self.assertEqual(result["outline"][-1]["heading"], "Section 39")
        unsafe = "# A" + chr(0x202e) + "B <script>evil</script> " + "x" * 600
        safe = pages.build_outline(unsafe)["outline"][0]["heading"]
        self.assertNotIn(chr(0x202e), safe)
        self.assertLessEqual(len(safe), pages.MAX_OUTLINE_HEADING_CHARS)
        self.assertEqual(pages.build_outline("No headings")["outline"], [])
        self.assertFalse(pages.build_outline("")["outline_truncated"])

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
