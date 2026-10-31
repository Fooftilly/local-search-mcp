"""Comparison of two independently authorized temporary Markdown documents."""
from collections import OrderedDict
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client

import bridge
import document_comparison as diff
import document_pages as pages
import domain_policy
import history_cli
from scripts import document_compare_benchmark as corpus


class ComparisonAlgorithmTests(unittest.TestCase):
    def test_synthetic_benchmark_corpus_and_bounded_output(self):
        for name, (left, right) in corpus._cases().items():
            with self.subTest(name=name):
                result = diff.compare(left, right, max_changes=12)
                self.assertEqual(result["text_equal"], left == right)
                self.assertEqual(result["total_changed_regions"] == 0, left == right)
                self.assertLessEqual(len(result["changes"]), 12)
                self.assertLessEqual(
                    len(json.dumps(result, ensure_ascii=True).encode()),
                    diff.MAX_OUTPUT_BYTES)
                for change in result["changes"]:
                    self.assertLessEqual(len(change["left_excerpt"]), 120)
                    self.assertLessEqual(len(change["right_excerpt"]), 120)

    def test_unicode_offsets_replace_and_truncation(self):
        result = diff.compare("🧭 Život\nA\n", "🧭 Život\nŽ\n", context_chars=2)
        self.assertFalse(result["text_equal"])
        self.assertEqual(result["total_changed_regions"], 1)
        region = result["changes"][0]
        self.assertEqual(region["kind"], "replace")
        self.assertEqual(region["left_start_offset"], len("🧭 Život\n"))
        self.assertEqual(region["right_start_offset"], len("🧭 Život\n"))
        self.assertEqual(region["left_excerpt"], "A\n")
        self.assertEqual(region["right_excerpt"], "Ž\n")
        self.assertEqual(result["offset_units"],
                         "unicode_code_points_in_normalized_markdown")
        a = "".join(f"Anchor {i}\nOld {i}\n" for i in range(40))
        b = "".join(f"Anchor {i}\nNew {i}\n" for i in range(40))
        limited = diff.compare(a, b, max_changes=3)
        self.assertEqual(len(limited["changes"]), 3)
        self.assertTrue(limited["changes_truncated"])
        self.assertGreater(limited["total_changed_regions"], 3)

    def test_reject_unbounded_and_invalid_inputs(self):
        with self.assertRaises(ValueError):
            diff.compare("a" * (diff.MAX_CHARS + 1), "b")
        with self.assertRaises(ValueError):
            diff.compare("line\n" * (diff.MAX_LINES + 1), "b")
        for bad in (0, 13, True, "12"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                diff.compare("left", "right", max_changes=bad)
        for bad in (0, 121, False):
            with self.subTest(context_chars=bad), self.assertRaises(ValueError):
                diff.compare("left", "right", context_chars=bad)
        self.assertFalse(diff.compare("", "line")["text_equal"])


class ComparisonMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.root / "history.jsonl"),
        })
        env.start()
        self.addCleanup(env.stop)
        cache = patch.object(pages, "_CACHE", OrderedDict())
        cache.start()
        self.addCleanup(cache.stop)
        bridge.request_cache.reset_for_test()
        self.addCleanup(bridge.request_cache.reset_for_test)

    def allowed_pair(self, left="Source a\nOld date\n", right="Source a\nNew date\n"):
        for host in ("left.example.com", "right.example.com",
                     "source.example.com"):
            domain_policy.decide(host, "allow", scope="exact")
        left_id, _ = pages.save(
            "https://left.example.com/text?session=PRIVATE_LEFT",
            left, {}, source_url="https://source.example.com/left",
            allow_short=True)
        right_id, _ = pages.save(
            "https://right.example.com/text?session=PRIVATE_RIGHT",
            right, {}, source_url="https://source.example.com/right",
            allow_short=True)
        return left_id, right_id

    async def test_fastmcp_discovery_comparison_and_no_queries_in_history(self):
        l, r = self.allowed_pair()
        async with Client(bridge.mcp) as client:
            tools = {x.name: x for x in await client.list_tools()}
            self.assertIn("web_compare_documents", tools)
            fields = tools["web_compare_documents"].inputSchema["properties"]
            self.assertIn("left_document_id", fields)
            self.assertIn("right_document_id", fields)
            result = await client.call_tool("web_compare_documents", {
                "left_document_id": l, "right_document_id": r,
                "max_changes": 4, "context_chars": 25})
            self.assertFalse(result.is_error)
            data = result.data
            self.assertTrue(data["success"])
            self.assertFalse(data["text_equal"])
            self.assertEqual(data["total_changed_regions"], 1)
            self.assertEqual(data["changes"][0]["kind"], "replace")
            self.assertEqual(data["left_source_url"],
                             "https://source.example.com/left")
            self.assertEqual(data["right_final_url"],
                             "https://right.example.com/text")
            self.assertFalse(data["publisher_revision_verified"])
            self.assertFalse(data["chronological_order_verified"])
            self.assertEqual(data["source_completeness"], "unknown")
            self.assertNotIn(l, repr(data))
            self.assertNotIn(r, repr(data))
            self.assertNotIn("PRIVATE_LEFT", repr(data))
            self.assertNotIn("PRIVATE_RIGHT", repr(data))
        audit = (self.root / "history.jsonl").read_text()
        self.assertIn('"action":"compare"', audit)
        for private in (l, r, "PRIVATE_LEFT", "PRIVATE_RIGHT", "Old date", "New date"):
            self.assertNotIn(private, audit)
        with (patch.object(history_cli, "history_path",
                           return_value=self.root / "history.jsonl"),
              patch.object(sys, "argv",
                           ["history_cli.py", "--kind", "page", "--json"])):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(history_cli.main(), 0)
            self.assertIn('"action": "compare"', output.getvalue())
            self.assertNotIn(l, output.getvalue())

    async def test_same_handle_cannot_impersonate_second_snapshot(self):
        left, _right = self.allowed_pair()
        with self.assertRaisesRegex(ValueError, "distinct temporary"):
            bridge.web_compare_documents(left, left)
        async with Client(bridge.mcp) as client:
            result = await client.call_tool("web_compare_documents", {
                "left_document_id": left, "right_document_id": left})
            self.assertTrue(result.is_error)
            self.assertNotIn("Old date", str(result))
            self.assertNotIn(left, str(result))

    async def test_single_expired_handle_and_revoked_domain_do_not_leak(self):
        left, right = self.allowed_pair()
        response = bridge.web_compare_documents(left, "Z" * 32)
        self.assertFalse(response["success"])
        self.assertEqual(response["status"], "expired_or_unknown")
        self.assertNotIn("Source a", repr(response))
        domain_policy.decide("right.example.com", "deny", scope="exact")
        denied = bridge.web_compare_documents(left, right)
        self.assertEqual(denied["status"], "blocked")
        self.assertNotIn("Old date", repr(denied))
        self.assertNotIn("New date", repr(denied))

    async def test_cross_host_original_permission_and_invalid_once_proof(self):
        left, right = self.allowed_pair()
        domain_policy.decide("source.example.com", "deny", scope="exact")
        self.assertEqual(bridge.web_compare_documents(left, right)["status"],
                         "blocked")
        domain_policy.decide("source.example.com", "allow", scope="exact")
        token, _ = pages.save(
            "https://left.example.com/p", "Private proof", {},
            source_url="https://left.example.com/p",
            once_proof=("left.example.com", "f" * 32),
            allow_short=True)
        failure = bridge.web_compare_documents(token, right)
        self.assertEqual(failure["status"], "blocked")
        self.assertNotIn("Private proof", repr(failure))

    async def test_policy_epoch_change_mid_diff_blocks_release(self):
        left, right = self.allowed_pair()
        original = diff.compare
        def changes_policy(*args, **kwargs):
            domain_policy.decide("right.example.com", "deny", scope="exact")
            return original(*args, **kwargs)
        with patch.object(bridge.document_comparison, "compare",
                          side_effect=changes_policy):
            result = bridge.web_compare_documents(left, right)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("Old date", repr(result))

    async def test_mid_diff_eviction_blocks_release(self):
        left, right = self.allowed_pair()
        original = diff.compare
        def evict(*args, **kwargs):
            with pages._LOCK:
                pages._CACHE.pop(right)
            return original(*args, **kwargs)
        with patch.object(bridge.document_comparison, "compare", side_effect=evict):
            result = bridge.web_compare_documents(left, right)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("Old date", repr(result))

    async def test_overlimit_does_not_leak_source_content(self):
        left, right = self.allowed_pair("X" * (diff.MAX_CHARS + 1), "safe")
        result = bridge.web_compare_documents(left, right)
        self.assertEqual(result["status"], "comparison_limit")
        self.assertNotIn("X" * 500, str(result))

    async def test_full_output_cap_handles_unicode_heavy_source_urls(self):
        for host in ("left.example.com", "right.example.com"):
            domain_policy.decide(host, "allow", scope="exact")
        long_left = "https://left.example.com/" + "ž" * 485
        long_right = "https://right.example.com/" + "č" * 485
        left, _ = pages.save(long_left, "identical\n", {}, allow_short=True)
        right, _ = pages.save(long_right, "identical\n", {}, allow_short=True)
        result = bridge.web_compare_documents(left, right)
        self.assertTrue(result["success"])
        self.assertTrue(result["text_equal"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode()),
                             diff.MAX_OUTPUT_BYTES)
        self.assertFalse(result["left_source_identity_available"])
        self.assertFalse(result["right_source_identity_available"])
        self.assertEqual(result["left_source_url"], "")
        self.assertEqual(result["right_final_url"], "")
        changed_left, _ = pages.save(long_left, "old\n", {}, allow_short=True)
        changed_right, _ = pages.save(long_right, "new\n", {}, allow_short=True)
        changed = bridge.web_compare_documents(changed_left, changed_right)
        self.assertTrue(changed["success"])
        self.assertFalse(changed["text_equal"])
        self.assertEqual(len(changed["changes"]), 1)
        self.assertFalse(changed["changes_truncated"])
        self.assertLessEqual(len(json.dumps(changed, ensure_ascii=True).encode()),
                             diff.MAX_OUTPUT_BYTES)

    async def test_no_fetch_or_approval_on_compare(self):
        left, right = self.allowed_pair()
        with (patch.object(bridge, "_run_fetch", side_effect=AssertionError("Crawl")),
              patch.object(bridge.domain_policy, "evaluate_fetch_url",
                           side_effect=AssertionError("New permission request"))):
            result = bridge.web_compare_documents(left, right)
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
