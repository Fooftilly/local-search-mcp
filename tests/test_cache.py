"""Isolated process-only cache, single-flight, freshness and policy tests."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

import request_cache as cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        cache.reset_for_test()

    def tearDown(self):
        cache.reset_for_test()

    def test_cache_hits_fresh_bypass_and_expiration(self):
        calls = []
        now = [1000.0]
        def load():
            calls.append(len(calls))
            return [{"title": str(len(calls))}]
        with patch.object(cache.time, "monotonic", side_effect=lambda: now[0]):
            self.assertEqual(cache.run("search", ("search", "q"), load, ttl=15), [{"title": "1"}])
            self.assertEqual(cache.run("search", ("search", "q"), load, ttl=15), [{"title": "1"}])
            self.assertEqual(cache.run("search", ("search", "q"), load, ttl=15, fresh=True),
                             [{"title": "2"}])
            self.assertEqual(cache.run("search", ("search", "q"), load, ttl=15), [{"title": "2"}])
            now[0] = 1015.0
            self.assertEqual(cache.run("search", ("search", "q"), load, ttl=15), [{"title": "3"}])
        self.assertEqual(len(calls), 3)

    def test_concurrent_requests_share_one_loader(self):
        started, finish = Event(), Event()
        count = []
        def load():
            count.append(True)
            started.set()
            self.assertTrue(finish.wait(5))
            return {"success": True, "markdown": "safe"}
        with ThreadPoolExecutor(max_workers=8) as pool:
            first = pool.submit(cache.run, "fetch", ("fetch", "page"), load, ttl=90)
            self.assertTrue(started.wait(5))
            followers = [
                pool.submit(cache.run, "fetch", ("fetch", "page"), load, ttl=90)
                for _ in range(7)
            ]
            finish.set()
            results = [job.result(timeout=5) for job in [first, *followers]]
        self.assertEqual(len(count), 1)
        self.assertTrue(all(r["markdown"] == "safe" for r in results))
        results[0]["markdown"] = "mutated"
        self.assertEqual(cache.run("fetch", ("fetch", "page"), load, ttl=90)["markdown"],
                         "safe")

    def test_fresh_request_replaces_stale_cache_and_prevents_late_overwrite(self):
        key = ("search", "long")
        started, finish = Event(), Event()
        def old_loader():
            started.set()
            self.assertTrue(finish.wait(5))
            return [{"title": "old"}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(cache.run, "search", key, old_loader, ttl=90)
            self.assertTrue(started.wait(5))
            fresh = cache.run("search", key, lambda: [{"title": "new"}],
                              ttl=90, fresh=True)
            finish.set()
            self.assertEqual(old.result(timeout=5), [{"title": "old"}])
        self.assertEqual(fresh, [{"title": "new"}])
        self.assertEqual(cache.run("search", key, lambda: [{"title": "unexpected"}], ttl=90),
                         [{"title": "new"}])

    def test_failure_does_not_poison_key(self):
        attempts = []
        def load():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("temporary")
            return {"ok": True}
        with self.assertRaisesRegex(RuntimeError, "temporary"):
            cache.run("search", ("search", "q"), load, ttl=60)
        self.assertEqual(cache.run("search", ("search", "q"), load, ttl=60), {"ok": True})
        self.assertEqual(len(attempts), 2)

    def test_strict_entry_count_total_bytes_and_oversized_skip(self):
        with patch.object(cache, "MAX_ITEMS", 2):
            for i in range(4):
                cache.run("search", ("search", str(i)), lambda i=i: {"value": str(i)}, ttl=90)
        self.assertEqual(len(cache._CACHE), 2)
        self.assertNotIn(("search", "0"), cache._CACHE)
        with patch.object(cache, "MAX_ENTRY_BYTES", 10):
            cache.run("search", ("search", "large"), lambda: {"value": "x" * 20}, ttl=90)
            self.assertNotIn(("search", "large"), cache._CACHE)
        with patch.object(cache, "MAX_BYTES", 90):
            cache.run("search", ("search", "a"), lambda: {"value": "a" * 60}, ttl=90)
            cache.run("search", ("search", "b"), lambda: {"value": "b" * 60}, ttl=90)
            self.assertLessEqual(sum(x[1] for x in cache._CACHE.values()), 90)

    def test_fetch_urls_with_query_credentials_or_distinct_paths(self):
        self.assertIsNone(cache.fetch_key("https://example.com/page?token=secret", "raw"))
        self.assertIsNone(cache.fetch_key("https://user:secret@example.com/page", "raw"))
        self.assertIsNone(cache.fetch_key("file:///etc/passwd", "raw"))
        a = cache.fetch_key("HTTPS://EXAMPLE.COM/a#top", "raw")
        b = cache.fetch_key("https://example.com/a#different", "raw")
        self.assertEqual(a, b)
        self.assertNotEqual(a, cache.fetch_key("https://example.com/b", "raw"))
        self.assertNotEqual(a, cache.fetch_key("https://example.com/a", "fit"))

    def test_policy_change_invalidates_fetch_and_prevents_inflight_repopulation(self):
        key = ("fetch", "long")
        cache.run("fetch", key, lambda: {"success": True}, ttl=90)
        cache.run("search", ("search", "q"), lambda: [1], ttl=90)
        epoch = cache.policy_epoch()
        cache.invalidate_fetch()
        self.assertEqual(cache.policy_epoch(), epoch + 1)
        self.assertNotIn(key, cache._CACHE)
        self.assertIn(("search", "q"), cache._CACHE)
        def load():
            cache.invalidate_fetch()
            return {"success": True}
        cache.run("fetch", key, load, ttl=90)
        self.assertNotIn(key, cache._CACHE)


if __name__ == "__main__":
    unittest.main()
