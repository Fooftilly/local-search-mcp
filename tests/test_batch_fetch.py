"""Bounded batch scheduling, isolation, cancellation and privacy regressions."""
from concurrent.futures import CancelledError
from threading import Barrier, Lock
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from batch_fetch import MAX_URLS, fetch_many, validate_batch


class BatchFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(self.tmp.name) / "history.jsonl"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_validation_rejects_oversized_batches_before_fetching(self):
        for urls in ([], ["https://example.com"] * (MAX_URLS + 1), "not-list",
                     ["https://example.com", None],
                     ["https://example.com/" + "a" * 2050],
                     ["x" * 1100] * 8):
            with self.subTest(urls=str(urls)[:45]), self.assertRaises(ValueError):
                fetch_many(urls, "raw", False,
                           lambda *args: self.fail("invalid input reached fetch"))
        for mode, fresh in (("unknown", False), ("raw", 1)):
            with self.assertRaises(ValueError):
                validate_batch(["https://example.com"], mode, fresh)

    def test_bounded_concurrency_and_input_order(self):
        urls = [f"https://example.com/{i}" for i in range(8)]
        barrier = Barrier(3)
        lock = Lock()
        active = [0]
        high_water = [0]
        observed = []
        def fetch(url, mode, fresh):
            with lock:
                active[0] += 1
                high_water[0] = max(high_water[0], active[0])
                observed.append(url)
            try:
                if int(url.rsplit("/", 1)[-1]) < 3:
                    barrier.wait(timeout=5)
                return {"success": True, "url": url, "markdown": "test"}
            finally:
                with lock:
                    active[0] -= 1
        result = fetch_many(urls, "raw", False, fetch)
        self.assertEqual(high_water[0], 3)
        self.assertEqual(len(observed), 8)
        self.assertEqual([r["requested_url"] for r in result["results"]], urls)
        self.assertEqual([r["index"] for r in result["results"]], list(range(8)))

    def test_repeated_url_is_deduplicated_and_has_separate_audit_event(self):
        url = "https://example.com/public?credential=PRIVATE_AUDIT_SENTINEL"
        urls = [url, url, "https://example.com/other", url]
        calls = []
        def fetch(requested, mode, fresh):
            calls.append((requested, mode, fresh))
            return {"url": requested, "success": True, "markdown": "safe page"}
        out = fetch_many(urls, "fit", True, fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["count"], 4)
        self.assertEqual([row["index"] for row in out["results"]], list(range(4)))
        self.assertEqual(out["results"][0]["result"], out["results"][1]["result"])
        self.assertIsNot(out["results"][0]["result"], out["results"][1]["result"])
        out["results"][0]["result"]["markdown"] = "mutated"
        self.assertEqual(out["results"][1]["result"]["markdown"], "safe page")
        # The unique URLs are audited by the real decorated web_fetch; the
        # two duplicate inputs must be accounted for without a second crawl.
        audit = (Path(self.tmp.name) / "history.jsonl").read_text()
        events = [json.loads(line) for line in audit.splitlines()]
        self.assertEqual(len(events), 2)
        self.assertEqual([entry["kind"] for entry in events], ["fetch", "fetch"])
        self.assertTrue(all(entry["url"] == "https://example.com/public"
                            for entry in events))
        self.assertNotIn("PRIVATE_AUDIT_SENTINEL", audit)
        self.assertNotIn("safe page", audit)

    def test_per_url_failures_and_cancellations_do_not_abort_other_urls(self):
        urls = ["https://example.com/ok", "https://example.com/timeout",
                "https://example.com/cancelled", "https://example.com/denied"]
        def fetch(url, mode, fresh):
            if url.endswith("/timeout"):
                raise TimeoutError("internal token PRIVATE_SECRET")
            if url.endswith("/cancelled"):
                raise CancelledError()
            if url.endswith("/denied"):
                return {"success": False, "status": "denied", "url": url}
            return {"success": True, "markdown": "success", "url": url}
        out = fetch_many(urls, "raw", False, fetch)
        r = [x["result"] for x in out["results"]]
        self.assertTrue(r[0]["success"])
        self.assertEqual(r[1]["status"], "fetch_error")
        self.assertEqual(r[1]["error_type"], "TimeoutError")
        self.assertEqual(r[2]["status"], "cancelled")
        self.assertEqual(r[3]["status"], "denied")
        self.assertNotIn("PRIVATE_SECRET", str(out))


if __name__ == "__main__":
    unittest.main()
