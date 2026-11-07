"""Offline regressions: deep-crawl compatibility probe never crawls or changes policy."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from scripts import deep_crawl_preflight as subject


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status, self.body = status, body

    def read(self, size):
        return self.body[:size]


class FakeConnection:
    created = []
    override = None

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.requests = []
        self.closed = False
        self.__class__.created.append(self)

    def request(self, method, path, *, body, headers):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        if self.override is not None:
            return self.override
        (_, _, payload, _), = self.requests
        return FakeResponse(body=payload)

    def close(self):
        self.closed = True


class DeepCrawlPreflightTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.created.clear()
        FakeConnection.override = None

    def test_default_only_health_and_never_posts(self):
        with (patch.object(subject, "get_json",
                           return_value={"status": "ok", "version": "0.9.5"}),
              patch.object(subject, "HTTPConnection",
                           side_effect=AssertionError("no config request"))):
            report = subject.report()
        self.assertEqual(report["config_status"], "not_probed")
        self.assertEqual(report["health"], "supported_and_healthy")
        self.assertEqual(report["discovered_url_authorization"], "not_verified")
        self.assertFalse(report["deep_crawl_ready"])
        self.assertEqual(report["public_crawls"], 0)
        self.assertEqual(report["external_requests"], 0)
        self.assertFalse(report["settings_modified"])

    def test_explicit_local_config_echo_is_only_serialization_evidence(self):
        with (patch.object(subject, "get_json",
                           return_value={"status": "ok", "version": "0.9.5"}),
              patch.object(subject, "HTTPConnection", FakeConnection),
              patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SECRET_TOKEN"}),
              contextlib.redirect_stdout(io.StringIO()) as output):
            code = subject.main(["--probe-config"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["config_status"], "fixed_config_echo_verified")
        self.assertFalse(report["deep_crawl_ready"])
        self.assertNotIn("SECRET_TOKEN", output.getvalue())
        self.assertEqual(len(FakeConnection.created), 1)
        conn = FakeConnection.created[0]
        self.assertEqual((conn.host, conn.port, conn.timeout),
                         ("127.0.0.1", 11235, 4))
        self.assertTrue(conn.closed)
        (method, path, data, headers), = conn.requests
        self.assertEqual((method, path), ("POST", "/config/dump"))
        self.assertEqual(headers["Authorization"], "Bearer SECRET_TOKEN")
        payload = json.loads(data)
        self.assertEqual(payload, subject._fixed_payload())
        self.assertNotIn("url", payload)
        self.assertNotIn("/crawl", path)
        self.assertFalse(payload["params"]["deep_crawl_strategy"]["params"][
            "include_external"])

    def test_rejected_config_is_not_claimed_to_be_disabled_flag(self):
        FakeConnection.override = FakeResponse(status=400, body=b"PRIVATE")
        with (patch.object(subject, "get_json",
                           return_value={"status": "ok", "version": "0.9.5"}),
              patch.object(subject, "HTTPConnection", FakeConnection),
              patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SECRET"}),
              contextlib.redirect_stdout(io.StringIO()) as output):
            code = subject.main(["--probe-config"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["config_status"], "rejected_400_reason_unknown")
        self.assertFalse(report["deep_crawl_ready"])
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertTrue(FakeConnection.created[-1].closed)

    def test_missing_or_invalid_token_does_not_make_request(self):
        for value in ("", "A\nB", "\u20ac", "x" * 4097):
            with (self.subTest(token=value[:10]),
                  patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": value}),
                  patch.object(subject, "get_json",
                               return_value={"status": "ok", "version": "0.9.5"}),
                  patch.object(subject, "HTTPConnection",
                               side_effect=AssertionError("unexpected request"))):
                result = subject.report(probe_config=True)
            self.assertEqual(result["config_status"], "operator_token_required")

    def test_unhealthy_backend_does_not_make_request(self):
        for health in ({"status": "ok", "version": "0.9.4"}, {"status": "bad"}):
            with (self.subTest(health=health),
                  patch.object(subject, "get_json", return_value=health),
                  patch.object(subject, "HTTPConnection",
                               side_effect=AssertionError("unexpected request"))):
                report = subject.report(probe_config=True)
            self.assertNotEqual(report["health"], "supported_and_healthy")
            self.assertEqual(report["config_status"], "not_probed")

    def test_no_redirect_followup_and_invalid_oversize_echo_never_claims_valid(self):
        cases = [
            FakeResponse(status=302, body=b"SECRET"),
            FakeResponse(status=401, body=b"SECRET"),
            FakeResponse(status=200, body=b"{invalid"),
            FakeResponse(status=200, body=b"x" * (subject.MAX_RESPONSE_BYTES + 1)),
            FakeResponse(status=200, body=b"[]"),
            FakeResponse(status=200, body=json.dumps({
                "type": "CrawlerRunConfig", "params": {
                    "deep_crawl_strategy": {"type": "BFSDeepCrawlStrategy",
                        "params": {"max_pages": 999, "max_depth": 1,
                                   "include_external": False}}}}).encode()),
        ]
        for response in cases:
            FakeConnection.override = response
            with (self.subTest(status=response.status, body=response.body[:20]),
                  patch.object(subject, "HTTPConnection", FakeConnection),
                  patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": "SECRET"})):
                self.assertIn(subject._probe_config("SECRET"),
                              {"unavailable", "unexpected_config_echo"})
            self.assertTrue(FakeConnection.created[-1].closed)
            self.assertEqual(len(FakeConnection.created[-1].requests), 1)

    def test_only_fixed_strategy_and_budgets_count_as_echo(self):
        valid = subject._fixed_payload()
        self.assertTrue(subject._fixed_echo_matches(valid))
        variants = [
            [],
            {},
            {"type": "CrawlerRunConfig", "params": {}},
            {"type": "CrawlerRunConfig", "params": {
                "deep_crawl_strategy": {"type": "DFSDeepCrawlStrategy",
                    "params": {"max_pages": 2, "max_depth": 1,
                               "include_external": False}}}},
            {"type": "CrawlerRunConfig", "params": {
                "deep_crawl_strategy": {"type": "BFSDeepCrawlStrategy",
                    "params": {"max_pages": True, "max_depth": 1,
                               "include_external": False}}}},
            {"type": "CrawlerRunConfig", "params": {
                "deep_crawl_strategy": {"type": "BFSDeepCrawlStrategy",
                    "params": {"max_pages": 2, "max_depth": 1,
                               "include_external": True}}}},
        ]
        for value in variants:
            with self.subTest(value=repr(value)[:65]):
                self.assertFalse(subject._fixed_echo_matches(value))


if __name__ == "__main__":
    unittest.main()
