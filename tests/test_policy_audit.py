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
        self.assertEqual(actions[2]["requested_url"], "https://docs.example.org")
        self.assertEqual(actions[2]["first_requested_at"],
                         first["first_requested_at"])
        self.assertEqual(actions[2]["last_requested_at"], now["last_requested_at"])
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

    def test_first_request_not_confused_with_import_or_creation(self):
        with patch.object(policy, "utcnow", return_value="2026-10-18T05:00:00Z"):
            policy.decide("example.org", "allow", reason="Manual approval")
        before = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual(before["first_requested_at"], "")
        self.assertEqual(policy.recent_decisions()[0]["first_requested_at"], "")
        with patch.object(policy, "utcnow", return_value="2026-10-18T06:00:00Z"):
            self.assertEqual(policy.request(
                "example.org", "https://example.org/first")[0], "allowed")
        with patch.object(policy, "utcnow", return_value="2026-10-18T07:00:00Z"):
            self.assertEqual(policy.request(
                "example.org", "https://example.org/second")[0], "allowed")
            policy.decide("example.org", "deny", reason="Reviewed")
        row = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual(row["first_requested_at"], "2026-10-18T06:00:00Z")
        last = policy.recent_decisions()[0]
        self.assertEqual(last["first_requested_at"], "2026-10-18T06:00:00Z")
        self.assertEqual(last["last_requested_at"], "2026-10-18T07:00:00Z")
        self.assertEqual(last["request_count"], 2)

    def test_raw_request_is_sanitized_even_outside_url_validator(self):
        policy.request("example.org", "https://example.org/a?secret=REDACTrMe#part")
        row = next(r for r in policy.entries() if r["domain"] == "example.org")
        self.assertEqual(row["last_requested_url"], "https://example.org/a")
        self.assertNotIn("REDACTrMe", admin_ui.render())

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

    def test_live_audit_updates_and_respects_domain_filter(self):
        policy.decide("yes.example.org", "allow", reason="SAFE_REASON")
        policy.decide("no.example.net", "deny", reason="OTHER_REASON")
        live = admin_ui.live_snapshot(query="yes.example.org")
        self.assertIn("SAFE_REASON", live["audit_html"])
        self.assertNotIn("OTHER_REASON", live["audit_html"])
        self.assertIn("id=\"live-audit\"", admin_ui.render("yes.example.org"))
        self.assertNotIn("OTHER_REASON", admin_ui.render("yes.example.org"))
        script = __import__("dashboard_live_script").LIVE_SCRIPT
        self.assertIn('updateTable(auditBody, data.audit_html)', script)

    def test_parent_decision_records_resolved_child_ids_and_origin(self):
        policy.request("child.docs.example.org",
                       "https://child.docs.example.org/invite/PRIVATE_LINK?key=HIDDEN")
        child = next(r for r in policy.entries()
                     if r["domain"] == "child.docs.example.org")
        policy.decide("docs.example.org", "allow", scope="subdomains",
                      reason="Approve parent after review")
        rows = policy.recent_decisions(search="child.docs.example.org")
        self.assertEqual(rows[0]["request_id"], child["request_id"])
        self.assertEqual(rows[0]["action"], "delete")
        self.assertEqual(rows[0]["reason"], "Approve parent after review")
        self.assertEqual(rows[0]["requested_url"], "https://child.docs.example.org")
        self.assertNotIn("PRIVATE_LINK", str(rows))

    def test_legacy_secret_in_path_never_leaks_to_dashboard_or_audit(self):
        policy.request("example.org", "https://example.org/invite/PRIVATE_LINK")
        policy.decide("example.org", "allow")
        with policy._db() as conn:
            conn.execute(
                "UPDATE domains SET last_requested_url=? WHERE domain=?",
                ("https://example.org/private/LEGACY_TOKEN?key=HIDDEN", "example.org"))
            conn.commit()
        html = admin_ui.render()
        self.assertNotIn("LEGACY_TOKEN", html)
        self.assertNotIn("PRIVATE_LINK", html)
        self.assertNotIn("HIDDEN", html)
        self.assertNotIn("PRIVATE_LINK", str(policy.recent_decisions()))
        self.assertNotIn("LEGACY_TOKEN", str(admin_ui.live_snapshot()))

    def test_clear_pending_keeps_request_id_in_audit(self):
        policy.request("queued.example.org", "https://queued.example.org/path")
        rid = next(r for r in policy.entries() if r["domain"] == "queued.example.org")["request_id"]
        self.assertEqual(policy.clear_pending_requests(), 1)
        self.assertEqual(policy.recent_decisions()[0]["request_id"], rid)


if __name__ == "__main__":
    unittest.main()
