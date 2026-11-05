"""Regression tests for offline sitemap evidence comparison (#298).

No network, MCP, credential, policy, or filesystem outside temporary inputs.
"""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scripts import url_mapping_benchmark as benchmark


class URLMappingBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.home = {
            "success": True, "url": "https://www.example.org/",
            "links": [{"url": "https://www.example.org/a/"},
                      {"url": "https://other.example.org/"}],
            "structured_info": {"links": {
                "unique_safe_candidates_examined": 2, "truncated": True}},
            "link_continuation": {"retained_links": 1, "link_id": "L" * 32},
        }
        self.sitemap_url = "https://www.example.org/sitemap.xml"

    def page(self, number, count, text):
        return {"success": True, "url": self.sitemap_url,
                "page": number, "page_count": count, "markdown": text,
                "document_id": "D" * 32}

    def test_split_url_reconstructed_only_after_concatenating_pages(self):
        first = self.page(1, 2, "https://www.example.org/a/ https://www.exa")
        second = self.page(2, 2, "mple.org/new/ https://other.org/no/ ")
        result = benchmark.analyze(self.home, [first, second], [
            {"success": True, "url": self.home["url"], "link_id": "L" * 32,
             "offset": 0, "retained_links": 1,
             "links": [{"url": "https://www.example.org/x/"}]}])
        self.assertEqual(result["homepage"]["observed_unique_in_scope_links"], 2)
        self.assertEqual(result["sitemap"]["heuristic_unique_same_host_url_candidates"], 2)
        self.assertEqual(result["sitemap"]["heuristic_candidates_not_in_observed_homepage_links"], 1)
        self.assertTrue(result["sitemap"]["all_extracted_pages_supplied"])
        self.assertFalse(result["sitemap"]["xml_structure_verified"])
        self.assertFalse(result["homepage"]["link_continuation_incomplete"])

    def test_missing_page_and_link_continuation_reported(self):
        result = benchmark.analyze(
            self.home, [self.page(1, 2, "https://www.example.org/a/ https://www.exa")])
        self.assertEqual(result["sitemap"]["heuristic_unique_same_host_url_candidates"], 1)
        self.assertFalse(result["sitemap"]["all_extracted_pages_supplied"])
        self.assertTrue(result["homepage"]["link_continuation_incomplete"])

    def test_rejects_missing_reordered_or_foreign_sitemap(self):
        for pages in (
            [self.page(2, 2, "x")],
            [self.page(1, 2, "x"), self.page(1, 2, "y")],
            [{**self.page(1, 1, "x"), "url": "https://bad.example.org/sitemap.xml"}],
            [self.page(1, 43, "x")],
            [self.page(1, 1, "x"), self.page(2, 2, "y")],
        ):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                benchmark.analyze(self.home, pages)

    def test_rejects_bad_or_out_of_scope_candidate_urls(self):
        result = benchmark.analyze(
            self.home, [self.page(1, 1, "https://www.example.org/z/ "
                                  "https://sub.www.example.org/a/ "
                                  "https://bad.org/no/ ")])
        self.assertEqual(result["sitemap"]["heuristic_unique_same_host_url_candidates"], 1)
        self.assertEqual(result["sitemap"]["heuristic_candidates_not_in_observed_homepage_links"], 1)
        self.assertIsNone(benchmark._candidate(
            "https://www.example.org:8080/", "www.example.org"))
        self.assertIsNone(benchmark._candidate(
            "https://u:p@www.example.org/", "www.example.org"))

    def test_last_non_delimited_url_not_claimed_complete(self):
        complete = benchmark.analyze(self.home, [
            self.page(1, 1, "https://www.example.org/z/ ")])
        fragment = benchmark.analyze(self.home, [
            self.page(1, 1, "https://www.example.org/z/")])
        self.assertEqual(complete["sitemap"]["heuristic_unique_same_host_url_candidates"], 1)
        self.assertEqual(fragment["sitemap"]["heuristic_unique_same_host_url_candidates"], 0)

    def _lines(self, *items):
        return b"".join(
            json.dumps(item).encode("utf-8") + b"\n" for item in items)

    def test_cli_reads_stdin_only_and_redacts_source_urls(self):
        stream = io.BytesIO(self._lines(
            self.home, self.page(1, 1, "https://www.example.org/new/ ")))
        output = io.StringIO()
        with patch("builtins.open", side_effect=AssertionError("No file opens allowed")):
            with contextlib.redirect_stdout(output):
                status = benchmark.main(["--sitemap-pages", "1"], stdin=stream)
        self.assertEqual(status, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(
            summary["sitemap"]["heuristic_unique_same_host_url_candidates"], 1)
        self.assertNotIn("example.org", output.getvalue())

    def test_cli_rejects_oversized_missing_and_extra_records(self):
        valid = self._lines(self.home, self.page(1, 1, "URL "))
        bad_inputs = (
            valid + b"{}\n",
            self._lines(self.home),
            self._lines(self.home) + b"x" * (benchmark.MAX_RECORD_BYTES + 1),
            valid[:-1],
            self._lines(self.home) + b"{invalid}\n",
            self._lines(self.home) + b"\xff\n",
        )
        for raw in bad_inputs:
            with self.subTest(length=len(raw)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = benchmark.main(
                        ["--sitemap-pages", "1"], stdin=io.BytesIO(raw))
                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(output.getvalue())["status"],
                    "invalid_benchmark_input")

    def test_cli_rejects_path_arguments_and_invalid_page_counts(self):
        # The benchmark no longer accepts any operator-controlled file path.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                benchmark.main(["--homepage", "/etc/shadow",
                                "--sitemap-pages", "1"], stdin=io.BytesIO())
        for args in (["--sitemap-pages", "0"],
                     ["--sitemap-pages", "43"],
                     ["--sitemap-pages", "1", "--link-pages", "3"]):
            with self.subTest(args=args):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        benchmark.main(args, stdin=io.BytesIO()), 1)
                self.assertNotIn("/etc", output.getvalue())

    def test_mixed_continuation_handles_and_offsets_fail_closed(self):
        first = self.page(1, 2, "https://www.example.org/a/ ")
        second = {**self.page(2, 2, "https://www.example.org/b/ "),
                  "document_id": "other"}
        with self.assertRaises(ValueError):
            benchmark.analyze(self.home, [first, second])
        link = {"success": True, "url": self.home["url"],
                "link_id": "L" * 32, "offset": 0, "retained_links": 1,
                "links": [{"url": "https://www.example.org/x/"}]}
        for changed in (
            {"url": "https://bad.example.org/"},
            {"link_id": "other"},
            {"offset": 10},
            {"retained_links": 2},
            {"links": []},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                benchmark.analyze(self.home, [self.page(1, 1, "")],
                                  [{**link, **changed}])

    def test_invalid_optional_fields_cannot_crash_or_overstate_counts(self):
        malformed = dict(self.home)
        malformed["structured_info"] = "malformed"
        malformed["link_continuation"] = {"retained_links": "invalid"}
        with self.assertRaises(ValueError):
            benchmark.analyze(malformed, [self.page(1, 1, "")])
        malformed["link_continuation"] = None
        result = benchmark.analyze(malformed, [self.page(1, 1, "")])
        self.assertIsNone(result["homepage"]["reported_unique_safe_candidates_examined"])


if __name__ == "__main__":
    unittest.main()
