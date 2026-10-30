"""Indexed history privacy, migration, rotation, retention, and concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import history_index
from history_log import append_history, history_path


def event(kind="fetch", status="success", *, time_utc=None, **extra):
    return {"time_utc": time_utc or datetime.now(timezone.utc).isoformat(),
            "kind": kind, "status": status, "duration_ms": 12.5, **extra}


class HistoryIndexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": tmp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(tmp.name) / "history.jsonl"),
            "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "30",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_history_offsets_reveal_only_the_selected_original_events(self):
        when = datetime.now(timezone.utc).isoformat()
        for i in range(64):
            if i % 3 == 0:
                append_history(event("search", backend="SearXNG",
                                     time_utc=when, query=f"private-query-{i:02d}"))
            elif i % 3 == 1:
                append_history(event("fetch", backend="Crawl4AI",
                                     time_utc=when,
                                     url=f"https://docs.example.org/page/{i:02d}"
                                         "?secret=DO_NOT_SHOW#frag"))
            else:
                append_history(event("page", backend="Crawl4AI",
                                     time_utc=when, document_id="private-handle"))
        first = history_index.search_history(limit=25)
        second = history_index.search_history(limit=25, offset=25)
        third = history_index.search_history(limit=25, offset=50)
        self.assertEqual((len(first), len(second), len(third)), (25, 25, 14))
        self.assertEqual(first[0]["kind"], "search")
        self.assertEqual(third[-1]["kind"], "search")
        for rows in (first, second, third):
            self.assertTrue(all("url" not in item and "query" not in item
                                and "byte_offset" not in item for item in rows))
        revealed = history_index.search_history(
            limit=25, offset=25, reveal_queries=True)
        self.assertEqual(len(revealed), 25)
        self.assertTrue(all(not (item["kind"] == "page" and
                                 ("url" in item or "query" in item))
                            for item in revealed))
        fetch_urls = [item["url"] for item in revealed if item["kind"] == "fetch"]
        self.assertTrue(fetch_urls)
        self.assertTrue(all(url.startswith("https://docs.example.org/page/")
                            and "secret=" not in url for url in fetch_urls))
        for item in revealed:
            self.assertNotIn("generation_id", item)
            self.assertNotIn("byte_offset", item)
        self.assertEqual(
            history_index.search_history(limit=25, offset=25), second)
        indexed = history_index.index_path().read_bytes()
        for secret in (b"private-query", b"/page/", b"DO_NOT_SHOW",
                       b"private-handle"):
            self.assertNotIn(secret, indexed)
        for invalid in (-1, 100_000, True, 1.5, "25"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                history_index.search_history(offset=invalid)

    def test_reveal_fetch_page_urls_only_from_matching_original_jsonl(self):
        stamp = datetime.now(timezone.utc).isoformat()
        target = "https://docs.example.org/articles/guide/part-2"
        append_history(event("fetch", "success", time_utc=stamp,
                             duration_ms=3, url=target,
                             backend="Crawl4AI"))
        append_history(event("fetch", "success", time_utc=stamp,
                             duration_ms=4,
                             url="https://docs.example.org/second-page?token=private#frag",
                             backend="Crawl4AI"))
        append_history(event("search", "success", time_utc=stamp,
                             query="hello search", backend="SearXNG"))
        append_history(event("page", "success", time_utc=stamp))
        ordinary = history_index.search_history(limit=10)
        self.assertTrue(all("url" not in row for row in ordinary))
        revealed = history_index.search_history(limit=10, reveal_queries=True)
        fetches = [row for row in revealed if row["kind"] == "fetch"]
        self.assertEqual([x["url"] for x in fetches],
                         ["https://docs.example.org/second-page",
                          "https://docs.example.org/articles/guide/part-2"])
        self.assertEqual([r["query"] for r in revealed if r["kind"] == "search"],
                         ["hello search"])
        self.assertNotIn("url", revealed[0])
        self.assertEqual(history_index.search_history(limit=10), ordinary)
        index_bytes = history_index.index_path().read_bytes()
        for raw in (b"/articles/guide/part-2", b"/second-page", b"token=private"):
            self.assertNotIn(raw, index_bytes)

    def test_reveal_fetch_unsafe_legacy_url_is_sanitized_or_unavailable(self):
        append_history(event("fetch", url="https://user:password@docs.example.org/private"
                             "?key=TOPSECRET#frag"))
        append_history(event("fetch", url="javascript:alert(1)"))
        append_history(event("fetch", url="https://docs.example.org/<img src=x>"))
        selected = history_index.search_history(reveal_queries=True)
        self.assertEqual(selected[0]["url"], "https://docs.example.org/<img src=x>")
        self.assertIsNone(selected[1]["url"])
        self.assertEqual(selected[2]["url"], "https://docs.example.org/private")
        self.assertNotIn("password", repr(selected))
        self.assertNotIn("TOPSECRET", repr(selected))

    def test_reveal_fetch_source_rotation_never_matches_another_event(self):
        append_history(event("fetch", backend="Crawl4AI",
                             url="https://old.example.org/a"))
        original = history_path()
        rotated = original.with_name(original.name + ".1")
        self.assertEqual(history_index.search_history(reveal_queries=True)[0]["url"],
                         "https://old.example.org/a")
        original.rename(rotated)
        append_history(event("fetch", backend="Crawl4AI",
                             url="https://new.example.org/b"))
        self.assertEqual(
            [r["url"] for r in history_index.search_history(reveal_queries=True)],
            ["https://new.example.org/b", "https://old.example.org/a"])
        rotated.unlink()
        remaining = history_index.search_history(reveal_queries=True)
        self.assertEqual(remaining[0]["url"], "https://new.example.org/b")
        self.assertIsNone(remaining[1]["url"])

    def test_reveal_uses_exact_source_coordinates_without_persisting_queries(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        for original in ("same-time-first", "same-time-second", "unicode-café-猫"):
            append_history(event("search", "success", time_utc=timestamp,
                                 backend="SearXNG", query=original))
        append_history(event("fetch", "success", time_utc=timestamp,
                             url="https://docs.example.org/private?q=secret"))
        normal = history_index.search_history(limit=10)
        self.assertTrue(all("query" not in row for row in normal))
        revealed = history_index.search_history(limit=10, reveal_queries=True)
        self.assertEqual([row["query"] for row in revealed if row["kind"] == "search"],
                         ["unicode-café-猫", "same-time-second", "same-time-first"])
        self.assertTrue(all("query" not in row for row in revealed
                            if row["kind"] != "search"))
        self.assertEqual(history_index.search_history(limit=10), normal)
        contents = history_index.index_path().read_bytes()
        for phrase in (b"same-time-first", b"same-time-second",
                       "unicode-café-猫".encode("utf-8")):
            self.assertNotIn(phrase, contents)
        with self.assertRaises(ValueError):
            history_index.search_history(reveal_queries=1)

    def test_reveal_rotated_legacy_source_and_missing_file_fails_closed(self):
        append_history(event("search", query="original-secret", backend="SearXNG"))
        self.assertEqual(
            history_index.search_history(reveal_queries=True)[0]["query"],
            "original-secret")
        original = history_path()
        rotated = original.with_name(original.name + ".1")
        original.rename(rotated)
        append_history(event("search", query="new-secret", backend="SearXNG"))
        current = history_index.search_history(reveal_queries=True)
        self.assertEqual([r["query"] for r in current], ["new-secret", "original-secret"])
        # Replacing a live source with a different event can never expose its
        # text merely because a timestamp/status happens to match.
        rotated.unlink()
        result = history_index.search_history(reveal_queries=True)
        self.assertEqual(result[0]["query"], "new-secret")
        self.assertIsNone(result[1]["query"])

    def test_tampered_original_line_never_reveals_different_query(self):
        append_history(event("search", backend="SearXNG", query="original"))
        self.assertEqual(
            history_index.search_history(reveal_queries=True)[0]["query"],
            "original")
        file = history_path()
        data = file.read_bytes().replace(b"original", b"impostor")
        file.write_bytes(data)  # same length, reused inode and byte offset
        result = history_index.search_history(reveal_queries=True)
        self.assertEqual([row["query"] for row in result],
                         ["impostor", None])

    def test_reveal_concurrent_appends_are_bounded_and_consistent(self):
        def writer():
            for n in range(35):
                append_history(event("search", backend="SearXNG",
                                     query=f"concurrent-{n:02}"))
        def reader():
            for _ in range(8):
                result = history_index.search_history(reveal_queries=True)
                for row in result:
                    self.assertTrue(row["query"] is None or
                                    row["query"].startswith("concurrent-"))
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(writer), pool.submit(reader), pool.submit(reader)]
            for future in futures:
                future.result(timeout=25)
        self.assertEqual(history_index.search_history(reveal_queries=True)[0]["query"],
                         "concurrent-34")

    def test_legacy_jsonl_migrates_idempotently_and_redacts_sensitive_fields(self):
        append_history(event(url="https://user:password@docs.example.org/INVITE_SECRET"
                                 "?token=QUERY_SECRET", query="SENSITIVE_QUERY",
                             markdown="SENSITIVE_BODY"))
        append_history(event("search", "blocked", query="TOP_SECRET_SEARCH"))
        once = history_index.search_history()
        twice = history_index.search_history()
        self.assertEqual(once, twice)
        self.assertEqual(len(once), 2)
        fetch = history_index.search_history(kind="fetch")[0]
        self.assertEqual(fetch["domain"], "docs.example.org")
        self.assertEqual(fetch["status"], "success")
        self.assertEqual(fetch["duration_ms"], 12.5)
        self.assertEqual(history_index.summarize_history()[0]["count"], 1)
        stored = history_index.index_path().read_bytes()
        for secret in (b"INVITE_SECRET", b"QUERY_SECRET", b"TOP_SECRET_SEARCH",
                       b"SENSITIVE_QUERY", b"SENSITIVE_BODY", b"password"):
            self.assertNotIn(secret, stored)
        self.assertEqual(history_index.index_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(history_index.index_path().parent.stat().st_mode & 0o777, 0o700)

    def test_domain_status_and_tool_filters(self):
        append_history(event("fetch", "denied", url="https://docs.example.org/secret"))
        append_history(event("fetch", "success", url="https://other.example.org/"))
        append_history(event("search", "success", query="secret query"))
        result = history_index.search_history(
            kind="fetch", status="denied", domain="docs.example.org")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["domain"], "docs.example.org")
        self.assertEqual(len(history_index.search_history(kind="search")), 1)
        self.assertEqual(len(history_index.search_history(domain="not.example.org")), 0)
        for filters in ({"kind": "delete"}, {"status": "X" * 100},
                        {"domain": "x' OR 1=1 --"}, {"limit": 101},
                        {"limit": 0}, {"limit": True}):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                history_index.search_history(**filters)

    def test_rolling_time_filters_match_rows_and_summary(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=3)).isoformat()
        append_history(event("fetch", "success", time_utc=old,
                             url="https://docs.example.org/old"))
        append_history(event("fetch", "blocked", time_utc=now.isoformat(),
                             url="https://docs.example.org/new"))
        append_history(event("search", "success", time_utc=now.isoformat(),
                             query="private search"))
        hour = history_index.search_history(hours=1)
        self.assertEqual(len(hour), 2)
        self.assertEqual(len(history_index.search_history(hours=24)), 3)
        self.assertEqual(len(history_index.search_history(hours=0)), 3)
        self.assertEqual(
            [row["status"] for row in history_index.search_history(
                hours=1, kind="fetch", domain="docs.example.org")],
            ["blocked"])
        summary = history_index.summarize_history(
            hours=1, kind="fetch", domain="docs.example.org")
        self.assertEqual([(row["kind"], row["status"], row["count"])
                          for row in summary], [("fetch", "blocked", 1)])
        all_summary = history_index.summarize_history(hours=24)
        self.assertEqual(sum(row["count"] for row in all_summary), 3)
        for invalid in (-1, 2, True, "24", 10000):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    history_index.search_history(hours=invalid)
                with self.assertRaises(ValueError):
                    history_index.summarize_history(hours=invalid)

    def test_partial_and_malformed_legacy_lines_never_lose_following_event(self):
        append_history(event("fetch", "success", url="https://one.example.org/"))
        path = history_path()
        with path.open("ab") as file:
            file.write(b'not-json\n')
            line = (json.dumps(event("fetch", "blocked",
                                     url="https://two.example.org/")) + "\n").encode()
            file.write(line[:15])
        self.assertEqual(len(history_index.search_history()), 1)
        with path.open("ab") as file:
            file.write(line[15:])
        records = history_index.search_history()
        self.assertEqual(len(records), 2)
        self.assertEqual(history_index.search_history(status="blocked")[0]["domain"],
                         "two.example.org")

    def test_oversized_line_is_skipped_and_following_events_are_indexed(self):
        # An oversized complete line must not poison the incremental offset.
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps(event(url="https://after.example.org/")).encode()
        with path.open("wb") as stream:
            stream.write(b'{"extra":"' + b'X' * (history_index.MAX_EVENT_BYTES + 100)
                         + b'"}\n')
            stream.write(good + b"\n")
        self.assertEqual(history_index.search_history()[0]["domain"],
                         "after.example.org")
        self.assertEqual(len(history_index.search_history()), 1)

    def test_reused_inode_and_matching_first_line_preserves_new_events(self):
        # Truncating/replacing a source simulates an inode recycled by rotation.
        # Same first event catches implementations which only fingerprint the
        # first line; the last checkpoint must also be revalidated.
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        first = json.dumps(event(url="https://same.example.org/",
                                 time_utc="2026-10-18T12:00:00Z"))
        second = json.dumps(event(url="https://original.example.org/"))
        with path.open("w") as stream:
            stream.write(first + "\n" + second + "\n")
        self.assertEqual(len(history_index.search_history()), 2)
        original_inode = path.stat().st_ino
        replacement = json.dumps(event(url="https://replacement.example.org/"))
        with path.open("w") as stream:
            stream.write(first + "\n" + replacement + "\n")
        self.assertEqual(path.stat().st_ino, original_inode)
        after = history_index.search_history()
        self.assertEqual(len(after), 4)
        self.assertEqual({r["domain"] for r in after},
                         {"same.example.org", "original.example.org",
                          "replacement.example.org"})
        self.assertEqual(history_index.search_history(), after)

    def test_rotated_sources_eventually_recycle_inodes_without_skipping(self):
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES": "64000"}):
            for i in range(150):
                append_history(event(url="https://example.org/",
                                     data="x" * 1200, serial=i))
            paths = [history_path().with_name(history_path().name + f".{i}")
                     for i in (3, 2, 1)] + [history_path()]
            surviving = sum(len(p.read_text().splitlines())
                            for p in paths if p.exists())
            history_index.sync_history()
            with sqlite3.connect(history_index.index_path()) as db:
                indexed = db.execute("SELECT count(*) FROM history").fetchone()[0]
            self.assertEqual(indexed, surviving)
            for i in range(150, 177):
                append_history(event(url="https://example.org/",
                                     data="x" * 1200, serial=i))
            history_index.sync_history()
            with sqlite3.connect(history_index.index_path()) as db:
                total = db.execute("SELECT count(*) FROM history").fetchone()[0]
            self.assertEqual(total, indexed + 27)
            history_index.sync_history()
            with sqlite3.connect(history_index.index_path()) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM history").fetchone()[0], total)

    def test_retention_does_not_delete_original_jsonl(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        append_history(event(time_utc=old, url="https://old.example.org/"))
        append_history(event(url="https://new.example.org/"))
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "1"}):
            records = history_index.search_history()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["domain"], "new.example.org")
        self.assertIn("old.example.org", history_path().read_text())
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "0"}):
            with self.assertRaises(ValueError):
                history_index.search_history()

    def test_rotation_keeps_inodes_and_does_not_duplicate_records(self):
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES": "64000"}):
            for i in range(16):
                append_history(event(url="https://example.org/",
                                     filler="x" * 2000, sequence=i))
            first = len(history_index.search_history(limit=100))
            self.assertEqual(first, 16)
            for i in range(16, 40):
                append_history(event(url="https://example.org/",
                                     filler="x" * 2000, sequence=i))
            second = len(history_index.search_history(limit=100))
            self.assertEqual(second, 40)
            self.assertEqual(len(history_index.search_history(limit=100)), 40)
            self.assertTrue(Path(str(history_path()) + ".1").exists())

    def test_simultaneous_reads_and_appends_keep_private_index_consistent(self):
        def writer():
            for i in range(16):
                append_history(event(url="https://docs.example.org/", count=i))
        def reader():
            for _ in range(5):
                history_index.search_history()
        with ThreadPoolExecutor(max_workers=3) as executor:
            tasks = [executor.submit(writer), executor.submit(reader),
                     executor.submit(reader)]
            for task in tasks:
                task.result(timeout=15)
        records = history_index.search_history()
        self.assertEqual(len(records), 16)
        with sqlite3.connect(history_index.index_path()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM history").fetchone()[0], 16)

    def test_refuse_symlink_history_and_index_inputs(self):
        history_path().symlink_to(Path(tempfile.gettempdir()) / "nonexistent-file")
        with self.assertRaises(OSError):
            history_index.sync_history()
        history_path().unlink()
        history_index.index_path().symlink_to(history_path())
        with self.assertRaises(OSError):
            history_index.sync_history()


if __name__ == "__main__":
    unittest.main()
