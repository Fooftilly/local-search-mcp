"""Read-only MCP and process-cache operational metrics, privacy and safety."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastmcp import Client
from fastmcp.exceptions import ToolError

import bridge
import history_index
from history_log import append_history
import request_cache


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tempdir = Path(temp.name)
        environment = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(temp.name) / "events.jsonl"),
            "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "30",
        })
        environment.start()
        self.addCleanup(environment.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)

    async def test_mcp_metric_tool_only_exposes_aggregates(self):
        now = datetime.now(timezone.utc).isoformat()
        for kind, status, url, query in (
            ("fetch", "success", "https://safe.example.org/SECRET_PATH?token=HIDDEN", ""),
            ("fetch", "denied", "https://blocked.example.org/password=PRIVATE", ""),
            ("fetch", "pending", "https://needs-approval.example.org/SECRET", ""),
            ("fetch", "blocked", "https://unsafe.example.org/SECRET", ""),
            ("fetch", "error", "https://errors.example.org/SECRET", ""),
            ("fetch", "http_error", "https://missing.example.org/SECRET", ""),
            ("search", "success", "", "PASSWORD_QUERY_PRIVATE"),
        ):
            append_history({
                "time_utc": now, "kind": kind, "status": status,
                "url": url, "query": query, "duration_ms": 20,
                "markdown": "PRIVATE_DOCUMENT",
            })
        key = ("search", "public-metrics-example")
        self.assertEqual(request_cache.run("search", key, lambda: {"ok": True},
                                           ttl=60), {"ok": True})
        self.assertEqual(request_cache.run("search", key, lambda: self.fail("cache hit"),
                                           ttl=60), {"ok": True})
        async with Client(bridge.mcp) as client:
            names = {x.name for x in await client.list_tools()}
            self.assertIn("web_metrics", names)
            metrics_tool = next(t for t in await client.list_tools()
                                if t.name == "web_metrics")
            self.assertEqual(set(metrics_tool.inputSchema["properties"]
                                 ["hours"]["enum"]), {1, 24, 168})
            result = await client.call_tool("web_metrics", {"hours": 24})
            self.assertFalse(result.is_error)
            response = result.data
            self.assertTrue(response["success"])
            with self.assertRaises(ToolError):
                await client.call_tool("web_metrics", {"hours": 0})
        counts = {(x["tool"], x["status"]): x["count"]
                  for x in response["history"]["outcomes"]}
        self.assertEqual(counts[("fetch", "success")], 1)
        for outcome in ("denied", "pending", "blocked", "error", "http_error"):
            self.assertEqual(counts[("fetch", outcome)], 1)
        self.assertEqual(counts[("search", "success")], 1)
        self.assertEqual(response["history"]["total_events"], 7)
        self.assertEqual(response["cache"]["scope"], "process_lifetime")
        self.assertEqual(response["cache"]["kinds"]["search"]["hits"], 1)
        self.assertEqual(response["cache"]["kinds"]["search"]["misses"], 1)
        self.assertEqual(response["cache"]["kinds"]["search"]["stores"], 1)
        for secret in ("SECRET_PATH", "HIDDEN", "PASSWORD_QUERY_PRIVATE",
                       "PRIVATE_DOCUMENT", "password=PRIVATE", "safe.example.org"):
            self.assertNotIn(secret, repr(response))

    async def test_untrusted_historical_status_is_not_exposed_to_mcp(self):
        append_history({
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "fetch", "status": "Bearer_PRIVATE_STATUS_VALUE",
            "duration_ms": 1, "url": "https://example.org/secret",
        })
        stats = bridge.web_metrics()
        self.assertTrue(stats["success"])
        self.assertEqual(stats["history"]["outcomes"][0]["status"], "other")
        self.assertNotIn("PRIVATE_STATUS_VALUE", repr(stats))

    async def test_time_windows_retain_only_recent_events(self):
        now = datetime.now(timezone.utc)
        append_history({
            "time_utc": (now - timedelta(hours=3)).isoformat(),
            "kind": "fetch", "status": "blocked", "duration_ms": 8,
            "url": "https://example.org/private",
        })
        append_history({
            "time_utc": now.isoformat(),
            "kind": "fetch", "status": "success", "duration_ms": 4,
            "url": "https://example.org/private",
        })
        self.assertEqual(history_index.operational_metrics(1)["total_events"], 1)
        self.assertEqual(history_index.operational_metrics(24)["total_events"], 2)
        self.assertEqual(history_index.operational_metrics(168)["total_events"], 2)
        for n in (-1, 0, True, 2, 1000, "24"):
            with self.subTest(n=n), self.assertRaises(ValueError):
                history_index.operational_metrics(n)

    async def test_cache_counters_track_hits_misses_refreshes_uncacheable_and_invalidation(self):
        key = ("fetch", "raw", "https://example.org/public")
        loads = []
        def load():
            loads.append(True)
            return {"success": True, "markdown": "safe"}
        cache = request_cache
        cache.run("fetch", key, load, ttl=60)
        cache.run("fetch", key, lambda: self.fail("unexpected miss"), ttl=60)
        cache.run("fetch", key, load, ttl=60, fresh=True)
        cache.run("fetch", None, load, ttl=60)
        first = cache.metrics_snapshot()["kinds"]["fetch"]
        self.assertEqual(first["hits"], 1)
        self.assertEqual(first["misses"], 1)
        self.assertEqual(first["refreshes"], 1)
        self.assertEqual(first["uncacheable"], 1)
        self.assertEqual(first["stores"], 2)
        self.assertEqual(first["entries"], 1)
        self.assertEqual(len(loads), 3)
        cache.invalidate_fetch()
        second = cache.metrics_snapshot()["kinds"]["fetch"]
        self.assertEqual(second["policy_invalidations"], 1)
        self.assertEqual(second["evictions"], 1)
        self.assertEqual(second["entries"], 0)
        self.assertNotIn("https://example.org", repr(cache.metrics_snapshot()))
        cache.reset_for_test()
        self.assertEqual(cache.metrics_snapshot()["kinds"]["fetch"]["hits"], 0)

    async def test_failed_or_unstorable_refresh_counts_evictions_exactly_once(self):
        key = ("search", "opaque-private-cache-key")
        original = {"value": "cached"}
        request_cache.run("search", key, lambda: original, ttl=60)

        def raises():
            raise RuntimeError("upstream is offline")

        with self.assertRaisesRegex(RuntimeError, "upstream is offline"):
            request_cache.run("search", key, raises, ttl=60, fresh=True)
        stats = request_cache.metrics_snapshot()["kinds"]["search"]
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(stats["evictions"], 1)

        request_cache.run("search", key, lambda: original, ttl=60)
        # Failed storage also removes the earlier entry without persisting
        # anything returned by a declined refresh.
        result = request_cache.run(
            "search", key, lambda: {"value": "not cacheable"},
            ttl=60, fresh=True, should_store=lambda _: False)
        self.assertEqual(result, {"value": "not cacheable"})
        stats = request_cache.metrics_snapshot()["kinds"]["search"]
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(stats["evictions"], 2)

        # A second failed refresh on an already-empty key cannot increment
        # the count again.
        with self.assertRaises(RuntimeError):
            request_cache.run("search", key, raises, ttl=60, fresh=True)
        self.assertEqual(
            request_cache.metrics_snapshot()["kinds"]["search"]["evictions"], 2)
        self.assertNotIn("opaque-private-cache-key",
                         repr(request_cache.metrics_snapshot()))

    async def test_invalid_history_index_is_degraded_without_leaking_file_paths(self):
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "0"}):
            response = bridge.web_metrics()
        self.assertFalse(response["success"])
        self.assertEqual(response["status"], "history_unavailable")
        self.assertNotIn(str(self.tempdir), repr(response))
        self.assertNotIn(str(self.tempdir / "events.jsonl"), repr(response))
        self.assertNotIn(str(self.tempdir / "events.jsonl.sqlite3"), repr(response))
        self.assertIn("cache", response)


if __name__ == "__main__":
    unittest.main()
