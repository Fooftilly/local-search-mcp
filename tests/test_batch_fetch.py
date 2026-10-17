"""Bounded batch scheduling, isolation, cancellation and privacy regressions."""
from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Barrier, Event, Lock
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from batch_fetch import MAX_URLS, fetch_many, validate_batch
import document_pages
import request_cache


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

    def test_policy_revocation_mid_batch_redacts_earlier_successes(self):
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        early_done, slow_started, release_slow = Event(), Event(), Event()
        urls = ["https://example.com/early", "https://example.com/slow"]
        def fetch(url, mode, fresh):
            if url.endswith("/early"):
                early_done.set()
                return {"success": True, "url": url,
                        "markdown": "REVOKED_CONTENT_SENTINEL"}
            slow_started.set()
            self.assertTrue(release_slow.wait(5))
            return {"success": True, "url": url, "markdown": "LATE_CONTENT_SENTINEL"}

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fetch_many, urls, "raw", False, fetch)
            try:
                self.assertTrue(early_done.wait(5))
                self.assertTrue(slow_started.wait(5))
                request_cache.invalidate_fetch()
            finally:
                release_slow.set()
            response = future.result(timeout=5)

        self.assertEqual(response["count"], 2)
        self.assertEqual([x["result"]["status"] for x in response["results"]],
                         ["blocked", "blocked"])
        self.assertNotIn("REVOKED_CONTENT_SENTINEL", str(response))
        self.assertNotIn("LATE_CONTENT_SENTINEL", str(response))

    def test_policy_revocation_redacts_failed_crawl_content_and_metadata(self):
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        early_done, slow_started, release_slow = Event(), Event(), Event()
        urls = ["https://example.com/failure", "https://example.com/slow"]

        def fetch(url, mode, fresh):
            if url.endswith("/failure"):
                early_done.set()
                return {"url": url, "success": False,
                        "status": "crawl_failed",
                        "markdown": "REVOKED_FAILED_MARKDOWN",
                        "metadata": {"title": "REVOKED_FAILED_TITLE"},
                        "document_id": "REVOKED_FAILED_HANDLE"}
            slow_started.set()
            self.assertTrue(release_slow.wait(5))
            return {"url": url, "success": True, "markdown": "LATE_MARKDOWN"}

        with ThreadPoolExecutor(max_workers=1) as executor:
            work = executor.submit(fetch_many, urls, "raw", False, fetch)
            try:
                self.assertTrue(early_done.wait(5))
                self.assertTrue(slow_started.wait(5))
                request_cache.invalidate_fetch()
            finally:
                release_slow.set()
            result = work.result(timeout=5)

        self.assertEqual([row["result"]["status"] for row in result["results"]],
                         ["blocked", "blocked"])
        for secret in ("REVOKED_FAILED_MARKDOWN", "REVOKED_FAILED_TITLE",
                       "REVOKED_FAILED_HANDLE", "LATE_MARKDOWN"):
            self.assertNotIn(secret, str(result))

    def test_late_policy_change_redacts_even_failed_results(self):
        import batch_fetch
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        urls = ["https://example.com/failure", "https://example.com/other"]
        audit_orig = batch_fetch._audit_duplicate

        def fetch(url, mode, fresh):
            return {"url": url, "success": False,
                    "status": "crawl_failed",
                    "markdown": "FAILED_CONTENT_SENTINEL"}

        def audit_then_revoke(url, result):
            request_cache.invalidate_fetch()
            return audit_orig(url, result)

        # The first epoch check passes, but a duplicate audit triggers a
        # revocation while final response rows are already being assembled.
        with patch.object(batch_fetch, "_audit_duplicate",
                          side_effect=audit_then_revoke):
            result = fetch_many([urls[0], urls[0], urls[1]],
                                "raw", False, fetch)
        self.assertTrue(all(row["result"]["status"] == "blocked"
                            for row in result["results"]))
        self.assertNotIn("FAILED_CONTENT_SENTINEL", str(result))

    def test_evicted_continuations_are_not_returned_as_usable_handles(self):
        # Eight eligible documents of about 1 MiB each exceed the existing
        # 3-million-character global store. Returning all eight handles would
        # silently give callers five already-evicted continuation capabilities.
        urls = [f"https://example.com/long-{i}" for i in range(8)]
        with patch.object(document_pages, "_CACHE", OrderedDict()):
            def fetch(url, mode, fresh):
                contents = "x" * document_pages.MAX_DOCUMENT_CHARS
                handle, pages = document_pages.save(url, contents, {})
                return {"url": url, "success": True, "markdown": contents[:24000],
                        "truncated": True, "document_id": handle,
                        "page": 1, "page_count": pages}
            response = fetch_many(urls, "raw", False, fetch)
            valid = [row["result"] for row in response["results"]
                     if row["result"]["success"]]
            expired = [row["result"] for row in response["results"]
                       if row["result"].get("status") == "continuation_evicted"]
            self.assertEqual(len(valid) + len(expired), 8)
            self.assertGreater(len(expired), 0)
            self.assertLessEqual(len(valid), 3)
            for result in valid:
                self.assertTrue(document_pages.read(result["document_id"], 2)["success"])
            for result in expired:
                self.assertNotIn("document_id", result)
                self.assertNotIn("markdown", result)

    def test_global_limit_across_two_simultaneous_batches(self):
        first_started, release = Event(), Event()
        lock = Lock()
        currently_running = [0]
        maximum_running = [0]
        started_count = [0]

        def slow_fetch(url, mode, fresh):
            with lock:
                currently_running[0] += 1
                started_count[0] += 1
                maximum_running[0] = max(maximum_running[0], currently_running[0])
                if started_count[0] == 3:
                    first_started.set()
            try:
                self.assertTrue(release.wait(5))
                return {"url": url, "success": True, "markdown": "safe"}
            finally:
                with lock:
                    currently_running[0] -= 1

        with ThreadPoolExecutor(max_workers=2) as caller:
            first = caller.submit(fetch_many,
                                  [f"https://example.com/a{i}" for i in range(5)],
                                  "raw", False, slow_fetch)
            second = caller.submit(fetch_many,
                                   [f"https://example.com/b{i}" for i in range(5)],
                                   "raw", False, slow_fetch)
            try:
                self.assertTrue(first_started.wait(5))
                with lock:
                    self.assertEqual(maximum_running[0], 3)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)["count"], 5)
            self.assertEqual(second.result(timeout=5)["count"], 5)
        self.assertEqual(started_count[0], 10)
        self.assertEqual(maximum_running[0], 3)

    def test_waiting_batch_gets_fair_turn_before_earlier_batch_next_url(self):
        import batch_fetch
        first_three = Event()
        allow_first = Event()
        allow_other_two = Event()
        second_started = Event()
        release_second = Event()
        gate = Lock()
        starts = []
        urls_a = [f"https://example.com/a{i}" for i in range(8)]
        urls_b = ["https://example.com/b0"]

        def fetch(url, mode, fresh):
            with gate:
                starts.append(url)
                if all(x in starts for x in urls_a[:3]):
                    first_three.set()
                if url == urls_b[0]:
                    second_started.set()
            if url == urls_a[0]:
                self.assertTrue(allow_first.wait(5))
            elif url in urls_a[1:3]:
                self.assertTrue(allow_other_two.wait(5))
            elif url == urls_b[0]:
                self.assertTrue(release_second.wait(5))
            return {"url": url, "success": True}

        with ThreadPoolExecutor(max_workers=2) as clients:
            first = clients.submit(fetch_many, urls_a, "raw", False, fetch)
            try:
                self.assertTrue(first_three.wait(5))
                second = clients.submit(fetch_many, urls_b, "raw", False, fetch)
                # Ensure the second batch has entered the FIFO waiting queue.
                for _ in range(100):
                    with batch_fetch._ACTIVE_BATCH_FETCHES._condition:
                        waiting = len(batch_fetch._ACTIVE_BATCH_FETCHES._waiting)
                    if waiting:
                        break
                    second_started.wait(0.01)
                self.assertGreater(waiting, 0)
                allow_first.set()
                self.assertTrue(second_started.wait(5))
                with gate:
                    self.assertNotIn(urls_a[3], starts)
            finally:
                allow_first.set()
                allow_other_two.set()
                release_second.set()
            self.assertEqual(first.result(timeout=5)["count"], 8)
            self.assertEqual(second.result(timeout=5)["count"], 1)
        with gate:
            self.assertLess(starts.index(urls_b[0]), starts.index(urls_a[3]))

    def test_cancellation_skips_pending_urls_without_starting_new_crawls(self):
        urls = [f"https://example.com/page-{i}" for i in range(8)]
        started, release, stopped = Event(), Event(), Event()
        count = [0]
        lock = Lock()
        calls = []
        def slow_fetch(url, mode, fresh):
            with lock:
                calls.append(url)
                count[0] += 1
                if count[0] == 3:
                    started.set()
            self.assertTrue(release.wait(5))
            return {"url": url, "success": True, "markdown": "safe"}
        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(fetch_many, urls, "raw", False,
                                   slow_fetch, cancelled=stopped)
            try:
                self.assertTrue(started.wait(5))
                stopped.set()
            finally:
                release.set()
            response = future.result(timeout=5)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sum(x["result"].get("status") == "cancelled"
                             for x in response["results"]), 5)
        self.assertEqual(response["count"], 8)

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
