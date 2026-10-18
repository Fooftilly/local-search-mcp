"""Loopback-only one-use grant control and secret-free decision audit tests."""
from collections import OrderedDict
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlencode
import os
import socket
import tempfile
import unittest

import admin_ui
import bridge
import document_pages
import domain_policy as policy
import request_cache


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
               ("93.184.216.34", 443))]


class OnceDashboardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": tmp.name,
            "ALLOWED_CRAWL_DOMAINS": "example.com",
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(tmp.name) / "history.jsonl"),
        })
        env.start()
        self.addCleanup(env.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        host = admin_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), admin_ui.Handler)
        host.daemon_threads = True
        self.addCleanup(host.server_close)
        self.port = host.server_address[1]
        patcher = patch.object(admin_ui, "PORT", self.port)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = Thread(target=host.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(host.shutdown)

    def request(self, path="/", data=None, *, token=None, headers=None, method=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=4)
        fields = None if data is None else {
            "csrf": admin_ui.CSRF if token is None else token, **data}
        body = urlencode(fields) if fields is not None else None
        try:
            conn.request(method or ("POST" if data is not None else "GET"),
                         path, body=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  **(headers or {})})
            res = conn.getresponse()
            return res.status, res.read().decode("utf-8")
        finally:
            conn.close()

    def grant(self, domain="docs.example.org", *, headers=None,
              token=None, **kwargs):
        return self.request("/once", {"domain": domain, "action": "stage",
                    "scope": "exact", "expires_minutes": "60", **kwargs},
                    headers=headers, token=token)

    def test_local_staging_and_approval_queue_stays_pending(self):
        original = ("https://docs.example.org/private/path?token=NEVER_RECORD")
        self.assertEqual(policy.evaluate_fetch_url(original)["status"],
                         "approval_required")
        row = next(r for r in policy.entries() if r["domain"] == "docs.example.org")
        self.assertEqual(self.grant(request_id=row["request_id"],
                                    reason="Short approval")[0], 200)
        with policy._db() as conn:
            grant = conn.execute(
                "SELECT grant_id,consumed_at FROM once_grants WHERE domain=?",
                ("docs.example.org",)).fetchone()
        self.assertEqual(grant["consumed_at"], "")
        self.assertFalse(policy.is_domain_currently_allowed("docs.example.org"))
        self.assertEqual(policy.evaluate_fetch_url(original)["status"],
                         "approval_required")
        code, html = self.request()
        self.assertEqual(code, 200)
        self.assertIn("Single-use domain permission", html)
        self.assertIn("Allow once", html)
        self.assertIn("Short approval", html)
        self.assertIn("docs.example.org", html)
        self.assertNotIn(grant["grant_id"], html)
        self.assertNotIn("NEVER_RECORD", html)
        self.assertNotIn("/private/path", html)
        events = policy.recent_once_events()
        self.assertEqual(events[0]["action"], "stage")
        self.assertEqual(events[0]["request_id"], row["request_id"])
        self.assertEqual(events[0]["requested_url"], "https://docs.example.org")
        self.assertNotIn(grant["grant_id"], repr(events))

    def test_one_use_claim_and_audit_consume_without_reusable_permission(self):
        code, _ = self.grant()
        self.assertEqual(code, 200)
        url = "https://docs.example.org/a"
        body = {"url": url, "success": True, "markdown": "CONTENT",
                "truncated": False}
        with patch.object(policy.socket, "getaddrinfo", return_value=PUBLIC_DNS), \
             patch.object(bridge, "CRAWLER_SECURITY_WARNING", None), \
             patch.object(bridge, "_run_fetch", return_value=body) as crawler:
            result = bridge.web_fetch(url)
        self.assertTrue(result["success"])
        self.assertEqual(crawler.call_count, 1)
        self.assertFalse(policy.is_domain_currently_allowed("docs.example.org"))
        events = policy.recent_once_events()
        self.assertEqual([r["action"] for r in events[:2]], ["consume", "stage"])
        self.assertNotIn("grant_id", repr(events))
        status, html = self.request()
        self.assertEqual(status, 200)
        self.assertIn("consumed", html)
        self.assertNotIn("CONTENT", html)

    def test_local_revocation_invalidates_inflight_proof(self):
        self.assertEqual(self.grant()[0], 200)
        claim = policy._consume_once_grant("docs.example.org")
        self.assertIsNotNone(claim)
        self.assertTrue(policy._once_proof_current("docs.example.org", claim))
        status, _ = self.request("/once", {
            "action": "revoke", "domain": "docs.example.org",
            "reason": "No longer required"})
        self.assertEqual(status, 200)
        self.assertFalse(policy._once_proof_current("docs.example.org", claim))
        self.assertEqual([r["action"] for r in policy.recent_once_events()[:3]],
                         ["revoke", "consume", "stage"])
        self.assertEqual(policy.recent_once_events()[0]["reason"],
                         "No longer required")
        self.assertNotIn(claim, self.request()[1])

    def test_stale_pending_id_denial_and_malformed_inputs_fail_closed(self):
        result = policy.evaluate_fetch_url("https://docs.example.org/a")
        self.assertEqual(result["status"], "approval_required")
        original = next(r for r in policy.entries()
                        if r["domain"] == "docs.example.org")
        self.assertEqual(self.grant(request_id="a" * 24)[0], 400)
        self.assertEqual(self.grant(request_id=original["request_id"])[0], 200)
        policy.decide("denied.example.org", "deny")
        for domain, fields in (
            ("denied.example.org", {}),
            ("github.io", {}),
            ("localhost", {}),
            ("127.0.0.1", {}),
            ("docs.example.org", {"scope": "*"}),
            ("docs.example.org", {"expires_minutes": "0"}),
            ("docs.example.org", {"expires_minutes": "999"}),
            ("docs.example.org", {"reason": "a" * 161}),
            ("docs.example.org", {"request_id": "b" * 24}),
        ):
            with self.subTest(domain=domain, fields=fields):
                self.assertEqual(self.grant(domain, **fields)[0], 400)

    def test_csrf_host_origin_and_get_never_mutate(self):
        self.assertEqual(self.request("/once", {"domain": "new.example.org",
            "action": "stage", "scope": "exact",
            "expires_minutes": "60"}, token="invalid")[0], 403)
        self.assertEqual(self.grant("new.example.org",
                         headers={"Origin": "http://evil.invalid"})[0], 403)
        self.assertEqual(self.grant("new.example.org",
                         headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.grant("new.example.org",
                         headers={"Host": "invalid.example.org"})[0], 403)
        self.assertEqual(self.request("/once?action=stage&domain=new.example.org")[0], 404)
        self.assertFalse(policy.once_grant_entries())
        self.assertEqual(self.request("/once", {"action": "nonsense",
                         "domain": "new.example.org"})[0], 400)
        self.assertFalse(policy.once_grant_entries())

    def test_untrusted_reason_is_escaped_and_never_becomes_markup(self):
        text = '<img src=x onerror="alert(1)">'
        self.assertEqual(self.grant(reason=text)[0], 200)
        html = self.request()[1]
        self.assertNotIn(text, html)
        self.assertIn("&lt;img src=x", html)
        self.assertEqual(policy.recent_once_events()[0]["reason"], text)
        self.assertEqual(self.request("/once", {"action": "revoke",
                         "domain": "docs.example.org", "reason": text})[0], 200)
        self.assertNotIn(text, self.request()[1])


if __name__ == "__main__":
    unittest.main()
