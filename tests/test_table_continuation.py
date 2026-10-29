"""Table continuation: bounded normalization, shared memory and expiry."""
from collections import OrderedDict
import json
import unittest
from unittest.mock import patch

import document_pages as pages
import structured_extraction as extraction


def source(tables=6, rows=23):
    return {"tables": [
        {"headers": ["Name", "Value"],
         "rows": [[f"table {i} row {j}", str(j)] for j in range(rows)],
         "caption": f"Table {i}"} for i in range(tables)]}


class TableContinuationTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch.object(pages, "_CACHE", OrderedDict())
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_more_than_four_tables_and_ten_rows_without_gaps(self):
        payload = extraction.prepare_table_continuation(source())
        self.assertEqual(payload["source_tables_observed"], 6)
        token = pages.save_tables("https://example.com/final", payload,
                                  source_url="https://example.com/start")
        visited = []
        offset = 0
        while True:
            result, original, proof = pages.read_table_with_security_context(
                token, 5, row_offset=offset, max_rows=7)
            self.assertTrue(result["success"])
            self.assertEqual(original, "https://example.com/start")
            self.assertIsNone(proof)
            visited.extend(row[0] for row in result["rows"])
            if not result["has_more"]:
                self.assertIsNone(result["next_offset"])
                break
            self.assertGreater(result["next_offset"], offset)
            offset = result["next_offset"]
        self.assertEqual(visited, [f"table 5 row {j}" for j in range(23)])
        self.assertEqual(result["source_rows_observed"], 23)
        self.assertFalse(result["retained_truncated"])
        self.assertEqual(result["source_completeness"], "unknown")

    def test_unicode_malformed_columns_and_retention_are_bounded(self):
        raw = source(19, 275)
        raw["tables"][0]["headers"] = ["😀" * 200] * 16
        raw["tables"][0]["rows"][0] = ["\u202e" * 300] * 20
        raw["tables"][1]["rows"][3] = {"not": "a row"}
        raw["tables"][2]["rows"][3] = [None, 4, ["bad"], "a" * 2000]
        payload = extraction.prepare_table_continuation(raw)
        self.assertLessEqual(payload["size_bytes"], extraction.MAX_RETAINED_BYTES)
        self.assertTrue(payload["retention_truncated"])
        self.assertLessEqual(len(payload["entries"]), 16)
        for table in payload["entries"]:
            self.assertLessEqual(len(table["rows"]), 250)
            self.assertLessEqual(len(table["headers"]), extraction.MAX_RETAINED_COLUMNS)
            for row in table["rows"]:
                self.assertLessEqual(len(row), extraction.MAX_RETAINED_COLUMNS)
                self.assertLessEqual(len(json.dumps({"cells": row},
                    ensure_ascii=True).encode()), extraction.MAX_ROW_BYTES)
        self.assertTrue(payload["entries"][0]["retained_truncated"])

    def test_column_continuation_recovers_bounded_columns_without_duplicates(self):
        src = source(1, 12)
        src["tables"][0]["headers"] = [f"H{i}" for i in range(18)]
        src["tables"][0]["rows"] = [
            [f"R{row}C{column}" for column in range(18)] for row in range(12)]
        payload = extraction.prepare_table_continuation(src)
        token = pages.save_tables("https://example.com/a", payload,
                                  source_url="https://example.com/a")
        stitched = [[] for _ in range(12)]
        for column_offset in (0, 7, 14):
            row_offset = 0
            while True:
                page, _, _ = pages.read_table_with_security_context(
                    token, 0, row_offset, 5, column_offset, 7)
                for index, row in enumerate(page["rows"]):
                    stitched[row_offset + index].extend(row)
                if not page["has_more"]:
                    break
                row_offset = page["next_offset"]
        self.assertEqual(stitched, src["tables"][0]["rows"])
        first, _, _ = pages.read_table_with_security_context(
            token, 0, column_offset=0, max_columns=7)
        self.assertTrue(first["has_more_columns"])
        self.assertEqual(first["next_column_offset"], 7)
        final, _, _ = pages.read_table_with_security_context(
            token, 0, column_offset=14, max_columns=7)
        self.assertFalse(final["has_more_columns"])
        self.assertIsNone(final["next_column_offset"])

    def test_mixed_document_and_table_handles_share_single_budget(self):
        with patch.object(pages, "MAX_TOTAL_CHARS", 24_500):
            doc, _ = pages.save("https://example.com/doc", "d" * 24_100, {})
            payload = extraction.prepare_table_continuation(source(1, 30))
            handle = pages.save_tables("https://example.com/table", payload,
                                       source_url="https://example.com/table")
            self.assertIn(handle, pages._CACHE)
            self.assertEqual(pages.read(doc, 2)["status"], "expired_or_unknown")
        with patch.object(pages, "MAX_DOCUMENTS", 1):
            old = pages.save_tables("https://example.com/old", payload,
                                    source_url="https://example.com/old")
            new = pages.save_tables("https://example.com/new", payload,
                                    source_url="https://example.com/new")
        self.assertEqual(pages.read_table_with_security_context(old, 0)[0]["status"],
                         "expired_or_unknown")
        self.assertTrue(pages.read_table_with_security_context(new, 0)[0]["success"])

    def test_malformed_rows_match_preview_and_truncation_is_truthful(self):
        raw = {"tables": [{"headers": ["H"], "rows": [
            ["A"], {"bad": "not a row"}, ["B"]]}]}
        preview = extraction.extract(raw, "https://example.com/a",
                                     include_links=False, include_tables=True)
        payload = extraction.prepare_table_continuation(raw)
        self.assertEqual(payload["entries"][0]["rows"], preview["tables"][0]["rows"])
        self.assertTrue(payload["retention_truncated"])
        self.assertTrue(payload["entries"][0]["retained_truncated"])
        self.assertEqual(payload["entries"][0]["source_rows_observed"], 3)
        over = extraction.prepare_table_continuation(source(1, 251))
        self.assertEqual(len(over["entries"][0]["rows"]), 250)
        self.assertTrue(over["entries"][0]["retained_truncated"])
        self.assertTrue(over["retention_truncated"])
        wide = source(1, 11)
        wide["tables"][0]["headers"] = [f"H{i}" for i in range(30)]
        wide["tables"][0]["rows"][0] = [str(i) for i in range(30)]
        clipped = extraction.prepare_table_continuation(wide)
        self.assertTrue(clipped["entries"][0]["columns_truncated"])
        self.assertTrue(clipped["retention_truncated"])

    def test_batch_finalizer_rejects_already_evicted_table_capability(self):
        from batch_fetch import _finalize_result
        payload = extraction.prepare_table_continuation(source(1, 20))
        with patch.object(pages, "MAX_DOCUMENTS", 1):
            handle = pages.save_tables("https://example.com/table", payload,
                                       source_url="https://example.com/table")
            result = {"url": "https://example.com/table", "success": True,
                      "table_continuation": {"table_id": handle},
                      "tables": [{"rows": [["preview"]]}]}
            self.assertTrue(_finalize_result(result["url"], result,
                                              policy_changed=False)["success"])
            pages.save("https://example.com/long", "x" * 24_001, {})
            finalized = _finalize_result(result["url"], result,
                                         policy_changed=False)
        self.assertFalse(finalized["success"])
        self.assertEqual(finalized["status"], "continuation_evicted")
        self.assertNotIn("preview", str(finalized))
        self.assertNotIn(handle, str(finalized))

    def test_expiry_invalid_parameters_and_private_context(self):
        payload = extraction.prepare_table_continuation(source(1, 11))
        proof = ("example.com", "a" * 32)
        with patch.object(pages.time, "monotonic", return_value=100):
            token = pages.save_tables("https://example.com/a", payload,
                source_url="https://example.com/a", once_proof=proof)
        for args in [("bad", 0), (token, True), (token, 16),
                     (token, 0, -1), (token, 0, True), (token, 0, 0, 11)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                pages.read_table_with_security_context(*args)
        with patch.object(pages.time, "monotonic", return_value=101):
            value, original, retained_proof = pages.read_table_with_security_context(token, 0)
        self.assertEqual(retained_proof, proof)
        self.assertNotIn("once_proof", str(value))
        self.assertEqual(original, "https://example.com/a")
        with patch.object(pages.time, "monotonic",
                          return_value=100 + pages.TTL_SECONDS):
            expired = pages.read_table_with_security_context(token, 0)[0]
        self.assertEqual(expired["status"], "expired_or_unknown")
