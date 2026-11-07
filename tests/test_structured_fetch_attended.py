"""Attended host runner is opt-in and cannot widen approvals by itself."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from scripts import structured_fetch_attended as subject


URLS = [
    "https://www.peroniko.xyz/",
    "https://www.peroniko.xyz/bookmarks/art/",
    "https://www.peroniko.xyz/projects/",
]


class AttendedRunnerTests(unittest.TestCase):
    def test_cli_never_crawls_without_explicit_opt_in(self):
        with (patch.object(subject, "report",
                           side_effect=AssertionError("must not run")),
              contextlib.redirect_stderr(io.StringIO())):
            with self.assertRaises(SystemExit) as caught:
                subject.main(["--url", URLS[0]])
        self.assertEqual(caught.exception.code, 2)

    def test_rejects_new_hosts_and_noncanonical_urls_before_requests(self):
        candidates = (
            [], URLS[:2], [URLS[0]] * 3,
            [URLS[0], URLS[1], "https://unapproved.example/"],
            [URLS[0], URLS[1], "http://www.peroniko.xyz/extra"],
            [URLS[0], URLS[1], "https://www.peroniko.xyz:443/extra"],
            [URLS[0], URLS[1], "https://www.peroniko.xyz/extra?secret=x"],
            [URLS[0], URLS[1], "https://user:pass@www.peroniko.xyz/extra"],
        )
        with patch("domain_policy.is_domain_currently_allowed",
                   side_effect=AssertionError("bad inputs must not consult policy")):
            for values in candidates:
                with self.subTest(values=values):
                    with self.assertRaises(ValueError):
                        subject._require_preapproved(values)

    def test_rejects_unapproved_host_without_network_or_pending_requests(self):
        with patch("domain_policy.is_domain_currently_allowed",
                   return_value=False) as allowed:
            with self.assertRaises(ValueError):
                subject._require_preapproved(URLS)
        allowed.assert_called_once_with("www.peroniko.xyz")

    def test_attended_smoke_uses_exactly_three_guarded_fetches(self):
        requests = []
        def fake_fetch(url, **kwargs):
            requests.append((url, kwargs))
            method = kwargs["structured_spec"]["strategy"]
            return {"success": True, "structured_extraction": {
                "records": [{"heading": "public title"}],
                "rows_returned": 1, "truncated": False,
                "authorization_verified": False,
                "extraction_method": "crawl4ai_json_" + method,
            }}
        with (patch.object(subject, "_require_preapproved"),
              patch.object(subject, "_load_local_credentials"),
              patch.dict("sys.modules", {"bridge": type("FakeBridge", (), {
                  "web_fetch": staticmethod(fake_fetch)})()})):
            result = subject.report(URLS)
        self.assertTrue(result["passed"])
        self.assertEqual(len(requests), 3)
        self.assertEqual([x[0] for x in requests], URLS)
        self.assertEqual([x[1]["structured_spec"]["strategy"] for x in requests],
                         ["css", "xpath", "css"])
        self.assertTrue(all(x[1]["fresh"] is True for x in requests))
        self.assertNotIn("public title", json.dumps(result))

    def test_attended_smoke_does_not_claim_success_on_denied_or_invalid_data(self):
        def denied(url, **kwargs):
            return {"success": False, "status": "denied",
                    "reason": "SENSITIVE_PAGE_SOURCE_NOT_FOR_REPORT"}
        with (patch.object(subject, "_require_preapproved"),
              patch.object(subject, "_load_local_credentials"),
              patch.dict("sys.modules", {"bridge": type("FakeBridge", (), {
                  "web_fetch": staticmethod(denied)})()})):
            result = subject.report(URLS)
        self.assertFalse(result["passed"])
        self.assertTrue(all(x["status"] == "denied" for x in result["cases"]))
        self.assertNotIn("SENSITIVE_PAGE_SOURCE_NOT_FOR_REPORT", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
