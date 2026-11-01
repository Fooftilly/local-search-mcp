"""Isolated local SearXNG operations are measured, not inferred from hits."""
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
import isolated_engine_observations as telemetry
import provider_observations
import request_cache


class IsolatedEngineObservationsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "events.jsonl"
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.path),
        })
        env.start()
        self.addCleanup(env.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)

    def _run(self, handler, **args):
        real_client = httpx.Client
        calls = []
        def counted(request):
            calls.append((request.url.path, dict(request.url.params)))
            return handler(request)
        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(counted),
                               trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep", return_value=None)):
            outcome = bridge.web_search("PRIVATE_USER_SEARCH", fresh=True, **args)
        return outcome, calls

    def _events(self):
        return [json.loads(row) for row in self.path.read_text().splitlines()]

    def test_explicit_isolated_search_records_local_operation_not_query_in_report(self):
        def handler(req):
            if req.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "duckduckgo", "enabled": True,
                     "categories": ["general"]}]})
            self.assertEqual(req.url.params.get("engines"), "duckduckgo")
            return httpx.Response(200, json={
                "results": [{"title": "Public", "url": "https://docs.python.org/3/",
                             "engine": "duckduckgo", "content": "SECRET_SNIPPET"}],
                "unresponsive_engines": []})
        result, calls = self._run(handler, engines="duckduckgo")
        self.assertIsInstance(result, list)
        self.assertEqual([p for p, _ in calls], ["/config", "/search"])
        rows = self._events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["query"], "PRIVATE_USER_SEARCH")
        operations = rows[0]["isolated_engine_operations"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(
            {key: operations[0][key] for key in ("engine", "source", "outcome")},
            {"engine": "duckduckgo", "source": "explicit", "outcome": "results"})
        self.assertGreaterEqual(operations[0]["roundtrip_ms"], 0)
        self.assertNotIn("SECRET_SNIPPET", str(operations))
        report = provider_observations.observations(24, now=datetime.now(timezone.utc))
        self.assertEqual(report["isolated_engine_operations"][0]["name"],
                         "duckduckgo")
        self.assertEqual(report["isolated_engine_operations"][0]["reported_results"], 1)
        self.assertEqual(report["isolated_engine_operations"][0]["local_operations"], 1)
        self.assertEqual(report["isolated_engine_operations"][0]["recovery_operations"], 0)
        self.assertNotIn("PRIVATE_USER_SEARCH", str(report))
        self.assertNotIn("SECRET_SNIPPET", str(report))

    def test_combined_search_hits_do_not_invent_provider_attempts(self):
        result, calls = self._run(
            lambda request: httpx.Response(200, json={
                "results": [{"title": "Public",
                             "url": "https://docs.python.org/3/",
                             "engine": "duckduckgo"}]}))
        self.assertEqual([p for p, _ in calls], ["/search"])
        self.assertIsInstance(result, list)
        self.assertNotIn("isolated_engine_operations", self._events()[0])
        report = provider_observations.observations(24)
        self.assertEqual(report["isolated_engine_operations"], [])
        self.assertEqual(report["observed_result_engines"][0]["name"], "duckduckgo")

    def test_bounded_recovery_observed_separately_from_failed_combined_search(self):
        search_calls = []
        def handler(request):
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "duckduckgo", "enabled": True,
                     "categories": ["general"]}]})
            search_calls.append(dict(request.url.params))
            if len(search_calls) == 1:
                return httpx.Response(200, json={
                    "results": [],
                    "unresponsive_engines": [["duckduckgo", "PRIVATE_API_TOKEN"]]})
            return httpx.Response(200, json={"results": [{
                "title": "Public", "url": "https://docs.python.org/3/",
                "engine": "duckduckgo"}]})
        result, calls = self._run(handler, include_search_status=True)
        self.assertTrue(result["success"])
        self.assertEqual(len(search_calls), 2)
        operations = self._events()[0]["isolated_engine_operations"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["source"], "isolated_recovery")
        self.assertEqual(operations[0]["outcome"], "results")
        self.assertNotIn("PRIVATE_API_TOKEN", str(operations))
        report = provider_observations.observations(24)
        self.assertEqual(report["isolated_engine_operations"][0]["recovery_operations"], 1)

    def test_explicit_same_query_recovery_is_excluded_from_direct_denominator(self):
        calls = []
        def handler(request):
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "duckduckgo", "enabled": True,
                     "categories": ["general"]}]})
            calls.append(request.url.path)
            if len(calls) == 1:
                return httpx.Response(200, json={
                    "results": [], "unresponsive_engines": ["duckduckgo"]})
            return httpx.Response(200, json={"results": [{
                "title": "Public", "url": "https://docs.python.org/3/",
                "engine": "duckduckgo"}]})
        result, _ = self._run(handler, engines="duckduckgo")
        self.assertIsInstance(result, list)
        operations = self._events()[0]["isolated_engine_operations"]
        self.assertEqual([x["source"] for x in operations],
                         ["explicit", "same_query_recovery"])
        self.assertEqual([x["outcome"] for x in operations],
                         ["reported_unresponsive", "results"])
        metrics = provider_observations.observations(24)["isolated_engine_operations"][0]
        self.assertEqual(metrics["local_operations"], 2)
        self.assertEqual(metrics["recovery_operations"], 1)
        self.assertEqual(metrics["direct_operations"], 1)
        self.assertEqual(metrics["direct_verified_results"], 0)
        self.assertIsNone(metrics["direct_verified_result_mean_ms"])

    def test_empty_reported_unresponsive_and_http_error_are_not_counted_as_hits(self):
        for response, expected in [
            (httpx.Response(200, json={
                "results": [], "unresponsive_engines": ["duckduckgo"]}),
             "reported_unresponsive"),
            (httpx.Response(403, text="PRIVATE_ERROR"), "http_error"),
        ]:
            with self.subTest(expected=expected):
                with telemetry.capture() as observations:
                    telemetry.observe({"engines": "duckduckgo"}, response, 45,
                                      source="explicit")
                self.assertEqual(observations[0]["outcome"], expected)
                self.assertNotIn("PRIVATE_ERROR", str(observations))
        with telemetry.capture() as observations:
            telemetry.observe({"engines": "duckduckgo,qwant"}, None, 100,
                              source="explicit")
            telemetry.observe({"engines": "tokenSECRET"}, None, 100,
                              source="explicit")
        self.assertEqual(observations, [])
        self.assertIsNone(telemetry._ACTIVE.get())


if __name__ == "__main__":
    unittest.main()
