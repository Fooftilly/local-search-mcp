"""Versioned rule-transfer invariants and real loopback preview/apply flows."""
from datetime import datetime, timedelta, timezone
from html import unescape
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import admin_ui
import domain_policy as policy
import policy_transfer as transfer
import request_cache


class PortableDomainRulesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name,
                           "ALLOWED_CRAWL_DOMAINS": ""})
        env.start()
        self.addCleanup(env.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)

    def payload(self, rules, **extra):
        return json.dumps({"schema": transfer.SCHEMA, "rules": rules, **extra})

    def rule(self, domain="docs.example.org", *, status="allowed",
             scope="exact", expiry=""):
        return dict(domain=domain, status=status, scope=scope,
                    expires_at=expiry)

    def test_redacted_export_and_fresh_install_round_trip(self):
        policy.decide("example.org", "allow", scope="subdomains")
        policy.decide("denied.example.org", "deny", scope="exact")
        policy.evaluate_fetch_url("https://queued.example.net/private?k=SECRET")
        private = policy._stage_once_grant("private.example.net")
        export = transfer.export_rules()
        self.assertEqual(export["schema"], transfer.SCHEMA)
        self.assertEqual(export["rules"], [
            self.rule("denied.example.org", status="denied"),
            self.rule("example.org", scope="subdomains")])
        dumped = json.dumps(export)
        for hidden in ("SECRET", "/private", "request_id", "grant_id",
                       private, "once_grants", "history", "reason", "token"):
            self.assertNotIn(hidden, dumped)
        fresh = tempfile.TemporaryDirectory()
        self.addCleanup(fresh.cleanup)
        with patch.dict(os.environ, {"XDG_STATE_HOME": fresh.name}):
            plan = transfer.preview_import(dumped)
            self.assertEqual(len(plan["add"]), 2)
            self.assertEqual(plan["conflicts"], [])
            self.assertEqual(transfer.apply_import(plan["canonical"], plan["state"], plan["ticket"]), 2)
            self.assertEqual(transfer.export_rules(), export)
            self.assertTrue(policy.is_domain_currently_allowed("safe.example.org"))
            self.assertFalse(policy.is_domain_currently_allowed("denied.example.org"))
    
    def test_preserves_expiry_and_narrowness(self):
        expires = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = self.payload([self.rule("docs.example.org", expiry=expires),
                            self.rule("example.net", status="denied",
                                      scope="subdomains")])
        preview = transfer.preview_import(doc)
        self.assertEqual(transfer.apply_import(doc, preview["state"], preview["ticket"]), 2)
        self.assertEqual(transfer.export_rules()["rules"],
                         [self.rule("docs.example.org", expiry=expires),
                          self.rule("example.net", status="denied",
                                    scope="subdomains")])
        self.assertTrue(policy.is_domain_currently_allowed("docs.example.org"))
        self.assertFalse(policy.is_domain_currently_allowed("child.docs.example.org"))
        self.assertFalse(policy.is_domain_currently_allowed("child.example.net"))
        self.assertEqual(transfer.preview_import(doc)["unchanged"],
                         ["docs.example.org", "example.net"])

    def test_invalid_imports_never_mutate(self):
        good = self.rule()
        cases = [
            "not json", "{}", self.payload([]),
            self.payload([good], version=2),
            self.payload([{**good, "token": "SECRET"}]),
            self.payload([{**good, "scope": "wildcard"}]),
            self.payload([{**good, "expires_at": "2099-01-01"}]),
            self.payload([{**good, "expires_at": "2020-01-01T00:00:00Z"}]),
            self.payload([self.rule("github.io")]),
            self.payload([self.rule("127.0.0.1")]),
            self.payload([good, good]),
            self.payload([{**good, "domain": "DOCS.EXAMPLE.ORG"}]),
            self.payload([{**good, "status": "pending"}]),
            self.payload([{**good, "expires_at": None}]),
            self.payload([good] * (transfer.MAX_RULES + 1)),
        ]
        for data in cases:
            with self.subTest(sample=data[:90]), self.assertRaises(ValueError):
                transfer.preview_import(data)
            with self.subTest(sample=data[:90]), self.assertRaises(ValueError):
                transfer.apply_import(data, "0" * 64, "0.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(policy.entries(), [])

    def test_conflict_refuses_every_rule_and_no_partial_insert(self):
        policy.decide("docs.example.org", "deny", scope="exact")
        doc = self.payload([
            self.rule("docs.example.org", status="allowed"),
            self.rule("new.example.org")])
        preview = transfer.preview_import(doc)
        self.assertEqual(preview["conflicts"], ["docs.example.org"])
        self.assertEqual(preview["add"], ["new.example.org"])
        with self.assertRaisesRegex(ValueError, "conflicts"):
            transfer.apply_import(doc, preview["state"], preview["ticket"])
        self.assertEqual({x["domain"] for x in policy.entries()},
                         {"docs.example.org"})

    def test_stale_preview_is_rejected_and_no_mutation_occurs(self):
        doc = self.payload([self.rule("docs.example.org")])
        preview = transfer.preview_import(doc)
        policy.decide("different.example.org", "deny")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            transfer.apply_import(doc, preview["state"], preview["ticket"])
        self.assertFalse(policy.is_domain_currently_allowed("docs.example.org"))
        with self.assertRaises(ValueError):
            transfer.apply_import(doc, "invalid", preview["ticket"])

    def test_import_replaces_pending_preserves_identity_and_audit(self):
        result = policy.evaluate_fetch_url(
            "https://docs.example.org/private?token=DONT_EXPORT")
        self.assertEqual(result["status"], "approval_required")
        pending = next(x for x in policy.entries() if x["domain"] == "docs.example.org")
        doc = self.payload([self.rule()])
        preview = transfer.preview_import(doc)
        self.assertEqual(transfer.apply_import(doc, preview["state"], preview["ticket"]), 1)
        row = next(x for x in policy.entries() if x["domain"] == "docs.example.org")
        self.assertEqual(row["request_id"], pending["request_id"])
        self.assertEqual(row["status"], "allowed")
        self.assertTrue(row["explicit_rule"])
        audit = policy.recent_decisions()[0]
        self.assertEqual(audit["action"], "allow")
        self.assertEqual(audit["request_id"], pending["request_id"])
        self.assertEqual(audit["requested_url"], "https://docs.example.org")
        self.assertEqual(audit["first_requested_at"], pending["first_requested_at"])
        self.assertEqual(audit["last_requested_at"], pending["last_requested_at"])
        self.assertEqual(audit["request_count"], pending["request_count"])
        self.assertNotIn("DONT_EXPORT", repr(audit))
        self.assertNotIn("DONT_EXPORT", json.dumps(transfer.export_rules()))

    def test_import_parent_reconciles_only_auto_pending_descendants(self):
        policy.evaluate_fetch_url("https://child.example.org/secret?k=HIDDEN")
        policy.decide("exception.example.org", "deny", scope="exact")
        doc = self.payload([self.rule("example.org", scope="subdomains")])
        plan = transfer.preview_import(doc)
        self.assertEqual(transfer.apply_import(doc, plan["state"], plan["ticket"]), 1)
        entries = {x["domain"]: x for x in policy.entries()}
        self.assertNotIn("child.example.org", entries)
        self.assertEqual(entries["exception.example.org"]["status"], "denied")
        self.assertTrue(policy.is_domain_currently_allowed("safe.example.org"))
        self.assertFalse(policy.is_domain_currently_allowed("exception.example.org"))
        self.assertIn("delete", [r["action"] for r in policy.recent_decisions()])

    def test_preview_confirmation_expires_and_rejects_swapped_data(self):
        source = self.payload([self.rule("docs.example.org")])
        preview = transfer.preview_import(source)
        changed = self.payload([self.rule("different.example.org")])
        with self.assertRaisesRegex(ValueError, "does not match"):
            transfer.apply_import(changed, preview["state"], preview["ticket"])
        with patch.object(transfer.time, "monotonic",
                          return_value=transfer.time.monotonic() + 1000):
            with self.assertRaisesRegex(ValueError, "expired"):
                transfer.apply_import(source, preview["state"], preview["ticket"])
        self.assertEqual(policy.entries(), [])

    def test_parent_denial_cannot_be_bypassed_by_imported_child_allow(self):
        policy.decide("example.org", "deny", scope="subdomains")
        doc = self.payload([self.rule("docs.example.org", status="allowed",
                                      scope="exact")])
        preview = transfer.preview_import(doc)
        self.assertEqual(preview["conflicts"], ["docs.example.org"])
        self.assertNotIn("docs.example.org", preview["add"])
        self.assertIn("Conflict", admin_ui.import_preview_html(preview))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            transfer.apply_import(doc, preview["state"], preview["ticket"])
        self.assertFalse(policy.is_domain_currently_allowed("docs.example.org"))

    def test_imported_descendant_keeps_pending_request_identity_and_count(self):
        policy.evaluate_fetch_url("https://z.a.example.org/first?token=PRIVATE")
        policy.evaluate_fetch_url("https://z.a.example.org/second?token=PRIVATE")
        before = next(x for x in policy.entries()
                      if x["domain"] == "z.a.example.org")
        rules = [self.rule("a.example.org", scope="subdomains"),
                 self.rule("z.a.example.org", scope="exact")]
        plan = transfer.preview_import(self.payload(rules))
        self.assertEqual(transfer.apply_import(plan["canonical"], plan["state"],
                                               plan["ticket"]), 2)
        after = next(x for x in policy.entries()
                     if x["domain"] == "z.a.example.org")
        self.assertEqual(after["request_id"], before["request_id"])
        self.assertEqual(after["request_count"], before["request_count"])
        self.assertEqual(after["last_requested_url"], before["last_requested_url"])
        self.assertTrue(after["explicit_rule"])
        self.assertNotIn("PRIVATE", repr(transfer.export_rules()))

    def test_expiration_rechecked_after_import_write_lock(self):
        start = datetime.now(timezone.utc).replace(microsecond=0)
        expires = (start + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = self.payload([self.rule(expiry=expires)])
        plan = transfer.preview_import(doc)
        # Validation before SQLite succeeds, but the target TTL crosses its
        # deadline before the write-lock-dependent recheck.
        with patch.object(transfer, "_now_dt", side_effect=[
                start, start + timedelta(days=3)]):
            with self.assertRaisesRegex(ValueError, "expired"):
                transfer.apply_import(doc, plan["state"], plan["ticket"])
        self.assertEqual(policy.entries(), [])

    def test_preview_explicit_statuses_name_conflict_domains(self):
        policy.decide("docs.example.org", "deny")
        doc = self.payload([self.rule("docs.example.org"),
                            self.rule("new.example.net")])
        preview = transfer.preview_import(doc)
        html = admin_ui.import_preview_html(preview)
        self.assertIn("docs.example.org", html)
        self.assertIn("new.example.net", html)
        self.assertIn("Conflict", html)
        self.assertIn("New", html)
        self.assertNotIn('action="/rules/apply"', html)

    def test_empty_export_is_versioned_and_contains_no_secrets(self):
        self.assertEqual(transfer.export_rules(),
                         {"schema": transfer.SCHEMA, "rules": []})


class TransferHTTPTests(PortableDomainRulesTests):
    def setUp(self):
        super().setUp()
        self.server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0),
                                                    admin_ui.Handler)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        patcher = patch.object(admin_ui, "PORT", self.port)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, *, data=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            body = urlencode(data).encode("utf-8") if data is not None else None
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  **(headers or {})})
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8"), dict(response.getheaders())
        finally:
            conn.close()

    def test_http_export_preview_and_confirm(self):
        policy.decide("docs.example.org", "deny", scope="exact")
        status, body, headers = self.request("GET", "/rules/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body)["rules"], [self.rule(status="denied")])
        page_status, page, _ = self.request("GET", "/")
        self.assertEqual(page_status, 200)
        self.assertIn("Validate and preview import", page)
        self.assertNotIn("grant_id", body)

        new = self.payload([self.rule("new.example.net")])
        preview_status, preview, _ = self.request(
            "POST", "/rules/preview",
            data={"csrf": admin_ui.CSRF, "rule_doc": new})
        self.assertEqual(preview_status, 200)
        self.assertIn("Domain rule import preview", preview)
        self.assertIn("New: 1", preview)
        self.assertNotIn("Type IMPORT", self.request("POST", "/rules/preview",
                             data={"csrf": "bad", "rule_doc": new})[1])
        self.assertNotIn("new.example.net", {x["domain"] for x in policy.entries()})
        match = re.search(r'name="state" value="([a-f0-9]{64})"', preview)
        self.assertIsNotNone(match)
        state = match.group(1)
        ticket_match = re.search(r'name="ticket" value="([0-9]+\.[a-f0-9]{64})"', preview)
        self.assertIsNotNone(ticket_match)
        ticket = ticket_match.group(1)
        # A confirmation for one preview cannot authorize a modified payload.
        self.assertEqual(self.request("POST", "/rules/apply", data={
            "csrf": admin_ui.CSRF, "state": state, "ticket": ticket,
            "rule_doc": self.payload([self.rule("unreviewed.example.net")]),
            "confirm": "IMPORT"})[0], 400)
        status, _, _ = self.request("POST", "/rules/apply", data={
            "csrf": admin_ui.CSRF, "state": state, "ticket": ticket,
            "rule_doc": new, "confirm": "IMPORT"})
        self.assertEqual(status, 200)
        self.assertTrue(policy.is_domain_currently_allowed("new.example.net"))
        self.assertEqual(self.request("POST", "/rules/apply", data={
            "csrf": admin_ui.CSRF, "state": state, "ticket": ticket,
            "rule_doc": new, "confirm": "IMPORT"})[0], 400)

    def test_http_accepts_valid_48kb_json_after_percent_encoding(self):
        # Newlines are legal JSON whitespace but cost three bytes (%0A)
        # each in form encoding; a 48 KB document easily exceeds the older
        # 128 KB Content-Length guard even when decoded size is valid.
        minimal = self.payload([self.rule("padded.example.net")])
        doc = minimal + "\n" * (transfer.MAX_JSON_BYTES - len(minimal))
        self.assertEqual(len(doc.encode("utf-8")), transfer.MAX_JSON_BYTES)
        encoded = urlencode({"csrf": admin_ui.CSRF, "rule_doc": doc})
        self.assertGreater(len(encoded.encode("utf-8")), 128_000)
        preview_status, preview, _ = self.request(
            "POST", "/rules/preview",
            data={"csrf": admin_ui.CSRF, "rule_doc": doc})
        self.assertEqual(preview_status, 200)
        self.assertIn("New: 1", preview)
        state = re.search(r'name="state" value="([a-f0-9]{64})"', preview)
        ticket = re.search(
            r'name="ticket" value="([0-9]+\\.[a-f0-9]{64})"', preview)
        self.assertIsNotNone(state)
        self.assertIsNotNone(ticket)
        status, _, _ = self.request("POST", "/rules/apply", data={
            "csrf": admin_ui.CSRF, "rule_doc": doc,
            "state": state.group(1), "ticket": ticket.group(1),
            "confirm": "IMPORT"})
        self.assertEqual(status, 200)
        self.assertTrue(policy.is_domain_currently_allowed("padded.example.net"))
        # The larger *decoded* document remains rejected by the JSON limit.
        oversize, _, _ = self.request(
            "POST", "/rules/preview",
            data={"csrf": admin_ui.CSRF, "rule_doc": doc + "\n"})
        self.assertEqual(oversize, 400)

    def test_http_origin_and_csrf_protections_and_no_get_mutations(self):
        doc = self.payload([self.rule()])
        for headers in ({"Origin": "http://evil.invalid"},
                        {"Sec-Fetch-Site": "cross-site"},
                        {"Host": "evil.invalid"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/rules/preview", data={
                    "csrf": admin_ui.CSRF, "rule_doc": doc},
                    headers=headers)[0], 403)
        self.assertEqual(self.request("GET", "/rules/apply")[0], 404)
        self.assertEqual(self.request("POST", "/rules/preview", data={
            "csrf": "bad", "rule_doc": doc})[0], 403)
        self.assertEqual(self.request("GET", "/rules/export",
                          headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(transfer.export_rules()["rules"], [])


if __name__ == "__main__":
    unittest.main()
