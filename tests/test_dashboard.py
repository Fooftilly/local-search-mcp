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
        data=urlencode({'domain':'python.org','action':'allow','csrf': admin_ui.CSRF if csrf is None else csrf}).encode()
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
        self.assertEqual(page.count('<tr><td class="domain">r'), 200)
        filtered = admin_ui.render("trusted.example.net")
        self.assertIn("trusted.example.net", filtered)

    def test_reject_csrf(self):
        self.assertEqual(self.post(csrf='bad'),403)

if __name__=='__main__': unittest.main()
