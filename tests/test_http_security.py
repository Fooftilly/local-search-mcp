"""Real loopback dashboard HTTP and simulated DNS policy regression matrix.

All state and history paths are temporary, all HTTP requests target a locally
spawned disposable server, and no public DNS lookup or crawler is used.
These tests do not certify Chromium container/network isolation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import ExitStack
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

import admin_ui
import domain_policy as policy


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(temp.name) / "history.jsonl"),
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        settings.start()
        self.addCleanup(settings.stop)

        self.server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0), admin_ui.Handler)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        dashboard_port = patch.object(admin_ui, "PORT", self.port)
        dashboard_port.start()
        self.addCleanup(dashboard_port.stop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def exchange(self, method, path="/", fields=None, headers=None):
        """Speak actual HTTP to a fresh ephemeral loopback listener."""
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            body = urlencode(fields).encode("utf-8") if fields is not None else None
            request_headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if headers:
                request_headers.update(headers)
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, payload, dict(response.getheaders())
        finally:
            connection.close()

    def post(self, domain, action, *, csrf=None, headers=None, new_domain=""):
        fields = {
            "domain": domain,
            "action": action,
            "csrf": admin_ui.CSRF if csrf is None else csrf,
            "new_domain": new_domain,
        }
        if action in ("allow", "deny"):
            fields["expires_minutes"] = "0"
        return self.exchange("POST", "/domain", fields, headers=headers)[0]

    def test_pending_allow_deny_rename_delete_over_http(self):
        """A remote fetch queues requests; only authenticated local POSTs decide."""
        result = policy.evaluate_fetch_url("https://docs.python.org/guide?token=do-not-store")
        self.assertEqual(result["status"], "approval_required")
        before = {entry["domain"]: entry for entry in policy.entries()}
        self.assertEqual(before["docs.python.org"]["status"], "pending")
        self.assertNotIn("do-not-store", repr(before))

        status, page, _ = self.exchange("GET", "/?q=docs.python.org")
        self.assertEqual(status, 200)
        self.assertIn(b"docs.python.org", page)
        self.assertNotIn(b"do-not-store", page)
        # No GET endpoint or MCP request is permitted to mutate approvals.
        self.assertEqual(self.exchange("GET", "/domain?action=allow&domain=docs.python.org")[0], 404)
        self.assertEqual({r["domain"]: r["status"] for r in policy.entries()}["docs.python.org"], "pending")

        self.assertEqual(self.post("docs.python.org", "allow"), 200)
        self.assertEqual({r["domain"]: r["status"] for r in policy.entries()}["docs.python.org"], "allowed")
        self.assertEqual(self.post("docs.python.org", "deny"), 200)
        self.assertEqual({r["domain"]: r["status"] for r in policy.entries()}["docs.python.org"], "denied")
        self.assertEqual(self.post("docs.python.org", "rename", new_domain="archive.python.org"), 200)
        rows = {r["domain"]: r["status"] for r in policy.entries()}
        self.assertNotIn("docs.python.org", rows)
        self.assertEqual(rows["archive.python.org"], "denied")
        self.assertEqual(self.post("archive.python.org", "delete"), 200)
        self.assertNotIn("archive.python.org", {r["domain"] for r in policy.entries()})
        with policy._db() as db:
            actions = [row["action"] for row in db.execute(
                "SELECT action FROM decisions WHERE domain IN (?,?) ORDER BY id",
                ("docs.python.org", "archive.python.org"),
            ).fetchall()]
        self.assertEqual(actions, ["allow", "deny", "rename", "delete"])

    def test_optional_24h_overview_is_read_only_bounded_and_private(self):
        policy.request("queued.example.org", "https://queued.example.org/SECRET")
        baseline = {r["domain"]: r["status"] for r in policy.entries()}
        for invalid in ("unexpected", "true", "-1", "1%26overview=0"):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.exchange(
                    "GET", "/live?view=policy&overview=" + invalid)[0], 400)
        self.assertEqual(self.exchange(
            "GET", "/live?view=policy&overview=1&overview=0")[0], 400)
        self.assertEqual(self.exchange(
            "GET", "/live?view=history&overview=1")[0], 400)
        self.assertEqual(self.exchange(
            "GET", "/live?view=policy&overview=1",
            headers={"Origin": "http://other.example"})[0], 403)
        default_status, default_bytes, _ = self.exchange("GET", "/live?view=policy")
        self.assertEqual(default_status, 200)
        default = json.loads(default_bytes)
        self.assertNotIn("operations_html", default)
        self.assertNotIn("activity_summary_html", default)
        status, body, _ = self.exchange("GET", "/live?view=policy&overview=1")
        self.assertEqual(status, 200)
        overview = json.loads(body)
        self.assertIn("Operations", overview["operations_html"])
        self.assertIn("activity_summary_html", overview)
        self.assertNotIn("SECRET", repr(overview))
        self.assertEqual(
            {r["domain"]: r["status"] for r in policy.entries()}, baseline)

    def test_host_origin_referer_fetch_metadata_and_csrf_fail_closed(self):
        """A forged browser request must not grant a domain approval."""
        origin = f"http://127.0.0.1:{self.port}"
        attacks = (
            {"Host": "evil.example"},
            {"Origin": "http://evil.example"},
            {"Origin": "null"},
            {"Origin": origin, "Referer": "http://evil.example/"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        )
        for headers in attacks:
            with self.subTest(headers=headers):
                self.assertEqual(self.post("docs.python.org", "allow", headers=headers), 403)
                self.assertNotIn("docs.python.org", {r["domain"] for r in policy.entries()})
        self.assertEqual(self.post("docs.python.org", "allow", csrf="invalid"), 403)
        self.assertNotIn("docs.python.org", {r["domain"] for r in policy.entries()})

        # Browsers are permitted to omit Origin on same-origin form submissions.
        self.assertEqual(self.post("docs.python.org", "allow", headers={
            "Origin": origin, "Sec-Fetch-Site": "same-origin",
            "Referer": origin + "/",
        }), 200)
        self.assertEqual({r["domain"]: r["status"] for r in policy.entries()}["docs.python.org"], "allowed")

    def test_all_dashboard_mutation_routes_reject_unauthenticated_browser_posts(self):
        # Keep this explicit inventory in sync with Handler.do_POST's allowlist.
        # A new management form must inherit the same Host/Origin/Fetch-Site
        # and CSRF checks as the domain-rule controls.
        routes = (
            "/domain", "/pending/clear", "/bulk", "/once",
            "/rules/preview", "/rules/apply", "/settings",
            "/upstream-settings", "/cache-settings",
            "/portable-settings/preview", "/portable-settings/apply",
        )
        writer_targets = (
            (admin_ui.safe_settings, "update_settings"),
            (admin_ui.upstream_settings, "update_settings"),
            (admin_ui.cache_settings, "update_settings"),
            (admin_ui.portable_settings, "apply"),
            (admin_ui.policy_transfer, "apply_import"),
            (admin_ui.policy, "clear_pending_requests"),
            (admin_ui.policy, "_stage_once_grant"),
            (admin_ui.policy, "_revoke_once_grant"),
            (admin_ui.policy, "bulk_decide"),
            (admin_ui.policy, "decide"),
        )
        with ExitStack() as stack:
            writers = [
                stack.enter_context(patch.object(module, name))
                for module, name in writer_targets
            ]
            for route in routes:
                for headers in (
                    {"Host": "remote.invalid"},
                    {"Origin": "http://remote.invalid"},
                    {"Sec-Fetch-Site": "cross-site"},
                ):
                    with self.subTest(route=route, headers=headers):
                        status, _, _ = self.exchange(
                            "POST", route, {"csrf": admin_ui.CSRF},
                            headers=headers)
                        self.assertEqual(status, 403)
                with self.subTest(route=route, barrier="csrf"):
                    status, _, _ = self.exchange(
                        "POST", route, {"csrf": "invalid"})
                    self.assertEqual(status, 403)
            for writer in writers:
                writer.assert_not_called()

    def test_health_endpoint_and_defense_headers(self):
        status, body, headers = self.exchange("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        bad, _, _ = self.exchange("GET", "/healthz", headers={"Host": "not-local.invalid"})
        self.assertEqual(bad, 403)

    def test_live_endpoint_tracks_pending_decisions_and_history(self):
        status, page, headers = self.exchange("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<tbody id="live-pending">', page)
        self.assertIn(b'<tbody id="live-decided">', page)
        self.assertIn(b'src="/dashboard-live.js"', page)
        self.assertIn(b'Enable desktop notifications', page)
        self.assertNotIn(b'<script>', page)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

        status, script, script_headers = self.exchange("GET", "/dashboard-live.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", script_headers["Content-Type"])
        self.assertIn(b"Notification.requestPermission()", script)
        self.assertIn(b"minNotificationGap = 60000", script)
        self.assertIn(b"body.contains(document.activeElement)", script)
        self.assertIn(b"lastTableHtml.get(body) === html", script)
        self.assertIn(b"navigator.locks.request(notificationLock", script)
        self.assertIn(b"window.crypto.subtle.digest(\"SHA-256\"", script)
        self.assertIn(b"window.localStorage.setItem(pendingAlertKey", script)
        self.assertIn(b"window.localStorage.setItem(seenDomainIdsKey", script)
        self.assertIn(b"window.localStorage.setItem(latestSnapshotKey", script)
        self.assertIn(b"owedIds.filter(id => current.has(id))", script)
        self.assertIn(b"Number.isSafeInteger(latest)", script)
        self.assertIn(b"snapshotUs <= latest", script)
        self.assertIn(b"retired.includes(snapshotRun)", script)
        self.assertIn(b'event.newValue !== "enabled"', script)
        self.assertIn(b"window.clearTimeout(pendingTimer)", script)
        self.assertIn(b"refreshQueue.then(", script)
        self.assertIn(b"activeController.abort()", script)
        self.assertIn(b"activeTabKeys()", script)
        self.assertIn(b"window.localStorage.removeItem(tabPresenceKey)", script)
        self.assertIn(b"window.localStorage.setItem(lastNotificationKey", script)
        self.assertIn(b"Keep only still-pending alerts owed", script)
        self.assertIn(b"window.addEventListener(\"storage\"", script)
        self.assertIn(b"startRefresh();", script)
        self.assertIn(b"data = await response.json();\n      } finally {\n        window.clearTimeout(abort);", script)
        self.assertNotIn(b"document.write(", script)

        policy.request("new.example.org", "https://new.example.org/private?secret=DO_NOT_LEAK")
        policy.request("other.example.org", "https://other.example.org/")
        code, data, headers = self.exchange("GET", "/live?view=policy")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        result = json.loads(data)
        self.assertEqual(result["counts"]["pending"], 2)
        self.assertRegex(result["snapshot_run"], r"^[a-f0-9]{32}$")
        self.assertIsInstance(result["snapshot_us"], int)
        self.assertGreater(result["snapshot_us"], 0)
        self.assertIn("new.example.org", result["pending_html"])
        self.assertEqual(set(result["pending_domains"]),
                         {"new.example.org", "other.example.org"})
        self.assertNotIn("DO_NOT_LEAK", str(result))
        self.assertNotIn("private", str(result))
        self.assertIn("csrf", result["pending_html"])
        self.assertNotIn("<script>", result["pending_html"])

        filtered = json.loads(self.exchange("GET",
                              "/live?view=policy&q=new.example.org")[1])
        self.assertIn("new.example.org", filtered["pending_html"])
        self.assertNotIn("other.example.org", filtered["pending_html"])
        # Notification counters remain unfiltered to detect new domains
        # even when the dashboard is showing a filtered pending queue.
        self.assertEqual(set(filtered["pending_domains"]),
                         {"new.example.org", "other.example.org"})

        self.assertEqual(self.post("new.example.org", "allow"), 200)
        after = json.loads(self.exchange("GET", "/live?view=policy")[1])
        self.assertEqual(after["counts"]["pending"], 1)
        self.assertGreater(after["snapshot_us"], result["snapshot_us"])
        self.assertEqual(after["snapshot_run"], result["snapshot_run"])
        self.assertIn("new.example.org", after["decided_html"])
        self.assertNotIn("new.example.org", after["pending_domains"])

        (Path(os.environ["LOCAL_SEARCH_MCP_HISTORY_FILE"])).write_text(
            json.dumps({"time_utc": datetime.now(timezone.utc).isoformat(),
                        "kind": "search", "status": "<script>alert(1)</script>",
                        "query": "<img src=x onerror=alert(1)>"}) + "\n",
            encoding="utf-8")
        history = json.loads(self.exchange("GET", "/live?view=history")[1])
        self.assertIn("&lt;script&gt;", history["history_html"])
        # Manually imported legacy file is not owner-verifiable, so the
        # source-backed query must remain unavailable (not guessed).
        self.assertNotIn("<img", history["history_html"])
        self.assertNotIn("onerror", history["history_html"])
        self.assertNotIn("<script>", history["history_html"])
        html_page = self.exchange("GET", "/history")[1]
        self.assertIn(b'<tbody id="live-history">', html_page)
        self.assertIn(b'&lt;script&gt;', html_page)

    def test_history_queries_show_by_default_and_survive_live_filter_refresh(self):
        from history_log import append_history
        long_query = ("Full Unicode café 猫 " + "x" * 240 +
                      ' <script>alert("x")</script>&' + chr(0x202e))
        self.assertLessEqual(len(long_query), 300)
        append_history({"time_utc": datetime.now(timezone.utc).isoformat(),
                        "kind": "search", "status": "success",
                        "backend": "SearXNG", "duration_ms": 10,
                        "query": long_query})
        append_history({"time_utc": datetime.now(timezone.utc).isoformat(),
                        "kind": "fetch", "status": "success", "duration_ms": 12,
                        "url": "https://docs.example.org/private?secret=yes"})
        base_status, normal, _ = self.exchange("GET", "/history")
        self.assertEqual(base_status, 200)
        self.assertIn(b"Full Unicode", normal)
        self.assertNotIn(b"private?secret", normal)
        self.assertIn(b'checked> Show search queries', normal)
        live_normal = json.loads(self.exchange("GET", "/live?view=history")[1])
        self.assertIn("Full Unicode", live_normal["history_html"])
        uri = "/history?hours=24&kind=search&show_queries=1"
        code, revealed, headers = self.exchange("GET", uri)
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        visible = revealed.decode("utf-8")
        self.assertIn("Full Unicode café 猫", visible)
        self.assertIn("x" * 240, visible)
        self.assertIn("&lt;script&gt;", visible)
        self.assertIn("&quot;x&quot;", visible)
        self.assertIn("&amp;", visible)
        self.assertNotIn("<script>alert", visible)
        self.assertNotIn(chr(0x202e), visible)
        self.assertIn("\\\\u202e", visible)  # actual bidi is escaped to inert text
        self.assertIn("Show search queries", visible)
        self.assertIn('value="1" checked', visible)
        self.assertIn('Search query / page URL / domain', visible)
        live = json.loads(self.exchange(
            "GET", "/live?view=history&kind=search&hours=24&show_queries=1")[1])
        self.assertIn("Full Unicode café 猫", live["history_html"])
        self.assertIn("&lt;script&gt;", live["history_html"])
        self.assertNotIn("<script>", live["history_html"])
        self.assertNotIn("Full Unicode", live["history_summary_html"])
        no_reveal = json.loads(self.exchange(
            "GET", "/live?view=history&kind=search&hours=24&show_queries=0")[1])
        self.assertNotIn("Full Unicode", str(no_reveal))
        fetch_only = json.loads(self.exchange(
            "GET", "/live?view=history&kind=fetch&show_queries=1")[1])
        self.assertNotIn("Full Unicode", str(fetch_only))
        self.assertIn("https://docs.example.org/private", fetch_only["history_html"])
        self.assertNotIn("private?secret", str(fetch_only))
        self.assertNotIn("secret=yes", str(fetch_only))
        self.assertNotIn("https://docs.example.org/private",
                         json.loads(self.exchange("GET", "/live?view=history&kind=fetch&show_queries=0")[1])["history_html"])
        for bad in ("2", "yes", "%3Cscript%3E", "1&show_queries=1"):
            self.assertEqual(self.exchange(
                "GET", "/history?show_queries=" + bad)[0], 400)
            self.assertEqual(self.exchange(
                "GET", "/live?view=history&show_queries=" + bad)[0], 400)

    def test_history_fetch_urls_default_visible_escaped_and_live(self):
        from history_log import append_history
        from urllib.parse import urlencode
        target = 'https://docs.example.org/a/<script>alert("x")</script>/new?token=SECRET#fragment'
        append_history({"time_utc": datetime.now(timezone.utc).isoformat(),
                        "kind": "fetch", "status": "success", "duration_ms": 1,
                        "backend": "Crawl4AI", "url": target})
        append_history({"time_utc": datetime.now(timezone.utc).isoformat(),
                        "kind": "page", "status": "success", "duration_ms": 2,
                        "backend": "Crawl4AI"})
        code, original, _ = self.exchange("GET", "/history?kind=fetch")
        self.assertEqual(code, 200)
        self.assertIn(b"docs.example.org", original)
        self.assertIn(b"/a/", original)
        code, reveal, headers = self.exchange(
            "GET", "/history?kind=fetch&hours=24&show_queries=1")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        html = reveal.decode()
        self.assertIn("https://docs.example.org/a/&lt;script&gt;", html)
        self.assertIn("&quot;x&quot;", html)
        self.assertNotIn("token=SECRET", html)
        self.assertNotIn("#fragment", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("Show search queries and page URLs", html)
        self.assertIn('name="show_queries" value="1" checked', html)
        url = "/live?view=history&kind=fetch&hours=24&show_queries=1"
        rows = json.loads(self.exchange("GET", url)[1])
        self.assertIn("https://docs.example.org/a/", rows["history_html"])
        self.assertNotIn("SECRET", str(rows))
        hidden = json.loads(self.exchange(
            "GET", "/live?view=history&kind=fetch&hours=24&show_queries=0")[1])
        self.assertNotIn("https://", hidden["history_html"])
        cont = json.loads(self.exchange(
            "GET", "/live?view=history&kind=page&show_queries=1")[1])
        self.assertNotIn("https://docs.example.org", str(cont))
        self.assertNotIn("document_id", str(cont))

    def test_history_page_and_live_each_sync_index_only_once(self):
        from history_log import append_history
        from datetime import datetime, timezone
        append_history({"time_utc": datetime.now(timezone.utc).isoformat(),
                        "kind": "fetch", "status": "success",
                        "url": "https://docs.example.org/private/SECRET?token=HIDDEN",
                        "duration_ms": 5})
        original = admin_ui.history_index.sync_history
        with patch.object(admin_ui.history_index, "sync_history",
                          wraps=original) as wrapped:
            code, rendered, _ = self.exchange("GET", "/history?kind=fetch&show_queries=0")
            wrapped.assert_called_once_with()
        self.assertEqual(code, 200)
        self.assertIn(b"1 matching history events", rendered)
        self.assertIn(b"docs.example.org", rendered)
        self.assertNotIn(b"/private/SECRET", rendered)
        with patch.object(admin_ui.history_index, "sync_history",
                          wraps=original) as wrapped:
            code, data, _ = self.exchange(
                "GET", "/live?view=history&kind=fetch&show_queries=1")
            wrapped.assert_called_once_with()
        self.assertEqual(code, 200)
        live = json.loads(data)
        self.assertIn("https://docs.example.org/private/SECRET",
                      live["history_html"])
        self.assertNotIn("token=HIDDEN", repr(live))
        self.assertEqual(live["history_page"], 1)
        self.assertIn("1 matching history events", live["history_pages_html"])
        with patch.object(admin_ui.history_index,
                          "dashboard_history_snapshot",
                          side_effect=OSError("unavailable")):
            code, rendered, _ = self.exchange("GET", "/history?kind=fetch")
            self.assertEqual(code, 200)
            self.assertIn(b"Indexed history is unavailable", rendered)
            self.assertIn(b'<tbody id="live-history">', rendered)
            self.assertIn(b"History pages unavailable", rendered)
            self.assertNotIn(b"1 matching history events", rendered)
            code, data, _ = self.exchange(
                "GET", "/live?view=history&kind=fetch")
            self.assertEqual(code, 200)
            live = json.loads(data)
            self.assertIn("Indexed history is unavailable",
                          live["history_html"])
            self.assertIn("History pages unavailable",
                          live["history_pages_html"])
            self.assertNotIn("1 matching history events", repr(live))

    def test_history_pagination_filters_visibility_and_rotation_safety(self):
        from history_log import append_history, history_path
        from datetime import datetime, timezone
        import json
        when = datetime.now(timezone.utc).isoformat()
        for i in range(61):
            if i % 2:
                append_history({"time_utc": when, "kind": "fetch",
                                "status": "success", "duration_ms": 7,
                                "url": f"https://docs.example.org/private-page/{i:02d}"
                                       "?token=DO_NOT_LEAK"})
            else:
                append_history({"time_utc": when, "kind": "search",
                                "status": "success", "duration_ms": 10,
                                "query": f"PRIVATE-SEARCH-{i:02d}"})
        code, page, _ = self.exchange(
            "GET", "/history?kind=fetch&show_queries=1")
        self.assertEqual(code, 200)
        self.assertIn(b'Showing 1', page)
        self.assertIn(b'30 matching history events', page)
        self.assertIn(b'Next 25</a>', page)
        self.assertIn(b'/private-page/', page)
        self.assertNotIn(b'token=DO_NOT_LEAK', page)
        self.assertIn(b'show_queries=1', page)
        status, hidden, _ = self.exchange(
            "GET", "/history?kind=fetch&page=2&show_queries=0")
        self.assertEqual(status, 200)
        self.assertIn(b'Showing 26', hidden)
        self.assertNotIn(b'/private-page/', hidden)
        self.assertIn(b'docs.example.org', hidden)
        self.assertIn(b'Previous 25</a>', hidden)
        status, revealed, _ = self.exchange(
            "GET", "/history?kind=fetch&page=2&show_queries=1")
        self.assertEqual(status, 200)
        self.assertIn(b'/private-page/', revealed)
        self.assertNotIn(b'token=DO_NOT_LEAK', revealed)
        self.assertIn(b'page=1', revealed)
        self.assertIn(b'show_queries=1', revealed)

        status, data, _ = self.exchange(
            "GET", "/live?view=history&kind=fetch&page=2&show_queries=1")
        self.assertEqual(status, 200)
        live = json.loads(data)
        self.assertEqual(live["history_page"], 2)
        self.assertEqual(live["history_html"].count("<tr>"), 5)
        self.assertIn("page 2 of 2", live["history_pages_html"])
        self.assertIn("https://docs.example.org/private-page/",
                      live["history_html"])
        self.assertNotIn("DO_NOT_LEAK", repr(live))

        all_history = json.loads(self.exchange(
            "GET", "/live?view=history&page=3&show_queries=0")[1])
        self.assertEqual(all_history["history_page"], 3)
        self.assertEqual(all_history["history_html"].count("<tr>"), 11)
        self.assertIn("page 3 of 3", all_history["history_pages_html"])
        self.assertNotIn("PRIVATE-SEARCH", repr(all_history))
        with self.subTest("pagination after rotation"):
            self.assertEqual(self.exchange("GET", "/history?page=4")[0], 200)
            # Incoming page 4 is clamped to the last valid page.
            self.assertIn(b'data-history-page="3"',
                          self.exchange("GET", "/history?page=4")[1])
            source = history_path()
            source.rename(source.with_name(source.name + ".1"))
            append_history({"time_utc": when, "kind": "fetch",
                            "status": "success", "duration_ms": 1,
                            "url": "https://other.example.org/new-page"})
            # Old records are still indexed while retained rotation exists.
            self.assertIn(b'data-history-page="3"',
                          self.exchange("GET", "/history?page=4")[1])
            source.with_name(source.name + ".1").unlink()
            # The index intentionally retains redacted metadata from absent
            # sources until retention expires; original secrets fail closed.
            absent_source = json.loads(self.exchange(
                "GET", "/live?view=history&page=3&show_queries=1")[1])
            self.assertEqual(absent_source["history_page"], 3)
            self.assertNotIn("PRIVATE-SEARCH", repr(absent_source))
            self.assertNotIn("/private-page/", repr(absent_source))
            # Simulate retention expiry of the old indexed rows separately.
            # The remaining new event must clamp a formerly valid page 3.
            import sqlite3
            import history_index
            with sqlite3.connect(history_index.index_path()) as db:
                db.execute("DELETE FROM history WHERE domain != ?",
                           ("other.example.org",))
            after = json.loads(self.exchange(
                "GET", "/live?view=history&page=3&show_queries=1")[1])
            self.assertEqual(after["history_page"], 1)
            self.assertIn("other.example.org", after["history_html"])
            self.assertNotIn("PRIVATE-SEARCH", repr(after))
            self.assertNotIn("/private-page/", repr(after))

        # A history page beyond the pending dashboard's 20-page cap must
        # still be accepted by the same-origin live endpoint.
        with patch.object(admin_ui.history_index, "dashboard_history_snapshot",
                          return_value={"metrics": [
                              {"kind": "fetch", "status": "success",
                               "count": 526, "avg_duration_ms": 1}],
                              "rows": [], "page": 21, "total": 526}), \
             patch.object(admin_ui, "history_items_html",
                          return_value="<tr><td>page 21</td></tr>"):
            code, body, _ = self.exchange(
                "GET", "/live?view=history&kind=fetch&page=21")
        self.assertEqual(code, 200)
        historical = json.loads(body)
        self.assertEqual(historical["history_page"], 21)
        self.assertIn("page 21 of 22", historical["history_pages_html"])
        self.assertIn("page 21", historical["history_html"])

        for invalid in ("0", "-1", "4001", "hello", "1%20OR%201", ""):
            with self.subTest(page=invalid):
                self.assertEqual(self.exchange(
                    "GET", "/history?page=" + invalid)[0], 400)
                self.assertEqual(self.exchange(
                    "GET", "/live?view=history&page=" + invalid)[0], 400)
        for url in ("/history?page=1&page=2",
                    "/live?view=history&page=1&page=2"):
            self.assertEqual(self.exchange("GET", url)[0], 400)

    def test_history_window_filters_and_live_summary_bars_are_safe(self):
        from datetime import timedelta
        from history_log import append_history
        now = datetime.now(timezone.utc)
        append_history({"time_utc": (now - timedelta(hours=4)).isoformat(),
                        "kind": "fetch", "status": "success",
                        "url": "https://docs.example.org/private/SECRET",
                        "duration_ms": 40})
        append_history({"time_utc": now.isoformat(), "kind": "fetch",
                        "status": "blocked",
                        "url": "https://docs.example.org/SECRET?token=HIDDEN",
                        "duration_ms": 10})
        append_history({"time_utc": now.isoformat(), "kind": "search",
                        "status": "success", "query": "PRIVATE_QUERY"})
        url = "/history?hours=1&kind=fetch&domain=docs.example.org&show_queries=0"
        status, page, headers = self.exchange("GET", url)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b'name="hours" value="1" checked required', page)
        self.assertIn(b'<tbody id="live-history-summary">', page)
        self.assertIn(b'<progress aria-label="fetch blocked count"', page)
        self.assertIn('<td class="domain">docs.example.org</td><td>—</td><td>10.0</td>'.encode(), page)
        self.assertIn('<td class="domain">docs.example.org</td><td>—</td><td>10.0</td>',
                      json.loads(self.exchange(
                          "GET", "/live?view=history&hours=1&kind=fetch&show_queries=0")[1]
                      )["history_html"])
        for secret in (b"PRIVATE_QUERY", b"SECRET", b"HIDDEN"):
            self.assertNotIn(secret, page)

        code, payload, _ = self.exchange(
            "GET", "/live?view=history&hours=1&kind=fetch&domain=docs.example.org&show_queries=0")
        self.assertEqual(code, 200)
        live = json.loads(payload)
        self.assertEqual(live["history_html"].count("<tr>"), 1)
        self.assertIn("blocked", live["history_html"])
        self.assertNotIn("success", live["history_html"])
        self.assertIn('<progress aria-label="fetch blocked count"',
                      live["history_summary_html"])
        self.assertNotIn("success", live["history_summary_html"])
        for secret in ("PRIVATE_QUERY", "SECRET", "HIDDEN"):
            self.assertNotIn(secret, str(live))

        all_rows = json.loads(self.exchange(
            "GET", "/live?view=history&hours=24&kind=fetch&show_queries=0")[1])
        self.assertEqual(all_rows["history_html"].count("<tr>"), 2)
        self.assertIn("fetch success", all_rows["history_summary_html"])
        for bad in ("-1", "2", "9999", "true", "24%3Cscript%3E", "xyz"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.exchange("GET", "/history?hours=" + bad)[0], 400)
                self.assertEqual(
                    self.exchange("GET", "/live?view=history&hours=" + bad)[0], 400)
        # The policy view's live endpoint ignores unrelated history filters.
        self.assertEqual(self.exchange(
            "GET", "/live?view=policy&hours=9999")[0], 200)

    def test_invalid_history_text_filters_keep_escaped_form_and_navigation(self):
        for key, value in (("domain", "https://docs.example.org/private"),
                           ("domain", "docs.example.org/"),
                           ("domain", "docs.example.org "),
                           ("status", "<img src=x onerror=alert(1)>"),
                           ("kind", "unsupported")):
            with self.subTest(key=key, value=value):
                params = urlencode({key: value, "hours": "24"})
                status, body, _ = self.exchange("GET", "/history?" + params)
                self.assertEqual(status, 400)
                self.assertIn(b'<nav>', body)
                self.assertIn(b'<form method="get" action="/history">', body)
                self.assertIn(b'role="alert"', body)
                self.assertIn(b'Invalid history filter', body)
                self.assertIn(b'name="hours" value="24" checked required', body)
                self.assertIn(value.replace("&", "&amp;")
                                  .replace("<", "&lt;").replace(">", "&gt;")
                                  .replace('"', "&quot;").encode(), body)
                self.assertNotIn(b'Indexed history is unavailable', body)
                if key == "status":
                    self.assertNotIn(b'<img src=x onerror=', body)
                # Strict API clients still get a 400, not a misleading empty
                # success response or an SQL query for unsupported input.
                code, _, _ = self.exchange("GET",
                                           "/live?view=history&" + params)
                self.assertEqual(code, 400)

    def test_invalid_retention_configuration_keeps_history_http_response(self):
        with patch.dict(os.environ, {
            "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "0"
        }):
            status, body, _ = self.exchange("GET", "/history")
            self.assertEqual(status, 200)
            self.assertIn(b"Indexed history is unavailable", body)
            self.assertNotIn(b"Traceback", body)
            live_status, payload, _ = self.exchange("GET", "/live?view=history")
            self.assertEqual(live_status, 200)
            self.assertIn("Indexed history is unavailable",
                          json.loads(payload)["history_html"])

    def test_live_endpoints_reject_cross_site_and_invalid_views(self):
        baseline = len(policy.entries())
        for endpoint in ("/live?view=policy", "/live?view=history",
                         "/dashboard-live.js"):
            for headers in (
                {"Host": "attacker.example"},
                {"Sec-Fetch-Site": "cross-site"},
                {"Sec-Fetch-Site": "same-site"},
                {"Origin": "http://attacker.example"},
                {"Referer": "http://attacker.example/"},
            ):
                with self.subTest(endpoint=endpoint, headers=headers):
                    code, _, _ = self.exchange("GET", endpoint, headers=headers)
                    self.assertEqual(code, 403)
        self.assertEqual(self.exchange("GET", "/live?view=unexpected")[0], 400)
        self.assertEqual(self.exchange("GET", "/live?view=policy", headers={
            "Sec-Fetch-Site": "same-origin",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Referer": f"http://127.0.0.1:{self.port}/",
        })[0], 200)
        self.assertEqual(len(policy.entries()), baseline)

    def test_bad_domain_input_has_no_side_effect(self):
        for domain in ("co.uk", "localhost", "http://example.com", "127.0.0.1"):
            with self.subTest(domain=domain):
                self.assertEqual(self.post(domain, "allow"), 400)
                self.assertNotIn(domain, {r["domain"] for r in policy.entries()})


    def test_bulk_operator_post_requires_csrf_origin_and_fresh_request_id(self):
        policy.request("new.example.org",
                       "https://new.example.org/private/TOKEN?password=PRIVATE")
        policy.request("other.example.org", "https://other.example.org/")
        selected = next(row for row in policy.entries()
                        if row["domain"] == "new.example.org")
        token = selected["domain"] + "|" + selected["request_id"]
        status, page, _ = self.exchange("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'action="/bulk"', page)
        self.assertIn(b'form="bulk-form"', page)
        self.assertIn(token.encode(), page)
        self.assertNotIn(b"TOKEN", page)
        self.assertNotIn(b"PRIVATE", page)
        request = {"csrf": admin_ui.CSRF, "selected": token, "action": "allow",
                   "scope": "exact", "expires_minutes": "60",
                   "reason": "Approved locally"}

        self.assertEqual(self.exchange("GET", "/bulk")[0], 404)
        self.assertEqual(self.exchange("POST", "/bulk",
                                       request | {"csrf": "wrong"})[0], 403)
        self.assertEqual(self.exchange("POST", "/bulk", request,
                                       {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.exchange("POST", "/bulk", request,
                                       {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(next(r["status"] for r in policy.entries()
                              if r["domain"] == "new.example.org"), "pending")
        self.assertEqual(self.exchange("POST", "/bulk", request)[0], 200)
        row = next(r for r in policy.entries() if r["domain"] == "new.example.org")
        self.assertEqual(row["status"], "allowed")
        self.assertEqual(row["match_scope"], "exact")
        self.assertTrue(row["expires_at"])
        audit = policy.recent_decisions(search="new.example.org")[0]
        self.assertEqual(audit["request_id"], selected["request_id"])
        self.assertEqual(audit["reason"], "Approved locally")
        self.assertEqual(audit["requested_url"], "https://new.example.org")
        self.assertEqual(self.exchange("POST", "/bulk", request)[0], 400)

    def test_bulk_rejects_missing_and_invalid_payload_without_grant(self):
        policy.request("one.example.org", "https://one.example.org/")
        row = next(r for r in policy.entries() if r["domain"] == "one.example.org")
        fields = {"csrf": admin_ui.CSRF, "action": "allow",
                  "scope": "subdomains", "expires_minutes": "0",
                  "selected": row["domain"] + "|" + row["request_id"]}
        for bad in (fields | {"selected": ""},
                    fields | {"scope": "wrong"},
                    fields | {"reason": "bad\u0085control"},
                    fields | {"action": "delete"},
                    fields | {"expires_minutes": "9999"},
                    fields | {"selected": "one.example.org|" + "f" * 24}):
            with self.subTest(bad=bad):
                self.assertEqual(self.exchange("POST", "/bulk", bad)[0], 400)
                self.assertEqual(next(r["status"] for r in policy.entries()
                                      if r["domain"] == "one.example.org"), "pending")
        self.assertEqual(policy.recent_decisions(search="one.example.org"), [])


class DNSBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        settings = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(temp.name) / "history.jsonl"),
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        settings.start()
        self.addCleanup(settings.stop)

    def test_mixed_public_private_dns_answers_fail_closed(self):
        """A public A answer cannot override a private A/AAAA answer."""
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.23", 443))
        loopback_v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))
        for records in ((public, private), (public, loopback_v6), (private, public)):
            with self.subTest(records=records), patch.object(policy.socket, "getaddrinfo", return_value=records):
                result = policy.evaluate_fetch_url("https://example.com/a")
                self.assertEqual(result["status"], "blocked")
                self.assertIn("Non-public", result["reason"])
        with patch.object(policy.socket, "getaddrinfo", return_value=[public]):
            self.assertIsNone(policy.evaluate_fetch_url("https://example.com/ok"))

    def test_dns_outage_or_missing_addresses_blocks_fetch(self):
        with patch.object(policy.socket, "getaddrinfo", side_effect=socket.gaierror("offline")):
            self.assertEqual(policy.evaluate_fetch_url("https://example.com/a")["status"], "blocked")
        with patch.object(policy.socket, "getaddrinfo", return_value=[]):
            self.assertEqual(policy.evaluate_fetch_url("https://example.com/a")["status"], "blocked")

    def test_invalid_url_credentials_ports_and_sensitive_query_are_redacted(self):
        for url in (
            "https://name:password@example.com/private?token=topsecret#fragment",
            "http://example.com:8080/path?token=topsecret",
            "http://localhost/path?token=topsecret",
        ):
            with self.subTest(url=url), patch.object(policy.socket, "getaddrinfo") as resolver:
                result = policy.evaluate_fetch_url(url)
                resolver.assert_not_called()
                self.assertEqual(result["status"], "blocked")
                for secret in ("password", "topsecret", "fragment"):
                    self.assertNotIn(secret, repr(result))


if __name__ == "__main__":
    unittest.main()
