import os
import secrets
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

import admin_ui
import domain_policy as policy

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p=patch.dict(os.environ, {'XDG_STATE_HOME':self.tmp.name,'ALLOWED_CRAWL_DOMAINS':'example.com'})
        p.start();self.addCleanup(p.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),admin_ui.Handler)
        self.server.daemon_threads=True
        # Handler's Host validation intentionally only permits the configured PORT.
        self.server_port=self.server.server_address[1]
        pport=patch.object(admin_ui,'PORT',self.server_port);pport.start();self.addCleanup(pport.stop)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, headers=None, csrf=None):
        data=urlencode({'domain':'python.org','action':'allow','expires_minutes':'0',
                        'csrf': admin_ui.CSRF if csrf is None else csrf}).encode()
        url=f'http://127.0.0.1:{self.server_port}/domain'
        request=Request(url,data=data,headers=headers or {}, method='POST')
        try:
            with urlopen(request,timeout=3) as r:return r.status
        except HTTPError as exc:return exc.code

    def test_same_origin_and_missing_origin(self):
        self.assertEqual(self.post(),200)
        self.assertEqual(policy.request('python.org','https://python.org')[0],'allowed')

    def test_reject_wrong_origin(self):
        self.assertEqual(self.post({'Origin':'http://evil.example'}),403)

    def test_reject_cross_site_metadata(self):
        self.assertEqual(self.post({'Sec-Fetch-Site':'cross-site'}),403)

    def test_pending_flood_never_hides_saved_allow_and_deny(self):
        policy.decide("trusted.example.net", "allow")
        policy.decide("blocked.example.org", "deny")
        # Simulate an existing legacy queue, larger than the previous shared
        # 2000-row display limit. Avoid thousands of unnecessary HTTP calls.
        with policy._db() as db:
            now = policy.utcnow()
            db.executemany("""INSERT INTO domains
                (domain,status,created_at,updated_at)
                VALUES (?,'pending',?,?)""",
                [(f"r{i}.flood-example.org", now, now) for i in range(2105)])
            db.commit()
        self.assertEqual(policy.counts()["pending"], 2105)
        self.assertEqual(
            {r["domain"] for r in policy.entries(limit=2000, status="decided")},
            {"example.com", "trusted.example.net", "blocked.example.org"},
        )
        page = admin_ui.render()
        self.assertIn("trusted.example.net", page)
        self.assertIn("blocked.example.org", page)
        self.assertIn("Showing at most 200 pending entries", page)
        self.assertEqual(page.count('form="bulk-form" aria-label="Select r'), 200)
        filtered = admin_ui.render("trusted.example.net")
        self.assertIn('name="q" value="trusted.example.net"', filtered)
        self.assertIn('<td class="domain">trusted.example.net</td>', filtered)
        self.assertNotIn('<td class="domain">blocked.example.org</td>', filtered)
        self.assertNotIn('aria-label="Select r0.flood-example.org"', filtered)

    def test_clear_pending_requires_confirmation_and_preserves_decisions(self):
        policy.decide("trusted.example.net", "allow")
        policy.request("queued.example.net", "https://queued.example.net/")
        endpoint = f"http://127.0.0.1:{self.server_port}/pending/clear"
        def clear(confirm, csrf=admin_ui.CSRF, headers=None):
            data = urlencode({"confirm": confirm, "csrf": csrf}).encode()
            request = Request(endpoint, data=data, method="POST", headers=headers or {})
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status
            except HTTPError as exc:
                return exc.code
        self.assertEqual(clear("CLEAR", csrf="bad"), 403)
        self.assertEqual(clear("CLEAR", headers={"Origin": "http://evil.example"}), 403)
        self.assertEqual(clear("wrong"), 400)
        self.assertEqual(policy.counts()["pending"], 1)
        self.assertEqual(clear("CLEAR"), 200)
        self.assertEqual(policy.counts()["pending"], 0)
        self.assertEqual(policy.request("trusted.example.net",
                                        "https://trusted.example.net/")[0], "allowed")

    def test_quick_review_uses_current_pending_request_and_bounded_scope(self):
        domain = "reviewed.example.net"
        policy.request(domain, "https://reviewed.example.net/some/path")
        record = next(r for r in policy.entries(status="pending")
                      if r["domain"] == domain)
        url = f"http://127.0.0.1:{self.server_port}/domain"
        def submit(request_id, action):
            data = urlencode({
                "csrf": admin_ui.CSRF, "domain": domain,
                "request_id": request_id, "action": action,
                "scope": "exact", "expires_minutes": "60",
            }).encode()
            try:
                with urlopen(Request(url, data=data, method="POST"), timeout=3) as reply:
                    return reply.status
            except HTTPError as exc:
                return exc.code
        self.assertEqual(submit(record["request_id"], "allow"), 200)
        saved = next(r for r in policy.entries(status="decided")
                     if r["domain"] == domain)
        self.assertEqual(saved["status"], "allowed")
        self.assertEqual(saved["match_scope"], "exact")
        self.assertTrue(saved["expires_at"])
        self.assertEqual(submit(record["request_id"], "deny"), 400)
        self.assertEqual(submit("f" * 24, "deny"), 400)
        final = next(r for r in policy.entries(status="decided")
                     if r["domain"] == domain)
        self.assertEqual(final["status"], "allowed")

    def test_reject_csrf(self):
        self.assertEqual(self.post(csrf='bad'),403)

if __name__=='__main__': unittest.main()
