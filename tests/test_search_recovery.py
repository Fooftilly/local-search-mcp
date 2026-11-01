"""Bounded recovery from intermittent SearXNG provider failures."""
from contextlib import nullcontext
import unittest
from unittest.mock import patch

import httpx
import bridge
import request_cache


class SearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)

    def client_for(self, handler):
        original = httpx.Client
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        return factory

    def test_empty_reported_failure_recovers_same_query_with_diagnostic(self):
        requests = []
        def handler(request):
            requests.append(dict(request.url.params))
            if len(requests) == 1:
                return httpx.Response(200, json={
                    "results": [],
                    "unresponsive_engines": [["duckduckgo", "PRIVATE_TOKEN_AND_URL"]],
                })
            return httpx.Response(200, json={"results": [{
                "title": "Python reference", "url": "https://docs.python.org/3/",
                "engine": "duckduckgo", "content": "public documentation",
            }], "unresponsive_engines": []})
        with (patch.object(bridge.httpx, "Client", side_effect=self.client_for(handler)),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep", return_value=None)):
            result = bridge.web_search("Python documentation", include_search_status=True)
        self.assertEqual(requests, [
            {"q": "Python documentation", "format": "json"},
            {"q": "Python documentation", "format": "json"},
        ])
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["results"][0]["engine"], "duckduckgo")
        recovery = result["search_status"]["recovery"]
        self.assertEqual(recovery["attempted"], True)
        self.assertEqual(recovery["recovered"], True)
        self.assertEqual(recovery["first_attempt"]["unresponsive_engines"],
                         ["duckduckgo"])
        self.assertNotIn("PRIVATE_TOKEN_AND_URL", str(result))

    def test_explicit_engine_retry_cannot_broaden_or_skip_inventory(self):
        requests = []
        def handler(request):
            requests.append((request.url.path, dict(request.url.params)))
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "duckduckgo", "enabled": True,
                     "categories": ["general"]},
                    {"name": "qwant", "enabled": True, "categories": ["general"]},
                ]})
            if len([x for x in requests if x[0] == "/search"]) == 1:
                return httpx.Response(200, json={
                    "results": [], "unresponsive_engines": [["duckduckgo", "timeout"]],
                })
            return httpx.Response(200, json={"results": [{
                "title": "Official", "url": "https://www.python.org/",
                "engine": "duckduckgo",
            }]})
        with (patch.object(bridge.httpx, "Client", side_effect=self.client_for(handler)),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep", return_value=None)):
            result = bridge.web_search("Python", engines="duckduckgo")
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["engine"], "duckduckgo")
        self.assertEqual([p for p, _ in requests],
                         ["/config", "/search", "/search"])
        self.assertEqual(requests[1][1], requests[2][1])
        self.assertEqual(requests[1][1]["engines"], "duckduckgo")

    def test_healthy_empty_result_is_not_retried_and_can_be_cached(self):
        requests = []
        def handler(request):
            requests.append(request.url.path)
            return httpx.Response(200, json={
                "results": [], "unresponsive_engines": [],
            })
        with (patch.object(bridge.httpx, "Client", side_effect=self.client_for(handler)),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext())):
            self.assertEqual(bridge.web_search("no results"), [])
            self.assertEqual(bridge.web_search("no results"), [])
        self.assertEqual(requests, ["/search"])

    def test_failed_retry_preserves_error_and_is_not_cached(self):
        requests = []
        def handler(request):
            requests.append(request.url.path)
            if len(requests) % 2 == 0:
                return httpx.Response(503, json={"private": "SECRET_SERVER_DETAIL"})
            return httpx.Response(200, json={
                "results": [],
                "unresponsive_engines": [["qwant", "SECRET_PROVIDER_DETAIL"]],
            })
        with (patch.object(bridge.httpx, "Client", side_effect=self.client_for(handler)),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep", return_value=None)):
            first = bridge.web_search("recover me", include_search_status=True)
            second = bridge.web_search("recover me", include_search_status=True)
        self.assertEqual(requests, ["/search"] * 4)
        self.assertEqual(first["status"], "upstream_unavailable")
        self.assertEqual(second["status"], "upstream_unavailable")
        self.assertFalse(first["search_status"]["recovery"]["recovered"])
        self.assertNotIn("SECRET", repr([first, second]))

    def test_unreadable_or_malformed_response_does_not_trigger_recovery(self):
        requests = []
        def handler(request):
            requests.append(request.url.path)
            return httpx.Response(200, json={
                "results": [], "unresponsive_engines": {"error": "PRIVATE"},
            })
        with (patch.object(bridge.httpx, "Client", side_effect=self.client_for(handler)),
              patch.object(bridge.on_demand, "lease", side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep",
                           side_effect=AssertionError("must not retry"))):
            result = bridge.web_search("malformed diagnostics", include_search_status=True)
        self.assertEqual(result["status"], "upstream_unavailable")
        self.assertEqual(requests, ["/search"])
        self.assertNotIn("PRIVATE", str(result))


if __name__ == "__main__":
    unittest.main()
