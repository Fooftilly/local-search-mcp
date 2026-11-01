"""Opt-in SearXNG failure metadata: private audit and provider report."""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import bridge
import history_log
import provider_observations
import request_cache


class OptInFailureAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "events.jsonl"
        environment = patch.dict(os.environ, {
            "XDG_STATE_HOME": tmp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.path),
        })
        environment.start()
        self.addCleanup(environment.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)

    def mock_backend(self, payloads):
        original = httpx.Client
        calls = []
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=payloads[len(calls) - 1])
        def client_factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        return calls, client_factory

    def read(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_default_search_and_diagnostic_only_never_persist_provider_failures(self):
        backend = {"results": [{"title": "Notice", "url": "https://example.org/a",
                                "engine": "yahoo", "content": "SECRET_SNIPPET"}],
                   "unresponsive_engines": [
                       ["qwant", "PRIVATE_UPSTREAM_TOKEN"],
                       ["yahoo news", "PRIVATE_EXCEPTION_DETAILS"],
                   ]}
        calls, factory = self.mock_backend([backend, backend])
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext())):
            first = bridge.web_search("public test", fresh=True)
            second = bridge.web_search("public test", include_search_status=True)
        self.assertIsInstance(first, list)
        self.assertTrue(second["success"])
        self.assertEqual(second["status"], "degraded")
        self.assertEqual(calls, ["/search", "/search"])
        events = self.read()
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertNotIn("provider_diagnostics_opt_in", event)
            self.assertNotIn("provider_diagnostics", event)
        full = self.path.read_text()
        self.assertNotIn("PRIVATE_UPSTREAM_TOKEN", full)
        self.assertNotIn("PRIVATE_EXCEPTION_DETAILS", full)
        self.assertNotIn("SECRET_SNIPPET", full)

    def test_explicit_opt_in_safely_persists_only_labels_and_report(self):
        payload = {"results": [
            {"title": "Report", "url": "https://example.org/report",
             "content": "PRIVATE_SNIPPET", "engine": "yahoo"}],
            "unresponsive_engines": [
                ["qwant", "PRIVATE_TIMEOUT_WITH_URL_https://example.com/key"],
                ["qwant", "ANOTHER_SECRET"],
                ["naver (KO)", "PRIVATE_EXCEPTION"],
                ["SECRET_PROVIDER", "PRIVATE_EXCEPTION"],
                ["https://token=PRIVATE", "PRIVATE_EXCEPTION"],
                [3, "PRIVATE_EXCEPTION"],
            ],
        }
        calls, factory = self.mock_backend([payload])
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext())):
            out = bridge.web_search(
                "public terms", include_search_status=True,
                record_provider_diagnostics=True)
        self.assertEqual(out["status"], "degraded")
        self.assertEqual(len(calls), 1)
        event = self.read()[0]
        self.assertTrue(event["provider_diagnostics_opt_in"])
        record = event["provider_diagnostics"]
        self.assertEqual(record["coverage"], "partial")
        self.assertEqual(record["unresponsive_engines"], ["qwant", "naver (KO)"])
        self.assertEqual(record["suppressed_or_unparsed_names"], 3)
        self.assertTrue(record["truncated"])
        for forbidden in ("PRIVATE_EXCEPTION", "PRIVATE_SNIPPET",
                          "SECRET_PROVIDER", "https://token",
                          "PRIVATE_TIMEOUT_WITH_URL"):
            self.assertNotIn(forbidden, self.path.read_text())

        r = provider_observations.observations(1)
        self.assertEqual(r["opted_in_diagnostic_calls"], 1)
        self.assertEqual(r["reported_partial_provider_calls"], 1)
        counts = {x["name"]: x["reported_unresponsive_calls"]
                  for x in r["reported_unresponsive_engines"]}
        self.assertEqual(counts, {"naver (KO)": 1, "qwant": 1})
        self.assertEqual(r["provider_failure_coverage"], "per_call_opt_in_incomplete")
        self.assertEqual(r["provider_labels_suppressed"], 3)
        self.assertEqual(r["provider_uptime"], "not_measured")
        self.assertTrue(r["retention_or_scan_limited"])
        for forbidden in ("PRIVATE_EXCEPTION", "PRIVATE_SNIPPET", "SECRET_PROVIDER"):
            self.assertNotIn(forbidden, repr(r))

    def test_successful_recovery_retains_first_provider_failure_in_private_rollup(self):
        first = {
            "results": [],
            "unresponsive_engines": [["qwant", "SECRET_FIRST_ERROR"]],
        }
        second = {
            "results": [{
                "title": "Public", "url": "https://example.org/report",
                "engine": "duckduckgo", "content": "SECRET_SNIPPET",
            }],
            "unresponsive_engines": [],
        }
        calls, factory = self.mock_backend([first, second])
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep", return_value=None)):
            result = bridge.web_search(
                "public recovery", include_search_status=True,
                record_provider_diagnostics=True)
        self.assertEqual(calls, ["/search", "/search"])
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["search_status"]["coverage"], "no_failures_reported")
        event = self.read()[0]
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["provider_diagnostics"]["coverage"], "partial")
        self.assertEqual(event["provider_diagnostics"]["unresponsive_engines"],
                         ["qwant"])
        self.assertEqual(event["provider_diagnostics"]["reported_count"], 1)
        observed = provider_observations.observations(1)
        self.assertEqual(observed["reported_partial_provider_calls"], 1)
        self.assertEqual(observed["reported_unresponsive_engines"][0]["name"],
                         "qwant")
        self.assertNotIn("SECRET", self.path.read_text())

    def test_private_recovery_rollup_unions_bounded_labels_from_both_attempts(self):
        combined = history_log._private_search_provider_diagnostic({
            "coverage": "partial",
            "unresponsive_engines": ["duckduckgo"],
            "reported_count": 1, "unparsed_count": 0, "truncated": False,
            "recovery": {"attempted": True, "first_attempt": {
                "coverage": "partial",
                "unresponsive_engines": ["qwant", "PRIVATE_SECRET"],
                "reported_count": 2, "unparsed_count": 0, "truncated": False,
            }},
        })
        self.assertEqual(combined["coverage"], "partial")
        self.assertEqual(combined["unresponsive_engines"],
                         ["qwant", "duckduckgo"])
        self.assertEqual(combined["reported_count"], 3)
        self.assertGreaterEqual(combined["suppressed_or_unparsed_names"], 1)
        self.assertNotIn("PRIVATE_SECRET", repr(combined))

    def test_zero_results_failure_is_diagnostic_only_when_opted_in(self):
        payload = {
            "results": [],
            "unresponsive_engines": [["qwant", "PRIVATE_ERROR"]]
        }
        calls, factory = self.mock_backend([payload] * 4)
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext())):
            ordinary = bridge.web_search("available data", fresh=True)
            diagnostic = bridge.web_search(
                "available data", include_search_status=True,
                record_provider_diagnostics=True)
        self.assertEqual(len(calls), 4)
        self.assertEqual(ordinary["status"], "upstream_unavailable")
        self.assertNotIn("search_status", ordinary)
        self.assertFalse(diagnostic["success"])
        self.assertEqual(diagnostic["search_status"]["unresponsive_engines"],
                         ["qwant"])
        self.assertEqual(self.read()[-1]["status"], "error")
        self.assertEqual(self.read()[-1]["provider_diagnostics"]["unresponsive_engines"],
                         ["qwant"])
        r = provider_observations.observations(1)
        self.assertEqual(r["sampled_search_calls"], 2)
        self.assertEqual(r["non_success_search_calls"], 2)
        self.assertEqual(r["opted_in_diagnostic_calls"], 1)
        self.assertEqual(r["reported_unresponsive_engines"][0]["name"], "qwant")
        self.assertNotIn("PRIVATE_ERROR", self.path.read_text())

    def test_invalid_opt_in_rejected_before_any_network(self):
        for bad in (1, "yes", None, {}, []):
            with self.subTest(value=repr(bad)):
                with patch.object(bridge.httpx, "Client") as transport:
                    with self.assertRaisesRegex(ValueError, "record_provider_diagnostics"):
                        bridge.web_search("public", include_search_status=True,
                                          record_provider_diagnostics=bad)
                    transport.assert_not_called()
        with patch.object(bridge.httpx, "Client") as transport:
            with self.assertRaisesRegex(ValueError, "requires include_search_status"):
                bridge.web_search("public", record_provider_diagnostics=True)
            transport.assert_not_called()

    def test_untrusted_reported_status_does_not_enable_logging_of_arbitrary_content(self):
        malicious = {"coverage": "partial",
                     "unresponsive_engines": [
                         "PASSWORD", "alice@example.com", "tokensecret",
                         "duckduckgo (web)", "duckduckgo (web)",
                     ],
                     "reported_count": -200, "unparsed_count": "PRIVATE",
                     "truncated": "PRIVATE"}
        filtered = history_log._private_provider_diagnostic(malicious)
        self.assertEqual(filtered["unresponsive_engines"], ["duckduckgo (web)"])
        self.assertIsNone(filtered["reported_count"])
        self.assertIsNone(filtered["unparsed_count"])
        self.assertTrue(filtered["truncated"])
        self.assertNotIn("PRIVATE", repr(filtered))
        self.assertIsNone(history_log._private_provider_diagnostic(
            {"coverage": "other", "unresponsive_engines": ["yahoo"]}))

    def test_dashboard_html_only_shows_bounded_observations(self):
        from admin_ui import provider_observations_page
        history_log.append_history({
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "search", "status": "success", "duration_ms": 25,
            "query": "PRIVATE_SEARCH_QUERY",
            "provider_diagnostics_opt_in": True,
            "provider_diagnostics": {
                "coverage": "partial", "reported_count": 1,
                "truncated": False, "unresponsive_engines": ["qwant"]
            },
        })
        page = provider_observations_page(1)
        self.assertIn("qwant", page)
        self.assertIn("Opted-in SearXNG", page)
        self.assertNotIn("PRIVATE_SEARCH_QUERY", page)
        self.assertIn("not measured", page.lower())


if __name__ == "__main__":
    unittest.main()
