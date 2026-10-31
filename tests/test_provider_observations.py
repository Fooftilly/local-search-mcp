"""Private provider-observation reports: existing audit only, no live requests."""
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import admin_ui
from history_log import append_history
import provider_observations as provider


class ProviderObservationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.jsonl"
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.path),
        })
        env.start()
        self.addCleanup(env.stop)
        self.now = datetime.now(timezone.utc)

    def add(self, *, minutes=0, **kwargs):
        event = {
            "time_utc": (self.now - timedelta(minutes=minutes)).isoformat(),
            "kind": "search", "status": "success", "duration_ms": 150,
            "query": "SECRET_SEARCH_QUERY", "url": "https://private.example?token=SECRET",
            "results": [{
                "title": "SECRET_TITLE", "url": "https://private.example/SECRET_URL",
                "engine": "yahoo",
            }],
        }
        event.update(kwargs)
        append_history(event)

    def test_counts_timing_missingness_and_privacy(self):
        self.add(duration_ms=100, categories="news",
                 results=[{"engine": "yahoo"}, {"engine": "qwant"},
                          {"engine": "yahoo"}])
        self.add(minutes=1, duration_ms=200, categories="news",
                 results=[{"engine": "yahoo"}])
        self.add(minutes=2, status="error", engines="yahoo", duration_ms=70,
                 results=None, error_type="timeout")
        self.add(minutes=3, duration_ms=None, categories="PrivateKeyword",
                 results=[])
        self.add(minutes=60 * 26, duration_ms=100, results=[{"engine": "yahoo"}])
        answer = provider.observations(24, now=self.now)
        self.assertTrue(answer["available"])
        self.assertEqual(answer["sampled_search_calls"], 4)
        self.assertEqual(answer["non_success_search_calls"], 1)
        self.assertEqual(answer["search_calls_with_observed_sources"], 2)
        self.assertEqual(answer["search_calls_without_observed_sources"], 1)
        self.assertEqual(answer["provider_uptime"], "not_measured")
        self.assertEqual(answer["provider_failure_coverage"],
                         "no_opt_in_reports_retained")
        self.assertEqual(answer["cache_hit_coverage"], "not_retained")
        engines = {row["name"]: row for row in answer["observed_result_engines"]}
        self.assertEqual(engines["yahoo"]["observed_searches"], 2)
        self.assertEqual(engines["yahoo"]["mean_search_call_ms"], 150)
        self.assertEqual(engines["qwant"]["observed_searches"], 1)
        categories = {row["name"]: row for row in answer["requested_categories"]}
        self.assertEqual(categories["news"]["observed_searches"], 2)
        self.assertEqual(categories["unspecified"]["observed_searches"], 1)
        for marker in ("SECRET_SEARCH_QUERY", "SECRET_TITLE", "SECRET_URL",
                       "private.example", "PrivateKeyword", "timeout"):
            self.assertNotIn(marker, repr(answer))

    def test_opt_in_failure_aggregation_merges_names_across_case(self):
        for name in ("Qwant", "qwant", "QWANT"):
            self.add(provider_diagnostics_opt_in=True,
                     provider_diagnostics={
                         "coverage": "partial", "unresponsive_engines": [name],
                         "suppressed_or_unparsed_names": 0, "truncated": False})
        answer = provider.observations(24, now=self.now)
        self.assertEqual(answer["opted_in_diagnostic_calls"], 3)
        self.assertEqual(answer["reported_partial_provider_calls"], 3)
        self.assertEqual(len(answer["reported_unresponsive_engines"]), 1)
        self.assertEqual(answer["reported_unresponsive_engines"][0]["name"].casefold(),
                         "qwant")
        self.assertEqual(answer["reported_unresponsive_engines"][0]["reported_unresponsive_calls"], 3)
        self.assertFalse(answer["provider_failure_groups_truncated"])

    def test_opt_in_suppression_count_is_not_lost_after_sanitization(self):
        self.add(provider_diagnostics_opt_in=True,
                 provider_diagnostics={
                     "coverage": "partial", "unresponsive_engines": ["yahoo"],
                     "suppressed_or_unparsed_names": 3, "truncated": True})
        observed = provider.observations(24, now=self.now)
        self.assertEqual(observed["provider_labels_suppressed"], 3)
        self.assertTrue(observed["retention_or_scan_limited"])
        self.assertEqual(observed["reported_unresponsive_engines"],
                         [{"name": "yahoo", "reported_unresponsive_calls": 1}])

    def test_parenthesized_engine_and_all_eight_requested_categories(self):
        self.add(categories="general,web,images,news,videos,music,files,it",
                 results=[{"engine": "naver (KO)"}])
        result = provider.observations(1, now=self.now)
        self.assertEqual(result["search_calls_with_observed_sources"], 1)
        self.assertEqual(result["search_calls_without_observed_sources"], 0)
        self.assertEqual(result["observed_result_engines"][0]["name"],
                         "naver (KO)")
        self.assertEqual({x["name"] for x in result["requested_categories"]},
                         {"general", "web", "images", "news",
                          "videos", "music", "files", "it"})

    def test_existing_history_lock_is_used_for_atomic_file_snapshot(self):
        import fcntl
        self.add(results=[{"engine": "yahoo"}])
        with patch.object(provider.fcntl, "flock", wraps=fcntl.flock) as lock:
            result = provider.observations(24, now=self.now)
        self.assertTrue(result["source_snapshot_lock_acquired"])
        modes = [x.args[1] for x in lock.call_args_list]
        self.assertIn(fcntl.LOCK_SH | fcntl.LOCK_NB, modes)
        self.assertIn(fcntl.LOCK_UN, modes)

    def test_rotated_history_truncation_and_missing_timing(self):
        self.add(minutes=4, duration_ms=None, results=[{"engine": "yahoo"}])
        archive = self.path.with_name(self.path.name + ".1")
        self.path.rename(archive)
        self.add(minutes=1, duration_ms=50, results=[{"engine": "yahoo"}])
        answer = provider.observations(1, now=self.now)
        self.assertEqual(answer["sampled_search_calls"], 2)
        row = answer["observed_result_engines"][0]
        self.assertEqual(row["observed_searches"], 2)
        self.assertEqual(row["timed_searches"], 1)
        self.assertEqual(row["mean_search_call_ms"], 50)
        self.assertEqual(answer["source_files_scanned"], 2)

    def test_invalid_timestamps_malformed_lines_and_bounds(self):
        with self.path.open("wb") as fp:
            fp.write(b"this is not json\n")
            fp.write(json.dumps({
                "time_utc": self.now.isoformat(), "kind": "search",
                "status": "success", "duration_ms": 1,
                "results": [{"engine": "SECRET_PASSWORD"}],
            }).encode() + b"\n")
            fp.write(b"x" * (provider.MAX_EVENT_BYTES + 1) + b"\n")
        answer = provider.observations(24, now=self.now)
        self.assertEqual(answer["malformed_records_ignored"], 2)
        self.assertTrue(answer["retention_or_scan_limited"])
        self.assertEqual(answer["sampled_search_calls"], 1)
        self.assertEqual(answer["observed_result_engines"], [])
        self.assertRaises(ValueError, provider.observations, 0)
        self.assertRaises(ValueError, provider.observations, True)

    def test_no_files_is_unavailable_and_symlink_fails_closed(self):
        self.assertFalse(provider.observations()["available"])
        outside = Path(self.temp.name) / "unrelated.txt"
        outside.write_text("PRIVATE_BYTES")
        self.path.symlink_to(outside)
        with self.assertRaises(OSError):
            provider.observations()

    def test_special_fifo_input_and_writer_lock_fail_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("POSIX FIFO support required")
        def check_fast():
            captured = []
            def worker():
                try:
                    provider.observations(24)
                    captured.append("unexpected success")
                except OSError:
                    captured.append("refused")
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            thread.join(1.5)
            self.assertFalse(thread.is_alive(), "Special history file must not block")
            self.assertEqual(captured, ["refused"])

        os.mkfifo(self.path)
        check_fast()
        self.path.unlink()
        # Existing private history lock may also be replaced by a FIFO:
        # it must never hang before stat.S_ISREG is verified.
        self.path.write_text("{}\n")
        os.mkfifo(Path(self.temp.name) / ".history.lock")
        check_fast()

    def test_file_tail_scan_is_explicitly_limited(self):
        with self.path.open("wb") as fp:
            fp.write(b"{}\n" * (provider.MAX_FILE_BYTES // 3 + 30))
        answer = provider.observations(24, now=self.now)
        self.assertTrue(answer["retention_or_scan_limited"])
        self.assertLessEqual(answer["records_examined"], provider.MAX_EVENTS)


class ProviderObservationsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.jsonl"
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": self.temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.path),
        })
        env.start()
        self.addCleanup(env.stop)
        self.server = admin_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), admin_ui.Handler)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        port_patch = patch.object(admin_ui, "PORT", self.port)
        port_patch.start()
        self.addCleanup(port_patch.stop)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def get(self, path, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8"), dict(response.getheaders())
        finally:
            conn.close()

    def test_private_html_and_origin_filters_no_new_provider_requests(self):
        append_history({
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "search", "status": "success", "duration_ms": 80,
            "query": "SECRET_PRIVATE_SEARCH",
            "results": [{"engine": "yahoo",
                         "title": "SECRET_TITLE",
                         "url": "https://example.org/SECRET_PATH"}],
        })
        with (patch.object(admin_ui.policy, "decide",
                           side_effect=AssertionError("No approval changes")),
              patch.object(admin_ui, "service_status") as service):
            status, html, headers = self.get("/provider-observations?hours=24")
        self.assertEqual(status, 200)
        self.assertIn("yahoo", html)
        self.assertIn("whole-call", html.lower())
        self.assertIn("not measured", html.lower())
        self.assertIn("no-store", headers["Cache-Control"])
        for secret in ("SECRET_PRIVATE_SEARCH", "SECRET_TITLE", "SECRET_PATH"):
            self.assertNotIn(secret, html)
        self.assertEqual(self.get("/provider-observations?hours=0")[0], 400)
        self.assertEqual(self.get("/provider-observations?hours=24&hours=1")[0], 400)
        self.assertEqual(self.get("/provider-observations?kind=search")[0], 400)
        self.assertEqual(self.get("/provider-observations", {
            "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.get("/provider-observations", {
            "Origin": "https://evil.example"})[0], 403)
        self.assertIn("Provider observations", self.get("/history")[1])

    def test_unavailable_or_unsafe_source_fails_closed(self):
        self.path.symlink_to(Path(self.temp.name) / "absent.txt")
        status, html, _ = self.get("/provider-observations")
        self.assertEqual(status, 200)
        self.assertIn("Private history is unavailable", html)
        self.assertNotIn("absent.txt", html)
        self.assertNotIn(str(self.path), html)


if __name__ == "__main__":
    unittest.main()
