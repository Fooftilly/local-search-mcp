"""Regression tests for explicit exact-host and time-bounded domain decisions."""
from concurrent.futures import ThreadPoolExecutor
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
            self.assertEqual(row["expires_at"], "")

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
                                    "scope": "invalid"})[0], 400)
        self.assertEqual(self.post({"domain": "example.org", "action": "allow",
                                    "expires_minutes": "666"})[0], 400)
        self.assertEqual(self.post({"domain": "example.org", "action": "allow",
                                    "expires_minutes": "not-a-number"})[0], 400)


if __name__ == "__main__":
    unittest.main()
