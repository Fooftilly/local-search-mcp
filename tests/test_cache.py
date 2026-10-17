"""Isolated process-only cache, single-flight, freshness and policy tests."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import time
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
            entered = Barrier(8)
            def follow():
                entered.wait(timeout=5)
                return cache.run("fetch", ("fetch", "page"), load, ttl=90)
            followers = [pool.submit(follow) for _ in range(7)]
            entered.wait(timeout=5)  # All seven followers start while loader waits.
            time.sleep(0.04)  # Give the coalescing calls time to join the Future.
            self.assertEqual(len(count), 1)
            finish.set()
            results = [job.result(timeout=5) for job in [first, *followers]]
        self.assertEqual(len(count), 1)
        self.assertTrue(all(r["markdown"] == "safe" for r in results))
        self.assertEqual(len({id(r) for r in results}), len(results))
        results[0]["markdown"] = "mutated"
        self.assertTrue(all(r["markdown"] == "safe" for r in results[1:]))
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

    def test_fresh_does_not_interrupt_unrelated_inflight_cache(self):
        started, release = Event(), Event()
        calls = []
        def load_other():
            started.set()
            self.assertTrue(release.wait(5))
            calls.append("other")
            return [{"title": "other"}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(cache.run, "search", ("search", "other"),
                                 load_other, ttl=90)
            self.assertTrue(started.wait(5))
            cache.run("search", ("search", "refreshed"),
                      lambda: [{"title": "fresh"}], ttl=90, fresh=True)
            release.set()
            future.result(timeout=5)
        self.assertIn(("search", "other"), cache._CACHE)
        self.assertEqual(calls, ["other"])

    def test_fresh_preserves_old_cache_during_load_and_evicts_on_failure(self):
        key = ("search", "one")
        cache.run("search", key, lambda: [{"title": "before"}], ttl=90)
        started, release = Event(), Event()
        def fresh_loader():
            started.set()
            self.assertTrue(release.wait(5))
            return [{"title": "after"}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            refreshing = pool.submit(cache.run, "search", key, fresh_loader,
                                     ttl=90, fresh=True)
            self.assertTrue(started.wait(5))
            # A default request during refresh must not open another upstream call.
            existing = cache.run("search", key, lambda: self.fail("extra fetch"), ttl=90)
            self.assertEqual(existing, [{"title": "before"}])
            release.set()
            refreshing.result(timeout=5)
        self.assertEqual(cache.run("search", key, lambda: self.fail("extra fetch"),
                                   ttl=90), [{"title": "after"}])
        with self.assertRaises(RuntimeError):
            cache.run("search", key, lambda: (_ for _ in ()).throw(
                RuntimeError("offline")), ttl=90, fresh=True)
        self.assertNotIn(key, cache._CACHE)
        self.assertFalse(cache._REFRESHING)

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
        # urlsplit treats empty delimiters as empty components; these are
        # still distinct browser URLs and must not share the plain-page key.
        for suffix in ("?", "#", "?#", "?x=1", "#/user/1", "?x=1#frag"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(
                    cache.fetch_key("https://example.com/page" + suffix, "raw")
                )
        self.assertIsNone(cache.fetch_key("https://example.com/#/user/1", "raw"))
        self.assertIsNone(cache.fetch_key("https://example.com/#access_token=secret", "raw"))
        self.assertIsNone(cache.fetch_key("https://example.com/" + "path" * 700, "raw"))
        self.assertIsNone(cache.fetch_key("https://user:secret@example.com/page", "raw"))
        self.assertIsNone(cache.fetch_key("file:///etc/passwd", "raw"))
        a = cache.fetch_key("HTTPS://EXAMPLE.COM/a", "raw")
        b = cache.fetch_key("https://example.com/a", "raw")
        self.assertEqual(a, b)
        self.assertNotEqual(a, cache.fetch_key("https://example.com/b", "raw"))
        self.assertNotEqual(a, cache.fetch_key("https://example.com/a", "fit"))

    def test_policy_edit_during_fresh_fetch_preserves_newer_cached_entry(self):
        key = ("fetch", "raw", "https://example.com/page")
        started, release = Event(), Event()
        cache.run("fetch", key,
                  lambda: {"success": True, "markdown": "before edit"}, ttl=90)

        def old_refresh():
            started.set()
            self.assertTrue(release.wait(5))
            return {"success": True, "markdown": "old policy refresh"}

        with ThreadPoolExecutor(max_workers=2) as pool:
            stale = pool.submit(cache.run, "fetch", key, old_refresh,
                                ttl=90, fresh=True)
            try:
                self.assertTrue(started.wait(5))
                self.assertIn(key, cache._REFRESHING)
                cache.invalidate_fetch()
                self.assertNotIn(key, cache._REFRESHING)
                self.assertNotIn(key, cache._CACHE)
                post_edit = cache.run(
                    "fetch", key,
                    lambda: {"success": True, "markdown": "new policy content"},
                    ttl=90)
                self.assertEqual(post_edit["markdown"], "new policy content")
            finally:
                release.set()
            self.assertEqual(stale.result(timeout=5)["markdown"],
                             "old policy refresh")

        self.assertEqual(cache.run(
            "fetch", key, lambda: self.fail("new content was wrongly evicted"),
            ttl=90)["markdown"], "new policy content")

    def test_default_flight_started_during_fresh_cannot_overwrite_refresh(self):
        key = ("search", "same query")
        refreshing_started, release_refresh = Event(), Event()
        default_started, release_default = Event(), Event()

        def fresh_loader():
            refreshing_started.set()
            self.assertTrue(release_refresh.wait(5))
            return [{"title": "new refresh result"}]

        def default_loader():
            default_started.set()
            self.assertTrue(release_default.wait(5))
            return [{"title": "old default result"}]

        with ThreadPoolExecutor(max_workers=2) as pool:
            fresh = pool.submit(cache.run, "search", key, fresh_loader,
                                ttl=90, fresh=True)
            try:
                self.assertTrue(refreshing_started.wait(5))
                default = pool.submit(cache.run, "search", key,
                                      default_loader, ttl=90)
                self.assertTrue(default_started.wait(5))
                self.assertTrue(cache._INFLIGHT[key][2]["superseded"])
                release_refresh.set()
                self.assertEqual(fresh.result(timeout=5),
                                 [{"title": "new refresh result"}])
            finally:
                release_refresh.set()
                release_default.set()
            self.assertEqual(default.result(timeout=5),
                             [{"title": "old default result"}])

        self.assertEqual(cache.run(
            "search", key, lambda: self.fail("late flight overwrote refresh"),
            ttl=90), [{"title": "new refresh result"}])

    def test_post_revoke_caller_never_joins_older_inflight_crawl(self):
        old_started, release_old = Event(), Event()
        key = ("fetch", "raw", "https://example.com/path")
        calls = []
        def previous():
            calls.append("previous")
            old_started.set()
            self.assertTrue(release_old.wait(5))
            return {"success": True, "markdown": "revoked secret"}
        def current():
            calls.append("current")
            return {"success": True, "markdown": "after policy change"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(cache.run, "fetch", key, previous, ttl=90)
            self.assertTrue(old_started.wait(5))
            cache.invalidate_fetch()
            self.assertNotIn(key, cache._INFLIGHT)
            recent = pool.submit(cache.run, "fetch", key, current, ttl=90)
            self.assertEqual(recent.result(timeout=5)["markdown"],
                             "after policy change")
            release_old.set()
            self.assertEqual(old.result(timeout=5)["markdown"], "revoked secret")
        self.assertEqual(calls, ["previous", "current"])
        self.assertEqual(cache.run("fetch", key, lambda: {"markdown": "wrong"}, ttl=90)
                         ["markdown"], "after policy change")

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
