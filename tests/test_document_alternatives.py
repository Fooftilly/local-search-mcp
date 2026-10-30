"""Source-advertised format discovery never guesses or retrieves linked documents."""
import unittest
from unittest.mock import patch

import structured_extraction as subject

BASE = "https://www.rfc-editor.org/info/rfc9110"
GROUP = """View as:
  * [Plain Text](https://www.rfc-editor.org/rfc/rfc9110.txt),
  * [Compact HTML](https://www.rfc-editor.org/rfc/rfc9110.html),
  * [PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)

Info
# RFC 9110: HTTP Semantics
"""


def extract(markdown, *, enabled=True, links=False, tables=False):
    with patch("domain_policy.socket.getaddrinfo",
               side_effect=AssertionError("Never resolve or fetch discovered links")):
        return subject.extract(
            {"links": {"internal": []}, "tables": []}, BASE,
            include_links=links, include_tables=tables,
            include_alternatives=enabled, markdown=markdown,
        )


class AdvertisedAlternativesTests(unittest.TestCase):
    def test_real_rfc_editor_markdown_group(self):
        found = extract(GROUP)
        self.assertEqual(found["alternatives"], [
            {"format": "txt", "url": "https://www.rfc-editor.org/rfc/rfc9110.txt",
             "evidence": "source_labeled_view_as"},
            {"format": "html", "url": "https://www.rfc-editor.org/rfc/rfc9110.html",
             "evidence": "source_labeled_view_as"},
            {"format": "pdf", "url": "https://www.rfc-editor.org/rfc/rfc9110.pdf",
             "evidence": "source_labeled_view_as"},
        ])
        self.assertFalse(found["structured_info"]["alternatives"]["format_verified"])
        self.assertEqual(found["structured_info"]["source_completeness"], "unknown")

    def test_default_output_is_unchanged(self):
        self.assertEqual(extract(GROUP, enabled=False), {})
        self.assertNotIn("alternatives", subject.extract(
            {"links": {"internal": []}}, BASE,
            include_links=True, include_tables=False))
        self.assertEqual(extract("A normal page")["alternatives"], [])

    def test_unrelated_navigation_and_ambiguous_words_are_not_alternatives(self):
        text = """# Alternative Implementations
[PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)
[HTML](https://www.rfc-editor.org/rfc/rfc9110.html)
"""
        self.assertEqual(extract(text)["alternatives"], [])
        self.assertEqual(extract(
            "View as:\n[Alternative Implementations](https://www.python.org/download/alternatives)"
        )["alternatives"], [])

    def test_group_needs_same_document_stem_and_pdf_counterpart(self):
        mismatched = """View as:
[HTML](https://www.rfc-editor.org/rfc/rfc9111.html)
[PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)

"""
        self.assertEqual(extract(mismatched)["alternatives"], [])
        self.assertEqual(extract(
            "View as:\n[HTML](https://www.rfc-editor.org/rfc/rfc9110.html)\n"
        )["alternatives"], [])

    def test_separate_groups_are_not_conflated(self):
        markdown = """View as:
[HTML](https://www.rfc-editor.org/rfc/rfc9110.html)

Elsewhere

View as:
[PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)

"""
        self.assertEqual(extract(markdown)["alternatives"], [])

    def test_code_fences_are_examples_not_offered_links(self):
        fenced = "~~~markdown\n" + GROUP + "~~~\n"
        self.assertEqual(extract(fenced)["alternatives"], [])
        backticks = chr(96) * 3
        fenced = backticks + "markdown\n" + GROUP + backticks + "\n"
        self.assertEqual(extract(fenced)["alternatives"], [])
        # Real links after a quoted sample are still eligible.
        self.assertEqual(len(extract(fenced + "\n" + GROUP)["alternatives"]), 3)

    def test_multiple_document_stems_fail_closed(self):
        group = """View as:
[HTML](https://www.rfc-editor.org/rfc/rfc9110.html)
[PDF](https://www.rfc-editor.org/rfc/rfc9111.pdf)
[HTML](https://www.rfc-editor.org/rfc/rfc9111.html)
[PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)

"""
        self.assertEqual(extract(group)["alternatives"], [])

    def test_unsafe_urls_are_filtered_without_dns_or_approval(self):
        candidates = [
            "javascript:alert(1)", "file:///etc/passwd", "data:text/html,evil",
            "http://127.0.0.1/rfc/rfc9110.html",
            "https://user:pass@www.rfc-editor.org/rfc/rfc9110.html",
            "https://www.rfc-editor.org:444/rfc/rfc9110.html",
            "https://www.rfc-editor.org/rfc/rfc9110.html%0a",
            "https://www.rfc-editor.org/rfc/rfc9110.html\nbad",
            "https://faß.de/rfc/rfc9110.html",
            "https://www.rfc-editor.org/rfc/rfc9110.html?credentials=1",
        ]
        # The query-string case is a valid URL: a query does not imply
        # permission, and the evidence remains an unverified link.
        for url in candidates:
            with self.subTest(url=url):
                group = ("View as:\n[Compact HTML](" + url + ")\n"
                         "[PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)\n\n")
                got = extract(group)["alternatives"]
                if "credentials=1" in url:
                    self.assertEqual(len(got), 2)
                else:
                    self.assertEqual(got, [])

    def test_different_public_host_remains_unapproved_discovery_only(self):
        group = """View as:
[Compact HTML](https://publisher.example/rfc/rfc9110.html)
[PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)

"""
        found = extract(group)["alternatives"]
        self.assertEqual(found[0]["url"], "https://publisher.example/rfc/rfc9110.html")
        self.assertEqual(len(found), 2)

    def test_budget_and_count_bounds(self):
        group = GROUP + "x" * 10000
        found = extract(group, links=True, tables=True)
        self.assertLessEqual(len(found["alternatives"]), subject.MAX_ALTERNATIVES)
        self.assertLessEqual(subject._size(found), subject.MAX_OUTPUT_BYTES)
        self.assertEqual(subject._advertised_alternatives(GROUP, BASE, budget=1), [])

    def test_not_extrapolated_from_missing_or_false_markup(self):
        text = """View as:
[PDF](https://www.rfc-editor.org/rfc/rfc9110.pdf)
[Compact HTML](https://www.rfc-editor.org/rfc/rfc9110.htm)
[Plain Text](https://www.rfc-editor.org/rfc/rfc9110.other)

"""
        self.assertEqual(extract(text)["alternatives"], [])


if __name__ == "__main__":
    unittest.main()
