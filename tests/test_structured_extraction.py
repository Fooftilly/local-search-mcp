"""Bounded structured crawl data: no followup requests, local files or grants."""
import json
import unittest
from unittest.mock import patch

import structured_extraction as subject


class StructuredExtractionTests(unittest.TestCase):
    def test_relative_and_external_links_and_dangerous_urls(self):
        raw = {"links": {"internal": [
            {"href": "../notice?a=1#part", "text": " Notice ", "title": "Details"},
            {"href": "javascript:alert(1)", "text": "no"},
            {"href": "file:///etc/passwd"},
            {"href": "http://127.0.0.1/private"},
            {"href": "//localhost/health"},
            {"href": "https://allowed.example@evil.example/data"},
            {"href": "https://safe.example/fine"},
            {"href": "https://safe.example/ctrl\nnewline"},
            {"href": "https://faß.de/path"},
            {"href": 23},
        ], "external": [
            {"href": "https://other.example/document", "text": "External"},
        ]}}
        with patch("domain_policy.socket.getaddrinfo",
                   side_effect=AssertionError("No DNS should be issued")):
            actual = subject.extract(raw, "https://safe.example/folder/page",
                                     include_links=True, include_tables=False)
        self.assertEqual([row["url"] for row in actual["links"]], [
            "https://safe.example/notice?a=1",
            "https://safe.example/fine",
            "https://other.example/document",
        ])
        self.assertEqual([row["kind"] for row in actual["links"]],
                         ["internal", "internal", "external"])
        self.assertEqual(actual["links"][0]["text"], "Notice")
        self.assertNotIn("tables", actual)

    def test_explicit_empty_query_does_not_inherit_original_query(self):
        cases = {
            "?": "https://example.com/article?",
            "?#section": "https://example.com/article?",
            "other?": "https://example.com/other?",
            "/absolute?": "https://example.com/absolute?",
            "https://outside.example/target?": "https://outside.example/target?",
            "?edition=latest": "https://example.com/article?edition=latest",
            "?q=what?": "https://example.com/article?q=what?",
            "/other?q=what?": "https://example.com/other?q=what?",
            "https://outside.example/target?q=what?": "https://outside.example/target?q=what?",
            "??": "https://example.com/article??",
            "#section": "https://example.com/article?edition=old",
        }
        for href, expected in cases.items():
            with self.subTest(href=href):
                links = subject.extract(
                    {"links": {"internal": [{"href": href}]}},
                    "https://example.com/article?edition=old",
                    include_links=True, include_tables=False)["links"]
                self.assertEqual([row["url"] for row in links], [expected])

    def test_navigation_overflow_prioritizes_descriptive_links_and_external(self):
        header = [{"href": f"/menu/nav-{i}", "text": "Menu"}
                  for i in range(80)]
        reports = [{"href": f"/articles/report-{i}",
                    "text": f"Detailed public report number {i}"}
                   for i in range(9)]
        mirrors = [{"href": f"/articles/report-{i}#section", "text": "More"}
                   for i in range(9)]
        external = [{"href": f"https://other.example/research/paper-{i}",
                     "text": f"External research paper number {i}"}
                    for i in range(5)]
        payload = {"links": {"internal": header + reports + mirrors,
                             "external": external},
                   "tables": [{"headers": ["H"], "rows": [["R"]]}]}
        with patch("domain_policy.socket.getaddrinfo",
                   side_effect=AssertionError("No DNS or link traversal")):
            output = subject.extract(payload, "https://site.example/",
                                     include_links=True, include_tables=True)
            repeat = subject.extract(payload, "https://site.example/",
                                     include_links=True, include_tables=True)
        self.assertEqual(output, repeat)
        links = output["links"]
        self.assertEqual(len(links), subject.MAX_LINKS)
        self.assertEqual(len({link["url"] for link in links}), len(links))
        for i in range(9):
            self.assertIn(f"https://site.example/articles/report-{i}",
                          [link["url"] for link in links])
        self.assertGreaterEqual(
            sum(link["kind"] == "external" for link in links), 2)
        self.assertEqual(output["tables"][0]["rows"], [["R"]])
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=True).encode()),
                             subject.MAX_OUTPUT_BYTES + 200)
        # Descriptive content is represented before low-value navigation.
        self.assertIn("/articles/", links[0]["url"])

    def test_link_dedup_prefers_descriptive_label_without_changing_first_kind(self):
        payload = {"links": {"internal": [
            {"href": "/article#header", "text": "Menu"},
            {"href": "/article#content", "text": "Detailed public article report"},
            {"href": "/article", "title": "Official announcement"},
            {"href": "https://site.example/article?version=1", "text": "Edition 1"},
            {"href": "https://site.example/article?version=1#fragment",
             "text": "Edition 1 duplicate"}],
            "external": [{"href": "https://site.example/article",
                          "text": "External duplicate"}]}}
        result = subject.extract(payload, "https://site.example/",
                                 include_links=True, include_tables=False)["links"]
        self.assertEqual([x["url"] for x in result],
                         ["https://site.example/article",
                          "https://site.example/article?version=1"])
        self.assertEqual(result[0]["kind"], "internal")
        self.assertEqual(result[0]["text"], "Detailed public article report")
        self.assertEqual(result[0]["title"], "Official announcement")
        self.assertEqual(result[1]["text"], "Edition 1 duplicate")

    def test_link_budget_skips_oversized_entries_and_backfills_short_links(self):
        # Twenty-four high-scoring long URLs used to exhaust the selection
        # before budget checks, hiding a short useful candidate in 25th place.
        large = [{"href": "/articles/" + "x" * 1830 + str(i),
                  "text": "Detailed public research report"}
                 for i in range(24)]
        short_url = "https://example.com/articles/short-report"
        page = {"links": {"internal": large + [
            {"href": "/articles/short-report",
             "text": "Detailed public research report"}]}}
        output = subject.extract(page, "https://example.com/",
                                 include_links=True, include_tables=False)
        urls = [entry["url"] for entry in output["links"]]
        self.assertIn(short_url, urls)
        self.assertLess(len(output["links"]), subject.MAX_LINKS)
        self.assertLessEqual(len(json.dumps(output, ensure_ascii=True).encode()),
                             subject.MAX_OUTPUT_BYTES + 200)

    def test_under_link_cap_preserves_safe_crawl_order_and_size_limits(self):
        payload = {"links": {"internal": [
            {"href": "/short", "text": "Short"},
            {"href": "/articles/report", "text": "Detailed report"}],
            "external": [{"href": "https://other.example/x", "text": "Reference"}]}}
        result = subject.extract(payload, "https://site.example/",
                                 include_links=True, include_tables=False)["links"]
        self.assertEqual([x["url"] for x in result],
                         ["https://site.example/short",
                          "https://site.example/articles/report",
                          "https://other.example/x"])
        self.assertNotIn("score", result[0])
        self.assertLessEqual(len(result), subject.MAX_LINKS)

    def test_tables_bounded_and_text_safe(self):
        page = {"tables": [{
            "headers": ["h"] * 30,
            "rows": [["cell" * 200] * 20 for _ in range(50)],
            "caption": "A" * 5000,
            "summary": "B" * 5000,
        }] * 12}
        output = subject.extract(page, "https://example.com/a",
                                 include_links=False, include_tables=True)
        self.assertLessEqual(len(output["tables"]), 4)
        self.assertLessEqual(len(json.dumps(output).encode()), subject.MAX_OUTPUT_BYTES + 100)
        for table in output["tables"]:
            self.assertLessEqual(len(table["headers"]), subject.MAX_COLUMNS)
            self.assertLessEqual(len(table["rows"]), subject.MAX_ROWS)
            self.assertLessEqual(len(table["caption"]), 200)
            for record in table["rows"]:
                self.assertLessEqual(len(record), subject.MAX_COLUMNS)
                self.assertTrue(all(len(cell) <= 200 for cell in record))
        self.assertNotIn("links", output)

    def test_malformed_structures_never_crash_or_leak_python_objects(self):
        tests = [
            {"links": 3, "tables": {"rows": "bad"}},
            {"links": {"internal": [None, {}, {"href": []}], "external": "bad"},
             "tables": [{}, None, {"rows": [{"private": "unwanted"}]}]},
            {"links": {"internal": [{"href": "https://example.com/a", "text": "\ud800"}]},
             "tables": [{"headers": ["h"], "rows": [[float("nan"), {"private": "bad"}, "\ud800"]]}]},
        ]
        for page in tests:
            with self.subTest(page=str(page)[:50]):
                result = subject.extract(page, "https://example.com/",
                                         include_links=True, include_tables=True)
                encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.assertNotIn(b"unwanted", encoded)
                self.assertNotIn(b"private", encoded)

    def test_no_flags_keep_original_shape_and_all_entries_bounded(self):
        page = {"links": {"internal": [{"href": f"/{i}", "text": "z" * 9999}
                                       for i in range(1500)]},
                "tables": [{"headers": ["H"], "rows": [["x"]]}] * 100}
        self.assertEqual(subject.extract(page, "https://example.com/",
                                         include_links=False, include_tables=False), {})
        answer = subject.extract(page, "https://example.com/",
                                 include_links=True, include_tables=True)
        self.assertLessEqual(len(answer["links"]), subject.MAX_LINKS)
        self.assertLessEqual(len(answer["tables"]), subject.MAX_TABLES)
        self.assertLessEqual(len(json.dumps(answer).encode("utf-8")),
                             subject.MAX_OUTPUT_BYTES + 200)

    def test_invalid_base_or_link_schemes_do_not_become_fetchable(self):
        for url in ["data:text/html,secret", "ftp://example.com/a", "//10.0.0.1/x",
                    "http://example.local/a", "https://evil.invalid/a",
                    "https://ex ample.com/", "https://example.com:8080/",
                    "https://example.com/%0a"] * 2:
            with self.subTest(url=url):
                actual = subject.extract(
                    {"links": {"internal": [{"href": url}]}}, "https://example.com/",
                    include_links=True, include_tables=False)
                self.assertEqual(actual["links"], [])


if __name__ == "__main__":
    unittest.main()
