"""Regression tests for explicit exact-host and time-bounded domain decisions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch
from urllib.parse import urlencode
import os
import sqlite3
import tempfile
import unittest

import admin_ui
import domain_policy as policy
import request_cache
from batch_fetch import fetch_many
from urllib.parse import urlsplit


class RuleModeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_defaults_preserve_existing_subdomain_permissions(self):
        policy.decide("example.org", "allow")
        policy.initialize()
        self.assertEqual(policy.request("child.example.org", "https://child.example.org/"),
                         ("allowed", "example.org"))
        row = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual((row["match_scope"], row["expires_at"]),
                         ("subdomains", ""))

    def test_exact_host_does_not_grant_or_deny_subdomains(self):
        policy.decide("example.org", "allow", scope="exact")
        self.assertEqual(policy.request("example.org", "https://example.org/")[0],
                         "allowed")
        self.assertEqual(policy.request("child.example.org", "https://child.example.org/")[0],
                         "pending")
        policy.decide("example.org", "deny", scope="exact")
        self.assertEqual(policy.request("example.org", "https://example.org/")[0],
                         "denied")
        self.assertEqual(policy.request("other.example.org", "https://other.example.org/")[0],
                         "pending")

    def test_exact_child_denial_overrides_permanent_parent(self):
        policy.decide("example.org", "allow")
        policy.decide("blocked.example.org", "deny", scope="exact")
        self.assertEqual(policy.request("blocked.example.org", "https://blocked.example.org/"),
                         ("denied", "blocked.example.org"))
        self.assertEqual(policy.request("deep.blocked.example.org",
                                        "https://deep.blocked.example.org/"),
                         ("allowed", "example.org"))

    def test_exact_parent_does_not_clear_child_pending_requests(self):
        self.assertEqual(policy.request("child.example.org",
                                         "https://child.example.org/")[0], "pending")
        policy.decide("example.org", "allow", scope="exact")
        self.assertIn("child.example.org", {r["domain"] for r in policy.entries(status="pending")})

    def test_expiration_survives_restart_and_no_longer_authorizes(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "allow", expires_minutes=60)
            self.assertEqual(policy.request("child.example.org",
                                             "https://child.example.org/")[0], "allowed")
        policy.initialize()
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            self.assertEqual(policy.request("child.example.org",
                                             "https://child.example.org/")[0], "pending")
            row = next(r for r in policy.entries() if r["domain"] == "example.org")
            self.assertTrue(row["expired"])
            self.assertEqual(policy.counts()["allowed"], 1)  # seed example.com only

    def test_expired_exact_rule_reenters_visible_pending_queue(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "deny", scope="exact", expires_minutes=60)
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            self.assertEqual(policy.request("example.org", "https://example.org/")[0],
                             "pending")
            row = next(r for r in policy.entries() if r["domain"] == "example.org")
            self.assertEqual(row["status"], "pending")
            self.assertFalse(row["explicit_rule"])
            # Keep the old deadline for a non-mutating in-flight expiry
            # recheck even after an agent requeues the visible approval row.
            self.assertEqual(row["expires_at"], "2026-10-17T13:00:00Z")
            self.assertTrue(policy.expired_grant_without_active_fallback("example.org"))

    def test_concurrent_expired_rule_checks_do_not_reuse_authorization(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "allow", expires_minutes=60)
        barrier = Barrier(6)
        def check(_):
            barrier.wait(timeout=10)
            return policy.request("same.example.org", "https://same.example.org/")[0]
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(check, range(6)))
        self.assertEqual(results, ["pending"] * 6)
        rows = [r for r in policy.entries() if r["domain"] == "same.example.org"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_count"], 6)

    def test_expired_child_rule_falls_back_to_active_parent(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "allow")
            policy.decide("child.example.org", "deny", scope="exact",
                          expires_minutes=60)
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            self.assertEqual(
                policy.request("child.example.org", "https://child.example.org/")[0],
                "allowed")
            self.assertFalse(
                policy.expired_grant_without_active_fallback("child.example.org"))

    def test_expired_child_allow_falls_back_to_parent_deny(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "deny")
            policy.decide("child.example.org", "allow", scope="exact",
                          expires_minutes=60)
            self.assertFalse(
                policy.expired_grant_without_active_fallback("child.example.org"))
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            self.assertEqual(
                policy.request("child.example.org", "https://child.example.org/")[0],
                "denied")
            self.assertTrue(
                policy.expired_grant_without_active_fallback("child.example.org"))

    def test_expired_exact_rule_respects_full_queue_limit(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "allow", scope="exact", expires_minutes=60)
        with policy._db() as conn:
            now = "2026-10-17T12:00:00Z"
            conn.executemany(
                """INSERT INTO domains(domain,status,created_at,updated_at)
                   VALUES(?,'pending',?,?)""",
                [(f"queued{i}.example.net", now, now)
                 for i in range(policy.MAX_PENDING_TOTAL)]
            )
            conn.commit()
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            self.assertEqual(
                policy.request("example.org", "https://example.org/")[0],
                "queue_full")
            row = next(r for r in policy.entries() if r["domain"] == "example.org")
            self.assertEqual(row["status"], "allowed")
            self.assertTrue(row["expired"])
            self.assertEqual(row["request_count"], 1)
            self.assertEqual(policy.counts()["pending"], policy.MAX_PENDING_TOTAL)

    def test_only_clearing_expired_grants_invalidates_inflight_fetches(self):
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        policy.request("unseen.example.org", "https://unseen.example.org/")
        old_epoch = request_cache.policy_epoch()
        self.assertEqual(policy.clear_pending_requests(), 1)
        self.assertEqual(request_cache.policy_epoch(), old_epoch)

        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "allow", scope="exact",
                          expires_minutes=60)
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            self.assertEqual(policy.request("example.org",
                                            "https://example.org/")[0], "pending")
            old_epoch = request_cache.policy_epoch()
            self.assertEqual(policy.clear_pending_requests(), 1)
            self.assertEqual(request_cache.policy_epoch(), old_epoch + 1)
            self.assertEqual(policy.clear_pending_requests(), 0)
            self.assertEqual(request_cache.policy_epoch(), old_epoch + 1)

    def test_expired_rules_share_site_hourly_admission_limits(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            for i in range(policy.MAX_NEW_PENDING_PER_SITE_PER_HOUR + 1):
                policy.decide(f"host{i}.example.org", "allow",
                              scope="exact", expires_minutes=60)
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            outcomes = [
                policy.request(f"host{i}.example.org",
                               f"https://host{i}.example.org/")[0]
                for i in range(policy.MAX_NEW_PENDING_PER_SITE_PER_HOUR + 1)
            ]
            self.assertEqual(outcomes,
                             ["pending"] * policy.MAX_NEW_PENDING_PER_SITE_PER_HOUR +
                             ["queue_full"])
            with policy._db() as conn:
                count = conn.execute(
                    "SELECT count(*) FROM pending_admissions WHERE site=?",
                    ("example.org",)).fetchone()[0]
            self.assertEqual(count, policy.MAX_NEW_PENDING_PER_SITE_PER_HOUR)
            self.assertEqual(
                policy.request("host0.example.org", "https://host0.example.org/")[0],
                "pending")
            with policy._db() as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM pending_admissions WHERE site=?",
                    ("example.org",)).fetchone()[0], count)

    def test_expiry_does_not_invalidate_unrelated_batch_results(self):
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "allow", scope="exact", expires_minutes=60)
        with patch.object(policy, "utcnow", return_value="2026-10-17T13:00:00Z"):
            before = request_cache.policy_epoch()

            def local_fetch(url, _mode, _fresh):
                status, _ = policy.request(urlsplit(url).hostname, url)
                return {"url": url, "success": status == "allowed",
                        "status": "approval_required" if status == "pending" else status}

            result = fetch_many(["https://example.com/", "https://example.org/"],
                                "raw", False, local_fetch)
            self.assertEqual(request_cache.policy_epoch(), before)
            self.assertTrue(result["results"][0]["result"]["success"])
            self.assertEqual(result["results"][1]["result"]["status"],
                             "approval_required")
            row = next(r for r in policy.entries() if r["domain"] == "example.org")
            self.assertEqual(row["match_scope"], "exact")
            self.assertEqual(row["status"], "pending")

    def test_expiry_is_checked_after_waiting_for_the_database(self):
        with patch.object(policy, "utcnow", return_value="2026-10-17T12:00:00Z"):
            policy.decide("example.org", "allow", expires_minutes=60)
        clock = {"now": "2026-10-17T12:59:59Z"}
        real_db = policy._db

        @contextmanager
        def db_after_wait():
            # Simulate a SQLite lock wait crossing the expiration boundary.
            # Previously request() captured utcnow() before entering _db().
            clock["now"] = "2026-10-17T13:00:00Z"
            with real_db() as conn:
                yield conn

        with patch.object(policy, "utcnow", side_effect=lambda: clock["now"]):
            with patch.object(policy, "_db", db_after_wait):
                self.assertEqual(policy.request("example.org", "https://example.org/")[0],
                                 "pending")

    def test_invalid_scope_and_ttl_rejected(self):
        for scope in ("", "all", "subdomains; DROP TABLE domains"):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                policy.decide("example.org", "allow", scope=scope)
        for ttl in (-1, 1, 999999, 1.5, True, "60"):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                policy.decide("example.org", "allow", expires_minutes=ttl)

    def test_old_sqlite_schema_migrates_without_broadening(self):
        path = policy.database_path()
        path.parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE domains (
                  domain TEXT PRIMARY KEY, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  request_count INTEGER NOT NULL DEFAULT 0,
                  last_requested_at TEXT NOT NULL DEFAULT '',
                  last_requested_url TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO domains(domain,status,created_at,updated_at)
                  VALUES('example.org','denied','2026-01-01','2026-01-01');
            """)
        policy.initialize()
        with policy._db() as conn:
            cols = {c["name"] for c in conn.execute("PRAGMA table_info(domains)")}
        self.assertIn("match_scope", cols)
        self.assertIn("expires_at", cols)
        self.assertEqual(policy.request("child.example.org", "https://child.example.org/")[0],
                         "denied")


class RuleModeHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)
        self.server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0), admin_ui.Handler)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        port_patch = patch.object(admin_ui, "PORT", self.server.server_address[1])
        port_patch.start()
        self.addCleanup(port_patch.stop)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def post(self, fields):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=4)
        try:
            conn.request("POST", "/domain", body=urlencode({
                "csrf": admin_ui.CSRF, **fields
            }), headers={"Content-Type": "application/x-www-form-urlencoded"})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_post_requires_duration_and_preserves_existing_timed_rule(self):
        # An omitted/blank duration is not an implicit permanent grant.
        for action in ("allow", "deny"):
            for duration in (None, ""):
                with self.subTest(action=action, duration=duration):
                    fields = {"domain": "example.org", "action": action}
                    if duration is not None:
                        fields["expires_minutes"] = duration
                    self.assertEqual(self.post(fields)[0], 400)
                    self.assertFalse(any(r["domain"] == "example.org"
                                         for r in policy.entries()))

        self.assertEqual(self.post({"domain": "example.org", "action": "allow",
                                    "scope": "exact", "expires_minutes": "60"})[0], 200)
        original = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertTrue(original["expires_at"])
        self.assertEqual(self.post({"domain": "example.org", "action": "allow"})[0], 400)
        after = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual(after["expires_at"], original["expires_at"])
        self.assertEqual(after["match_scope"], "exact")

        # Explicitly selected permanent permissions remain supported.
        self.assertEqual(self.post({"domain": "example.org", "action": "allow",
                                    "expires_minutes": "0"})[0], 200)
        updated = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual(updated["expires_at"], "")
        # Duration is irrelevant for rename/delete actions.
        self.assertEqual(self.post({"domain": "example.org", "action": "delete"})[0], 200)

    def test_post_scope_and_expiration_and_validate_inputs(self):
        status, _ = self.post({"domain": "example.org", "action": "allow",
                               "scope": "exact", "expires_minutes": "60"})
        self.assertEqual(status, 200)
        row = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual(row["match_scope"], "exact")
        self.assertTrue(row["expires_at"])
        self.assertEqual(policy.request("child.example.org",
                                         "https://child.example.org/")[0], "pending")
        self.assertEqual(self.post({"domain": "example.org", "action": "allow",
                                    "scope": "invalid",
                                    "expires_minutes": "60"})[0], 400)
        self.assertEqual(self.post({"domain": "example.org", "action": "allow",
                                    "expires_minutes": "666"})[0], 400)
        self.assertEqual(self.post({"domain": "example.org", "action": "allow",
                                    "expires_minutes": "not-a-number"})[0], 400)


if __name__ == "__main__":
    unittest.main()
