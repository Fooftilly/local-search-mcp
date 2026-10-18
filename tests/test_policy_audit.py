"""Stable request/decision correlation, migration and local audit regressions."""
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import admin_ui
import domain_policy as policy


class PolicyAuditTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": state.name,
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_request_id_survives_repeat_decisions_rename_and_restart(self):
        url = "https://docs.example.org/article?token=PRIVATE#fragment"
        sanitized = policy.safe_display_url(url)
        self.assertEqual(policy.request("docs.example.org", sanitized)[0], "pending")
        first = next(r for r in policy.entries() if r["domain"] == "docs.example.org")
        request_id = first["request_id"]
        self.assertRegex(request_id, r"^[0-9a-f]{24}$")
        self.assertEqual(first["request_count"], 1)
        policy.request("docs.example.org", sanitized)
        policy.decide("docs.example.org", "allow", scope="exact", reason="Article research")
        policy.initialize()
        policy.decide("docs.example.org", "rename", new_domain="renamed.example.org")
        policy.decide("renamed.example.org", "deny", reason="No longer trusted")
        now = next(r for r in policy.entries() if r["domain"] == "renamed.example.org")
        self.assertEqual(now["request_id"], request_id)
        actions = policy.recent_decisions()
        self.assertEqual([r["action"] for r in actions[:3]], ["deny", "rename", "allow"])
        self.assertTrue(all(r["request_id"] == request_id for r in actions[:3]))
        self.assertEqual(actions[2]["reason"], "Article research")
        self.assertEqual(actions[2]["request_count"], 2)
        self.assertEqual(actions[2]["requested_url"], sanitized)
        self.assertNotIn("PRIVATE", str(actions))
        policy.decide("renamed.example.org", "delete")
        self.assertEqual(policy.recent_decisions()[0]["request_id"], request_id)

    def test_existing_v4_database_receives_stable_ids_and_audit_columns(self):
        path = policy.database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.executescript("""
                CREATE TABLE domains (
                    domain TEXT PRIMARY KEY, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    last_requested_at TEXT NOT NULL DEFAULT '',
                    last_requested_url TEXT NOT NULL DEFAULT '',
                    explicit_rule INTEGER NOT NULL DEFAULT 0,
                    match_scope TEXT NOT NULL DEFAULT 'subdomains',
                    expires_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL, domain TEXT NOT NULL, action TEXT NOT NULL
                );
                INSERT INTO domains(domain,status,created_at,updated_at,explicit_rule)
                  VALUES('example.org','allowed','2026-01-01','2026-01-01',1);
                INSERT INTO decisions(at,domain,action)
                  VALUES('2026-01-01','example.org','allow');
            """)
        policy.initialize()
        before = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertRegex(before["request_id"], r"^[0-9a-f]{24}$")
        policy.initialize()
        after = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual(before["request_id"], after["request_id"])
        policy.decide("example.org", "deny", reason="Reviewed")
        self.assertEqual(policy.recent_decisions()[0]["request_id"], before["request_id"])
        self.assertEqual(policy.recent_decisions()[-1]["reason"], "")
        self.assertEqual(policy.request("example.org", "https://example.org/")[0], "denied")

    def test_rejects_bad_reasons_before_changing_permission(self):
        for reason in ("x" * 161, "newline\nsecret", "tab\tsecret", 1, None):
            with self.subTest(reason=repr(reason)), self.assertRaises(ValueError):
                policy.decide("example.org", "allow", reason=reason)
        self.assertFalse(any(r["domain"] == "example.org" for r in policy.entries()))

    def test_ui_escapes_reason_and_redacts_request_url_query(self):
        url = policy.safe_display_url("https://example.org/p?password=HIDDEN")
        policy.request("example.org", url)
        policy.decide("example.org", "deny", reason="<script>alert(1)</script>")
        html = admin_ui.render()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("HIDDEN", html)
        self.assertIn("https://example.org/p", html)
        row = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertIn(row["request_id"], html)

    def test_clear_pending_keeps_request_id_in_audit(self):
        policy.request("queued.example.org", "https://queued.example.org/path")
        rid = next(r for r in policy.entries() if r["domain"] == "queued.example.org")["request_id"]
        self.assertEqual(policy.clear_pending_requests(), 1)
        self.assertEqual(policy.recent_decisions()[0]["request_id"], rid)


if __name__ == "__main__":
    unittest.main()
