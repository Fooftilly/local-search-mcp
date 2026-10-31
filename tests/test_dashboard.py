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
        self.assertIn("Showing 200 pending requests per page", page)
        self.assertEqual(page.count('form="bulk-form" aria-label="Select r'), 200)
        filtered = admin_ui.render("trusted.example.net")
        self.assertIn('name="q" value="trusted.example.net"', filtered)
        self.assertIn('<td class="domain">trusted.example.net</td>', filtered)
        self.assertNotIn('<td class="domain">blocked.example.org</td>', filtered)
        self.assertNotIn('aria-label="Select r0.flood-example.org"', filtered)

    def test_triage_sort_is_bounded_stable_and_does_not_modify_permissions(self):
        with policy._db() as db:
            db.executemany("""INSERT INTO domains
                (domain,status,created_at,updated_at,request_count,request_id)
                VALUES (?,'pending',?,?,?,?)""", [
                (f"s{i:03d}.queue-example.org",
                 f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                 f"2026-02-01T00:{i // 60:02d}:{i % 60:02d}Z",
                 500 if i == 51 else 1,
                 f"{i:024x}")
                for i in range(207)
            ])
            db.commit()
        recent = policy.entries(status="pending", limit=200)
        oldest = policy.entries(status="pending", limit=200,
                                pending_sort="oldest")
        frequent = policy.entries(status="pending", limit=200,
                                  pending_sort="frequent")
        self.assertEqual(recent[0]["domain"], "s206.queue-example.org")
        self.assertEqual(oldest[0]["domain"], "s000.queue-example.org")
        self.assertEqual(frequent[0]["domain"], "s051.queue-example.org")
        for mode in policy.PENDING_SORT_MODES:
            first = policy.entries(status="pending", limit=200,
                                   pending_sort=mode)
            last = policy.entries(status="pending", limit=200, offset=200,
                                  pending_sort=mode)
            self.assertEqual((len(first), len(last)), (200, 7))
            self.assertEqual(len({x["domain"] for x in first + last}), 207)
        for bad in ("DROP TABLE domains", "", "RECENT", "oldest;--", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                policy.entries(status="pending", pending_sort=bad)
        with self.assertRaises(ValueError):
            policy.entries(status="decided", pending_sort="frequent")
        self.assertEqual(policy.pending_match_count(), 207)
        self.assertEqual(policy.counts()["pending"], 207)

    def test_frequent_ties_use_last_request_not_row_update(self):
        with policy._db() as db:
            db.executemany("""INSERT INTO domains
                (domain,status,created_at,updated_at,last_requested_at,
                 request_count,request_id)
                VALUES (?,'pending',?,?,?,?,?)""", [
                ("recent-activity.example.org", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z", "2026-05-05T00:00:00Z",
                 3, "a" * 24),
                ("stale-activity.example.org", "2026-01-02T00:00:00Z",
                 "2026-05-01T00:00:00Z", "2026-01-03T00:00:00Z",
                 3, "b" * 24),
                ("legacy-no-timestamp.example.org", "2026-01-01T00:00:00Z",
                 "2026-01-10T00:00:00Z", "", 3, "c" * 24),
            ])
            db.commit()
        ordered = [r["domain"] for r in policy.entries(
            status="pending", pending_sort="frequent")]
        self.assertEqual(ordered, [
            "recent-activity.example.org",
            "legacy-no-timestamp.example.org",
            "stale-activity.example.org",
        ])
        # Existing default remains based on row updated_at.
        self.assertEqual(policy.entries(status="pending")[0]["domain"],
                         "stale-activity.example.org")

    def test_triage_sort_survives_page_post_and_live_refresh(self):
        with policy._db() as db:
            now = policy.utcnow()
            db.executemany("""INSERT INTO domains
                (domain,status,created_at,updated_at,request_count,request_id)
                VALUES (?,'pending',?,?,?,?)""", [
                (f"t{i:03d}.queue-example.org", now, now,
                 999 if i == 51 else 1, f"{i:024x}")
                for i in range(205)
            ])
            db.commit()
        root = f"http://127.0.0.1:{self.server_port}"
        def request(path, fields=None):
            data = urlencode(fields).encode() if fields is not None else None
            try:
                with urlopen(Request(root + path, data=data,
                                     method="POST" if data else "GET"),
                             timeout=4) as response:
                    return response.status, response.read().decode()
            except HTTPError as exc:
                return exc.code, exc.read().decode()
        status, body = request("/?sort=frequent")
        self.assertEqual(status, 200)
        self.assertIn('data-review-sort="frequent"', body)
        self.assertIn('name="sort" value="frequent" checked required', body)
        self.assertIn('name="review_sort" value="frequent"', body)
        self.assertIn('href="/?q=&amp;page=2&amp;sort=frequent"', body)
        self.assertLess(body.index('aria-label="Select t051.queue-example.org"'),
                        body.index('aria-label="Select t000.queue-example.org"'))
        status, body = request("/?sort=oldest&q=t0")
        self.assertEqual(status, 200)
        self.assertIn('name="sort" value="oldest" checked required', body)
        self.assertIn('data-review-sort="oldest"', body)
        status, raw = request("/live?view=policy&sort=frequent&page=2")
        self.assertEqual(status, 200)
        import json
        snapshot = json.loads(raw)
        self.assertIn('page 2 of 2', snapshot["pending_pages_html"])
        self.assertIn('&amp;sort=frequent', snapshot["pending_pages_html"])
        tail = policy.entries(status="pending", limit=200, offset=200,
                              pending_sort="frequent")
        self.assertIn(tail[0]["domain"], snapshot["pending_html"])
        picked = tail[0]
        status, result = request("/domain", {
            "csrf": admin_ui.CSRF, "domain": picked["domain"],
            "request_id": picked["request_id"], "action": "deny",
            "scope": "exact", "expires_minutes": "60",
            "review_query": "t", "review_page": "2",
            "review_sort": "frequent"})
        self.assertEqual(status, 200)
        self.assertIn('data-review-page="2"', result)
        self.assertIn('data-review-sort="frequent"', result)
        self.assertIn('href="/?q=t&amp;page=2&amp;sort=frequent">Refresh</a>',
                      result)
        self.assertIn('name="review_sort" value="frequent"', result)
        self.assertEqual(next(x["status"] for x in policy.entries()
                              if x["domain"] == picked["domain"]), "denied")
        status, result = request("/domain", {
            "csrf": admin_ui.CSRF, "domain": picked["domain"],
            "request_id": picked["request_id"], "action": "allow",
            "scope": "exact", "expires_minutes": "60",
            "review_page": "2", "review_sort": "frequent"})
        self.assertEqual(status, 400)
        self.assertIn('data-review-sort="frequent"', result)
        self.assertEqual(next(x["status"] for x in policy.entries()
                              if x["domain"] == picked["domain"]), "denied")
        for invalid in ("random", "frequent;--", "MOST", "x" * 101):
            with self.subTest(invalid=invalid):
                self.assertEqual(request("/?sort=" + invalid)[0], 400)
                self.assertEqual(request(
                    "/live?view=policy&sort=" + invalid)[0], 400)
        self.assertEqual(request(
            "/?sort=oldest&sort=frequent")[0], 400)
        for malformed in ("sort=", "sort=oldest&sort=", "sort=&sort=frequent",
                          "sort=&sort="):
            with self.subTest(malformed=malformed):
                self.assertEqual(request("/?" + malformed)[0], 400)
                self.assertEqual(request("/live?view=policy&" + malformed)[0],
                                 400)

    def test_pending_pagination_exposes_later_rows_without_moving_decisions(self):
        with policy._db() as db:
            now = policy.utcnow()
            db.executemany("""INSERT INTO domains
                (domain,status,created_at,updated_at,request_id)
                VALUES (?,'pending',?,?,?)""", [
                (f"q{i:03d}.queue-example.org", now, now, f"{i:024x}")
                for i in range(425)
            ])
            db.commit()
        first = policy.entries(limit=200, status="pending")
        second = policy.entries(limit=200, status="pending", offset=200)
        third = policy.entries(limit=200, status="pending", offset=400)
        self.assertEqual((len(first), len(second), len(third)), (200, 200, 25))
        self.assertEqual(len({r["domain"] for r in first+second+third}), 425)
        page1 = admin_ui.render(page=1)
        page2 = admin_ui.render(page=2)
        page3 = admin_ui.render(page=3)
        self.assertIn('href="/?q=&amp;page=2"', page1)
        self.assertNotIn('rel="prev"', page1)
        self.assertIn('Showing 201–400 of 425', page2)
        self.assertIn('href="/?q=&amp;page=3"', page2)
        self.assertIn('Showing 401–425 of 425', page3)
        self.assertNotIn('rel="next"', page3)
        self.assertIn('data-review-page="2"', page2)
        self.assertIn('name="review_page" value="2"', page2)
        self.assertIn('aria-label="Select q200.queue-example.org"', page2)
        self.assertNotIn('aria-label="Select q000.queue-example.org"', page2)
        filtered = admin_ui.render("q2", page=1)
        self.assertIn('page 1 of 1', filtered)
        self.assertNotIn('rel="next"', filtered)
        self.assertEqual({r["domain"] for r in policy.entries(status="decided")},
                         {"example.com"})

    def test_final_page_clamps_after_last_pending_decision(self):
        with policy._db() as db:
            now = policy.utcnow()
            db.executemany("""INSERT INTO domains
                (domain,status,created_at,updated_at,request_id)
                VALUES (?,'pending',?,?,?)""", [
                (f"p{i:03d}.queue-example.org", now, now, f"{i:024x}")
                for i in range(201)
            ])
            db.commit()
        lone = policy.entries(limit=200, status="pending", offset=200)[0]
        endpoint = f"http://127.0.0.1:{self.server_port}/domain"
        request = Request(endpoint, data=urlencode({
            "csrf": admin_ui.CSRF, "domain": lone["domain"],
            "request_id": lone["request_id"], "action": "allow",
            "scope": "exact", "expires_minutes": "60",
            "review_page": "2"}).encode(), method="POST")
        with urlopen(request, timeout=4) as result:
            self.assertEqual(result.status, 200)
            page = result.read().decode()
        self.assertIn('data-review-page="1"', page)
        self.assertIn('page 1 of 1', page)
        self.assertIn('aria-label="Select p000.queue-example.org"', page)
        self.assertNotIn('name="review_page" value="2"', page)
        snapshot = admin_ui.live_snapshot(pending_page=2)
        self.assertEqual(snapshot["pending_page"], 1)
        self.assertIn('page 1 of 1', snapshot["pending_pages_html"])
        self.assertEqual(policy.pending_match_count(), 200)

    def test_review_page_survives_post_and_live_poll(self):
        with policy._db() as db:
            now = policy.utcnow()
            db.executemany("""INSERT INTO domains
                (domain,status,created_at,updated_at,request_id)
                VALUES (?,'pending',?,?,?)""", [
                (f"z{i:03d}.queue-example.org", now, now, f"{i:024x}")
                for i in range(210)
            ])
            db.commit()
        row = policy.entries(limit=200, status="pending", offset=200)[0]
        root = f"http://127.0.0.1:{self.server_port}"
        def req(path, data=None):
            request = Request(root + path,
                              data=urlencode(data).encode() if data is not None else None,
                              method="POST" if data is not None else "GET")
            try:
                with urlopen(request, timeout=4) as result:
                    return result.status, result.read().decode()
            except HTTPError as exc:
                return exc.code, exc.read().decode()
        code, page = req("/?page=2")
        self.assertEqual(code, 200)
        self.assertIn('data-review-page="2"', page)
        code, raw = req("/live?view=policy&q=&page=2")
        self.assertEqual(code, 200)
        import json
        live = json.loads(raw)
        self.assertIn('page 2 of 2', live["pending_pages_html"])
        self.assertIn(row["domain"], live["pending_html"])
        self.assertNotIn("z000.queue-example.org", live["pending_html"])
        code, page = req("/domain", {
            "csrf": admin_ui.CSRF, "domain": row["domain"],
            "request_id": row["request_id"], "action": "allow",
            "scope": "exact", "expires_minutes": "60",
            "review_page": "2"})
        self.assertEqual(code, 200)
        self.assertIn('data-review-page="2"', page)
        self.assertIn('name="review_page" value="2"', page)
        self.assertIn('href="/?q=&amp;page=2">Refresh</a>', page)
        self.assertEqual(next(r["status"] for r in policy.entries()
                              if r["domain"] == row["domain"]), "allowed")
        for invalid in ("0", "21", "-1", "5000", "2x", "True"):
            with self.subTest(invalid=invalid):
                self.assertEqual(req("/?page="+invalid)[0], 400)
                self.assertEqual(req("/live?view=policy&page="+invalid)[0], 400)
        code, page = req("/domain", {
            "csrf": admin_ui.CSRF, "domain": row["domain"],
            "request_id": row["request_id"], "action": "deny",
            "scope": "exact", "expires_minutes": "60",
            "review_page": "999"})
        self.assertEqual(code, 400)
        self.assertIn('data-review-page="1"', page)
        self.assertEqual(next(r["status"] for r in policy.entries()
                              if r["domain"] == row["domain"]), "allowed")

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

    def test_dashboard_radio_choices_keep_safe_defaults_and_existing_forms(self):
        policy.request("radio-review.example.net",
                       "https://radio-review.example.net/section")
        html = admin_ui.render()
        self.assertNotIn("<select", html)
        self.assertIn('<fieldset class="choice-field"><legend>Scope</legend>', html)
        self.assertIn('<fieldset class="choice-field"><legend>Duration</legend>', html)
        self.assertIn('name="sort" value="recent" checked required', html)
        self.assertIn('name="sort" value="frequent" required', html)
        self.assertIn('name="expires_minutes" value="0" required', html)
        self.assertNotIn('name="expires_minutes" value="0" checked', html)

        quick = html.split('<form class="quick-decision"', 1)[1].split(
            "</form>", 1)[0]
        self.assertIn('name="scope" value="exact" checked required', quick)
        self.assertIn('name="scope" value="subdomains" required', quick)
        self.assertIn('name="expires_minutes" value="60" checked required', quick)
        self.assertIn('name="expires_minutes" value="0" required', quick)
        self.assertIn('name="action" value="allow"', quick)
        self.assertIn('name="action" value="deny"', quick)
        self.assertIn('name="csrf"', quick)
        self.assertIn('name="request_id"', quick)

        once = html.split('<form class="once-action"', 1)[1].split(
            "</form>", 1)[0]
        self.assertIn('name="scope" value="exact" checked required', once)
        self.assertIn('name="expires_minutes" value="60" checked required', once)
        self.assertNotIn('name="expires_minutes" value="0"', once)
        self.assertIn('name="action" value="stage"', once)

        manual = html.split(
            '<summary>Add a domain manually</summary>', 1)[1].split(
            "</form>", 1)[0]
        self.assertIn('name="scope" value="exact" checked required', manual)
        self.assertIn('name="expires_minutes" value="60" required', manual)
        self.assertNotIn('name="expires_minutes" value="60" checked', manual)
        self.assertIn('name="expires_minutes" value="0" required', manual)
        self.assertIn('name="action" value="allow"', manual)

        bulk = html.split('<form id="bulk-form"', 1)[1].split(
            "</form>", 1)[0]
        self.assertIn('name="scope" value="exact" checked required', bulk)
        self.assertIn('name="expires_minutes" value="60" checked required', bulk)

        self.assertIn('input:checked + .choice-text',
                      admin_ui.STYLESHEET)
        self.assertIn('input:focus-visible + .choice-text',
                      admin_ui.STYLESHEET)

    def test_radio_helper_rejects_bad_defaults_and_escapes_labels(self):
        with self.assertRaises(ValueError):
            admin_ui.choice_buttons("scope", "Scope",
                                    admin_ui.SCOPE_CHOICES, "other")
        markup = admin_ui.choice_buttons(
            "scope", "<Scope>", (("x", "<Unsafe>"),), "x")
        self.assertIn("&lt;Scope&gt;", markup)
        self.assertIn("&lt;Unsafe&gt;", markup)
        self.assertNotIn("<Unsafe>", markup)

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

    def test_filtered_review_survives_single_bulk_once_and_errors(self):
        for domain in ("one.example.net", "other.example.net",
                       "bulk.example.net", "once.example.net"):
            policy.request(domain, "https://" + domain + "/private/path?secret=hidden")
        base = f"http://127.0.0.1:{self.server_port}"
        def submit(path, fields):
            request = Request(base + path,
                              data=urlencode({"csrf": admin_ui.CSRF, **fields}).encode(),
                              method="POST")
            try:
                with urlopen(request, timeout=3) as response:
                    return response.status, response.read().decode("utf-8")
            except HTTPError as exc:
                return exc.code, exc.read().decode("utf-8")

        with urlopen(base + "/?q=one.example.net", timeout=3) as response:
            filtered = response.read().decode("utf-8")
        self.assertIn('name="review_query" value="one.example.net"', filtered)
        self.assertIn('data-review-query="one.example.net"', filtered)
        self.assertIn('href="/?q=one.example.net&amp;page=1">Refresh</a>', filtered)
        self.assertIn('name="q" value="one.example.net"', filtered)
        self.assertNotIn('aria-label="Select other.example.net"', filtered)
        first = next(x for x in policy.entries(status="pending")
                     if x["domain"] == "one.example.net")
        fields = {"domain": first["domain"], "request_id": first["request_id"],
                  "action": "allow", "scope": "exact", "expires_minutes": "60",
                  "review_query": "one.example.net"}
        code, page = submit("/domain", fields)
        self.assertEqual(code, 200)
        self.assertIn('name="q" value="one.example.net"', page)
        self.assertIn('data-review-query="one.example.net"', page)
        self.assertIn('one.example.net', page)
        self.assertNotIn('aria-label="Select other.example.net"', page)
        self.assertEqual(next(x["status"] for x in policy.entries()
                              if x["domain"] == "one.example.net"), "allowed")

        code, page = submit("/domain", fields)
        self.assertEqual(code, 400, "stale request still fails closed")
        self.assertIn('name="q" value="one.example.net"', page)

        once = next(x for x in policy.entries(status="pending")
                    if x["domain"] == "once.example.net")
        code, page = submit("/once", {
            "domain": once["domain"], "request_id": once["request_id"],
            "action": "stage", "scope": "exact", "expires_minutes": "60",
            "review_query": "once.example.net"})
        self.assertEqual(code, 200)
        self.assertIn('name="q" value="once.example.net"', page)
        self.assertIn('name="review_query" value="once.example.net"', page)
        self.assertNotIn('aria-label="Select other.example.net"', page)
        # The grant-revocation form must also retain the active filter.
        self.assertIn('name="action" value="revoke"', page)
        code, page = submit("/once", {
            "domain": once["domain"], "action": "revoke",
            "review_query": "once.example.net"})
        self.assertEqual(code, 200)
        self.assertIn('name="q" value="once.example.net"', page)

        bulk = next(x for x in policy.entries(status="pending")
                    if x["domain"] == "bulk.example.net")
        code, page = submit("/bulk", {
            "selected": bulk["domain"] + "|" + bulk["request_id"],
            "action": "deny", "scope": "exact", "expires_minutes": "60",
            "review_query": "bulk.example.net"})
        self.assertEqual(code, 200)
        self.assertIn('name="q" value="bulk.example.net"', page)
        self.assertEqual(next(x["status"] for x in policy.entries()
                              if x["domain"] == "bulk.example.net"), "denied")

    def test_review_filter_is_presentation_only_and_escaped(self):
        policy.request("safe.example.net", "https://safe.example.net/")
        code, page = (None, None)
        base = f"http://127.0.0.1:{self.server_port}/domain"
        for injected, expected in (
            ('<img src=x onerror=alert(1)>', '&lt;img src=x onerror=alert(1)&gt;'),
            ('x' * 101, None),
            ('bad' + chr(10) + 'filter', None),
            ('bad' + chr(0x202e) + 'filter', None),
        ):
            with self.subTest(injected=injected):
                data = urlencode({
                    "domain": "safe.example.net", "action": "invalid",
                    "csrf": admin_ui.CSRF, "review_query": injected,
                }).encode()
                try:
                    with urlopen(Request(base, data=data, method="POST"), timeout=3) as response:
                        code, page = response.status, response.read().decode()
                except HTTPError as exc:
                    code, page = exc.code, exc.read().decode()
                self.assertEqual(code, 400)
                if expected:
                    self.assertIn('name="q" value="' + expected + '"', page)
                    self.assertNotIn(injected, page)
                else:
                    self.assertIn('name="q" value=""', page)
                self.assertEqual(next(x["status"] for x in policy.entries()
                                      if x["domain"] == "safe.example.net"), "pending")

    def test_reject_csrf(self):
        self.assertEqual(self.post(csrf='bad'),403)

if __name__=='__main__': unittest.main()
