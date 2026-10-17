"""Real loopback dashboard HTTP and simulated DNS policy regression matrix.

All state and history paths are temporary, all HTTP requests target a locally
spawned disposable server, and no public DNS lookup or crawler is used.
These tests do not certify Chromium container/network isolation.
"""
from __future__ import annotations

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
        return self.exchange("POST", "/domain", {
            "domain": domain,
            "action": action,
            "csrf": admin_ui.CSRF if csrf is None else csrf,
            "new_domain": new_domain,
        }, headers=headers)[0]

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

    def test_health_endpoint_and_defense_headers(self):
        status, body, headers = self.exchange("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        bad, _, _ = self.exchange("GET", "/healthz", headers={"Host": "not-local.invalid"})
        self.assertEqual(bad, 403)

    def test_bad_domain_input_has_no_side_effect(self):
        for domain in ("co.uk", "localhost", "http://example.com", "127.0.0.1"):
            with self.subTest(domain=domain):
                self.assertEqual(self.post(domain, "allow"), 400)
                self.assertNotIn(domain, {r["domain"] for r in policy.entries()})


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
