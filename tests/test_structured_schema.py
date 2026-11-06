"""Offline security and resource regression tests for bounded schema extraction."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import structured_schema as subject


def spec(strategy="css"):
    return {"strategy": strategy,
            "base_selector": "article.card" if strategy == "css" else "//article",
            "fields": [
                {"name": "headline", "selector": "h2.title" if strategy == "css" else ".//h2",
                 "type": "text"},
                {"name": "hint", "selector": "img" if strategy == "css" else ".//img",
                 "type": "attribute", "attribute": "alt"},
            ]}


class StructuredSchemaTests(unittest.TestCase):
    def test_css_and_xpath_build_only_reviewed_non_llm_strategy(self):
        for strategy, klass in (("css", "JsonCssExtractionStrategy"),
                                ("xpath", "JsonXPathExtractionStrategy")):
            with self.subTest(strategy=strategy):
                canonical = subject.validate_spec(spec(strategy))
                payload = subject.to_crawl4ai_config(canonical)
                self.assertEqual(set(payload), {"type", "params"})
                self.assertEqual(payload["type"], "CrawlerRunConfig")
                upstream = payload["params"]["extraction_strategy"]
                self.assertEqual(upstream["type"], klass)
                self.assertEqual(upstream["params"]["schema"]["baseSelector"],
                                 canonical["base_selector"])
                self.assertEqual(len(upstream["params"]["schema"]["fields"]), 2)
                self.assertNotIn("js_code", str(payload))
                self.assertNotIn("regex", str(payload))
                self.assertNotIn("url", payload)

    def test_reject_wrong_schema_keys_strategy_and_size(self):
        invalid = [None, [], {}, {"strategy": "css"},
                   {**spec(), "proxy": "127.0.0.1"},
                   {**spec(), "strategy": "regex"},
                   {**spec(), "strategy": None},
                   {**spec(), "fields": []},
                   {**spec(), "fields": spec()["fields"] * 5},
                   {**spec(), "fields": [None]},
                   {**spec(), "fields": {"name": "item"}}]
        for value in invalid:
            with self.subTest(value=str(value)[:80]), self.assertRaises(
                    subject.StructuredSchemaRejected):
                subject.validate_spec(value)

    def test_reject_css_selectors_with_js_css_functions_and_complexity(self):
        invalid = ["", "h2:hover", "div:has(.private)", "div[onclick]",
                   "div,script", "div *", "div > a", "div + a",
                   "div:not(.private)", "div\nscript", "div\tspan",
                   "a[href^='javascript:']", "div\\:foo", "div  span",
                   "article " + "a " * 10, "x" * 100, "//article",
                   "div#foo#bar#baz", "div[attr='abc']"]
        for selector in invalid:
            with self.subTest(selector=selector), self.assertRaises(
                    subject.StructuredSchemaRejected):
                subject.validate_spec({**spec(), "base_selector": selector})
        safe = ["article", ".card", "article.card", "#main", "article h2.title"]
        for selector in safe:
            with self.subTest(safe=selector):
                subject.validate_spec({**spec(), "base_selector": selector})

    def test_reject_xpath_functions_predicates_and_complexity(self):
        invalid = ["", "article", "//article[1]", "count(//a)", "//a/@href",
                   "//div|//script", "//div//span", ".//div/../span",
                   "//div[@id='x']", "//div[contains(@class,'x')]",
                   "ancestor::script", "/html/body", ".//x/*", "//a\nscript",
                   "//" + "/".join(["div"] * 7), "//" + "a" * 100]
        for selector in invalid:
            with self.subTest(selector=selector), self.assertRaises(
                    subject.StructuredSchemaRejected):
                subject.validate_spec({**spec("xpath"), "base_selector": selector})
        for selector in ["//article", ".//h2", "./div/h2", "//article/header/h2"]:
            subject.validate_spec({**spec("xpath"), "base_selector": selector})

    def test_reject_field_name_duplicates_unsupported_properties_and_methods(self):
        cases = [
            [{**spec()["fields"][0], "name": "bad space"}],
            [{**spec()["fields"][0], "name": "__import__"}],
            [spec()["fields"][0], spec()["fields"][0]],
            [{**spec()["fields"][0], "type": "regex", "pattern": "(a+)+"}],
            [{**spec()["fields"][0], "type": "computed", "expression": "eval"}],
            [{**spec()["fields"][0], "transform": "lowercase"}],
            [{**spec()["fields"][0], "source": "+ script"}],
            [{**spec()["fields"][0], "type": "attribute", "attribute": "onclick"}],
            [{**spec()["fields"][0], "type": "attribute", "attribute": "href"}],
            [{**spec()["fields"][0], "type": "attribute"}],
            [{**spec()["fields"][0], "selector": "a:hover"}],
            [{**spec()["fields"][0], "selector": "h2", "default": "x"}],
        ]
        for fields in cases:
            with self.subTest(fields=str(fields)[:100]), self.assertRaises(
                    subject.StructuredSchemaRejected):
                subject.validate_spec({**spec(), "fields": fields})

    def test_no_automatic_network_side_effect(self):
        with (patch("socket.create_connection", side_effect=AssertionError("network")),
              patch("builtins.open", side_effect=AssertionError("filesystem")),
              patch("subprocess.Popen", side_effect=AssertionError("process"))):
            validated = subject.validate_spec(spec())
            result = subject.normalize_extracted_content(
                '[{"headline":"Public headline","hint":null}]', validated)
        self.assertEqual(result["rows_returned"], 1)
        self.assertFalse(result["authorization_verified"])

    def test_normalize_deterministic_text_and_missing_fields(self):
        # Build a hostile markup-shaped string from non-markup fragments to
        # comply with AGENTS.md's separation of web source from Python.
        hostile_text = "".join(("<", "script", ">", "untrusted-page-text",
                                "<", "/", "script", ">"))
        raw = json.dumps([{"headline": hostile_text, "hint": None},
                          {"headline": "a\u0000b\nc"}])
        result = subject.normalize_extracted_content(raw, spec())
        self.assertEqual(result["records"][0]["headline"], hostile_text)
        self.assertIsNone(result["records"][0]["hint"])
        self.assertEqual(result["records"][1]["headline"], "a b c")
        self.assertIsNone(result["records"][1]["hint"])
        self.assertEqual(result["rows_observed_in_response"], 2)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["extraction_method"], "crawl4ai_json_css")
        self.assertEqual(result["site_completeness"], "unknown")
        self.assertFalse(result["authorization_verified"])
        self.assertEqual(result, subject.normalize_extracted_content(raw, spec()))

    def test_unknown_fields_invalid_nested_and_non_text_data_rejected(self):
        invalid = [
            '{}', 'null', '"text"', '[1]',
            '[{"headline":"ok","unknown":"leak"}]',
            '[{"headline":{"nested":"value"}}]',
            '[{"headline":["list"]}]',
            '[{"headline":12}]',
            '[{"headline":true}]',
            '[{"headline":1.0}]',
            '[{"headline":NaN}]',
            '[{"headline":"a","headline":"b"}]',
            '', '[', "x" * (subject.MAX_INPUT_BYTES + 1),
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:60]), self.assertRaises(
                    subject.StructuredSchemaRejected):
                subject.normalize_extracted_content(raw, spec())

    def test_reject_malformed_rows_after_output_row_limit(self):
        first_rows = [{"headline": "valid"} for _ in range(subject.MAX_ROWS)]
        cases = [
            {"headline": {"unexpected": "nested"}},
            {"unknown": "unexpected field"},
            {"headline": 23},
            ["nested"],
            {"headline": "\\ud800"},
        ]
        for invalid in cases:
            with self.subTest(invalid=repr(invalid)):
                raw = json.dumps(first_rows + [invalid])
                with self.assertRaises(subject.StructuredSchemaRejected):
                    subject.normalize_extracted_content(raw, spec())

    def test_reject_malformed_rows_after_output_byte_limit(self):
        # The first eight fields exhaust the 12 KiB output budget before
        # the last row; excluded rows must still be validated.
        many = {f"f{i}": "界" * 100 for i in range(subject.MAX_FIELDS)}
        schema = {"strategy": "css", "base_selector": "article",
                  "fields": [{"name": key, "selector": "h2", "type": "text"}
                             for key in many]}
        for invalid in ({"f0": {"nested": "bad"}},
                        {"unexpected": "value"},
                        {"f0": True}):
            with self.subTest(invalid=invalid):
                raw = json.dumps([many] * 19 + [invalid], ensure_ascii=False)
                self.assertLess(len(raw.encode("utf-8")), subject.MAX_INPUT_BYTES)
                with self.assertRaises(subject.StructuredSchemaRejected):
                    subject.normalize_extracted_content(raw, schema)

    def test_full_json_envelope_never_exceeds_limit_at_boolean_boundary(self):
        # Regression: the old check measured truncated=true (4 bytes) while
        # returning truncated=false (5 bytes), admitting a 12,289-byte result.
        columns = [f"f{i}" for i in range(subject.MAX_FIELDS)]
        schema = {"strategy": "css", "base_selector": "article",
                  "fields": [{"name": key, "selector": "h2", "type": "text"}
                             for key in columns]}
        rows = [{key: "\\" * 33 for key in columns}
                for _ in range(subject.MAX_ROWS)]
        rows[-1][columns[-1]] = "\\" * 146 + "a"
        raw = json.dumps(rows)
        self.assertLess(len(raw.encode("utf-8")), subject.MAX_INPUT_BYTES)
        result = subject.normalize_extracted_content(raw, schema)
        encoded = json.dumps(result, ensure_ascii=True,
                             separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), subject.MAX_OUTPUT_BYTES)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["rows_returned"], subject.MAX_ROWS - 1)
        self.assertEqual(result["rows_observed_in_response"], subject.MAX_ROWS)

    def test_complete_full_json_envelope_never_exceeds_limit(self):
        raw = json.dumps([{"headline": "safe", "hint": "hint"}])
        result = subject.normalize_extracted_content(raw, spec())
        self.assertFalse(result["truncated"])
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=True,
                           separators=(",", ":")).encode("utf-8")),
            subject.MAX_OUTPUT_BYTES)

    def test_reject_json_lone_surrogate(self):
        with self.assertRaises(subject.StructuredSchemaRejected):
            subject.normalize_extracted_content('[{"headline":"\\ud800"}]', spec())

    def test_clip_large_cell_and_expose_truncation(self):
        raw = json.dumps([{"headline": "h" * 400, "hint": "notice"}])
        out = subject.normalize_extracted_content(raw, spec())
        self.assertEqual(len(out["records"][0]["headline"]), subject.MAX_CELL_CHARS)
        self.assertTrue(out["truncated"])

    def test_row_limit_and_result_limit(self):
        raw = json.dumps([{"headline": f"headline {i}"} for i in range(60)])
        out = subject.normalize_extracted_content(raw, spec())
        self.assertEqual(out["rows_observed_in_response"], 60)
        self.assertEqual(out["rows_returned"], subject.MAX_ROWS)
        self.assertTrue(out["truncated"])

    def test_escaped_output_bytes_are_bounded(self):
        extended = {f"f{i}": "界" * 100 for i in range(subject.MAX_FIELDS)}
        schema = {"strategy": "css", "base_selector": "article",
                  "fields": [{"name": name, "selector": "h2", "type": "text"}
                             for name in extended]}
        raw = json.dumps([extended] * 20, ensure_ascii=False)
        self.assertLess(len(raw.encode("utf-8")), subject.MAX_INPUT_BYTES)
        out = subject.normalize_extracted_content(raw, schema)
        self.assertTrue(out["truncated"])
        self.assertLess(out["rows_returned"], 20)
        self.assertLessEqual(len(json.dumps({"records": out["records"]},
                                            ensure_ascii=True).encode("utf-8")),
                             subject.MAX_OUTPUT_BYTES)

    def test_already_invalid_upstream_results_rejected_before_any_release(self):
        with self.assertRaises(subject.StructuredSchemaRejected):
            subject.normalize_extracted_content(
                '[{"headline":"ok","hint":{"secret":"value"}}]', spec())


if __name__ == "__main__":
    unittest.main()
