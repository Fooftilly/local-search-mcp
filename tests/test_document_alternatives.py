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


# Observed publisher page: https://www.w3.org/TR/2003/REC-DOM-Level-2-HTML-20030109/
# It names XML, TXT, PostScript, PDF, single HTML and ZIP side by side.
# Only safe text/HTML/PDF siblings are relevant to this narrowly scoped MCP.
W3C_BASE = "https://www.w3.org/TR/2003/REC-DOM-Level-2-HTML-20030109/"
W3C_GROUP = """This document is also available in these non-normative formats:
[XML file](DOM2-HTML.xml), [plain text](DOM2-HTML.txt), [PostScript file](DOM2-HTML.ps),
[PDF file](DOM2-HTML.pdf), [single HTML file](DOM2-HTML.html), and [ZIP file](DOM2-HTML.zip).

## Abstract
The document object model describes the spec.
"""


def extract_w3c(markdown, *, enabled=True, base=W3C_BASE):
    with patch("domain_policy.socket.getaddrinfo",
               side_effect=AssertionError("Alternative links must never be fetched")):
        return subject.extract(
            {"links": {"internal": []}, "tables": []}, base,
            include_links=False, include_tables=False,
            include_alternatives=enabled, markdown=markdown)


class NonNormativeFormatTests(unittest.TestCase):
    def test_observed_w3c_dom2_html_publication_formats(self):
        result = extract_w3c(W3C_GROUP)
        root = W3C_BASE + "DOM2-HTML."
        self.assertEqual(result["alternatives"], [
            {"format": "txt", "url": root + "txt",
             "evidence": "source_labeled_non_normative_formats"},
            {"format": "html", "url": root + "html",
             "evidence": "source_labeled_non_normative_formats"},
            {"format": "pdf", "url": root + "pdf",
             "evidence": "source_labeled_non_normative_formats"},
        ])
        info = result["structured_info"]
        self.assertEqual(info["alternatives"]["returned"], 3)
        self.assertEqual(info["alternatives"]["evidence"],
                         "source_labeled_non_normative_formats")
        self.assertFalse(info["alternatives"]["format_verified"])
        self.assertEqual(info["source_completeness"], "unknown")

    def test_rfc_parser_and_unopted_fetch_remain_identical(self):
        rfc = extract(GROUP)
        self.assertEqual(rfc["structured_info"]["alternatives"]["evidence"],
                         "source_labeled_view_as")
        self.assertEqual(extract_w3c(W3C_GROUP, enabled=False), {})
        self.assertEqual(extract_w3c("# No publisher formats")["alternatives"], [])
        self.assertEqual(extract_w3c(
            "This document is also available in these non-normative formats:\n"
            "[single HTML file](DOM2-HTML.html)\n\n")["alternatives"], [])

    def test_sibling_and_original_host_constraints(self):
        intro = "This document is also available in these non-normative formats:\n"
        variants = [
            # Link to a different document, including a mixed-format list.
            "[plain text](DOM2-HTML.txt), [PDF file](Other.pdf)",
            # Same filenames on another public host are NOT source siblings.
            "[plain text](https://other.example/TR/2003/REC-DOM-Level-2-HTML-20030109/DOM2-HTML.txt), "
            "[PDF file](DOM2-HTML.pdf)",
            # A different directory cannot be treated as the same spec.
            "[plain text](../another/DOM2-HTML.txt), [PDF file](DOM2-HTML.pdf)",
            # A query changes representation identity; no guessing.
            "[plain text](DOM2-HTML.txt?token=private), [PDF file](DOM2-HTML.pdf)",
            # Even an empty query delimiter may distinguish an origin resource.
            "[plain text](DOM2-HTML.txt?), [PDF file](DOM2-HTML.pdf)",
            "[plain text](DOM2-HTML.txt?#fragment), [PDF file](DOM2-HTML.pdf)",
            "[plain text](https://www.w3.org/TR/2003/REC-DOM-Level-2-HTML-20030109/DOM2-HTML.txt?), "
            "[PDF file](DOM2-HTML.pdf)",
            # URLs with credentials, unsafe ports and schemes are excluded.
            "[plain text](https://someone:password@www.w3.org/TR/2003/REC-DOM-Level-2-HTML-20030109/DOM2-HTML.txt), "
            "[PDF file](DOM2-HTML.pdf)",
            "[plain text](javascript:alert), [PDF file](DOM2-HTML.pdf)",
        ]
        for group in variants:
            with self.subTest(group=group):
                self.assertEqual(extract_w3c(intro + group + "\n\n")["alternatives"], [])

    def test_conflicting_duplicates_or_multiple_documents_are_rejected(self):
        marker = "This document is also available in these non-normative formats:\n"
        contradictory = (marker +
            "[plain text](DOM2-HTML.txt), [plain text](DOM2-HTML-copy.txt), "
            "[PDF file](DOM2-HTML.pdf)\n\n")
        self.assertEqual(extract_w3c(contradictory)["alternatives"], [])
        two_documents = (marker +
            "[plain text](DOM2-HTML.txt), [PDF file](DOM2-HTML.pdf), "
            "[single HTML file](DOM3-HTML.html)\n\n")
        self.assertEqual(extract_w3c(two_documents)["alternatives"], [])

    def test_fenced_fake_examples_and_unrelated_navigation(self):
        self.assertEqual(extract_w3c("~~~markdown\n" + W3C_GROUP +
                                     "~~~\n")["alternatives"], [])
        self.assertEqual(extract_w3c(
            "### File formats\n[plain text](DOM2-HTML.txt) "
            "[PDF file](DOM2-HTML.pdf)")["alternatives"], [])
        self.assertEqual(extract_w3c(
            "> This document is also available in these non-normative formats:\n"
            "> [plain text](DOM2-HTML.txt), [PDF file](DOM2-HTML.pdf)\n\n"
        )["alternatives"], [])

    def test_no_cross_paragraph_pair_or_unsafe_control_links(self):
        marker = "This document is also available in these non-normative formats:\n"
        separated = marker + "[plain text](DOM2-HTML.txt)\n\n" + \
            "A separate unrelated attachment: [PDF file](DOM2-HTML.pdf)\n"
        self.assertEqual(extract_w3c(separated)["alternatives"], [])
        unsafe = marker + "[plain text](DOM2-HTML.txt%0a), [PDF file](DOM2-HTML.pdf)\n\n"
        self.assertEqual(extract_w3c(unsafe)["alternatives"], [])

    def test_strict_budget_and_default_public_output_size(self):
        item = extract_w3c(W3C_GROUP)
        self.assertLessEqual(subject._size(item), subject.MAX_OUTPUT_BYTES)
        self.assertEqual(subject._advertised_w3c_style_alternatives(
            W3C_GROUP, W3C_BASE, budget=1), [])
        self.assertLessEqual(len(item["alternatives"]), subject.MAX_ALTERNATIVES)


if __name__ == "__main__":
    unittest.main()
