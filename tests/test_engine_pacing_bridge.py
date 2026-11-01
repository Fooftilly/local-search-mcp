"""Strict, opt-in single-engine pacing is enforced at the real /search boundary."""
from contextlib import nullcontext
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import bridge
import engine_pacing
import request_cache


class PacedSearchBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.config = self.folder / "local-search-mcp"
        env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.folder),
            "XDG_STATE_HOME": str(self.folder),
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.folder / "history.jsonl"),
        })
        env.start()
        self.addCleanup(env.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        self.calls = []
        self.search_response = httpx.Response(200, json={"results": [
            {"title": "Public", "url": "https://docs.python.org/3/",
             "engine": "duckduckgo"}], "unresponsive_engines": []})

    def policy(self, enabled=True, entries=None):
        self.config.mkdir(mode=0o700, exist_ok=True)
        path = self.config / engine_pacing.FILENAME
        entries = {"duckduckgo": 60} if entries is None else entries
        path.write_text(json.dumps({
            "schema": engine_pacing.SCHEMA, "enabled": enabled,
            "engines": {name: {"min_spacing_seconds": gap}
                        for name, gap in entries.items()},
        }))
        path.chmod(0o600)
        return path

    def execute(self, query="public example", **kwargs):
        original = httpx.Client

        def handler(request):
            self.calls.append((request.url.path, dict(request.url.params)))
            if request.url.path == "/config":
                return (self.inventory_response if hasattr(self, "inventory_response")
                        else httpx.Response(200, json={"engines": [
                            {"name": "duckduckgo", "enabled": True,
                             "categories": ["general"]},
                            {"name": "qwant", "enabled": True,
                             "categories": ["general"]},
                        ]}))
            self.assertEqual(request.url.path, "/search")
            if isinstance(self.search_response, Exception):
                raise self.search_response
            if callable(self.search_response):
                return self.search_response(request)
            return self.search_response

        def client_factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=client_factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep", return_value=None)):
            return bridge.web_search(query, fresh=True, **kwargs)

    def test_disabled_policy_preserves_search_default(self):
        for enabled in (None, False):
            with self.subTest(enabled=enabled):
                if enabled is not None:
                    self.policy(enabled=False)
                self.calls.clear()
                result = self.execute()
                self.assertIsInstance(result, list)
                self.assertEqual([x[0] for x in self.calls], ["/search"])

    def test_single_engine_admitted_once_and_cooldown_rejects_next(self):
        self.policy()
        first = self.execute(engines="duckduckgo")
        self.assertIsInstance(first, list)
        second = self.execute(engines="duckduckgo")
        self.assertEqual(second["status"], "rate_limited")
        self.assertGreater(second["retry_after_seconds"], 0)
        self.assertEqual([p for p, _ in self.calls],
                         ["/config", "/search", "/config"])

    def test_ambiguous_requests_do_not_query_searxng(self):
        self.policy()
        for arguments in ({"categories": "general"},
                          {"engines": "duckduckgo,qwant"},
                          {"engines": "qwant"}):
            with self.subTest(arguments=arguments):
                self.calls.clear()
                result = self.execute(**arguments)
                self.assertEqual(result["status"], "pacing_selection_required")
                self.assertEqual(self.calls, [])

    def test_paced_default_routes_only_to_verified_general_engine(self):
        self.policy(entries={"duckduckgo": 60})
        with patch.object(bridge.isolated_engine_observations, "observe",
                          wraps=bridge.isolated_engine_observations.observe) as observed:
            result = self.execute()
        self.assertIsInstance(result, list)
        self.assertEqual([path for path, _ in self.calls], ["/config", "/search"])
        self.assertEqual(self.calls[-1][1]["engines"], "duckduckgo")
        self.assertNotIn("categories", self.calls[-1][1])
        self.assertEqual(observed.call_args.kwargs["source"], "paced_default")

    def test_paced_default_skips_cooling_engine_without_extra_searches(self):
        self.policy(entries={"duckduckgo": 60, "qwant": 60})
        def answer(request):
            engine = request.url.params["engines"]
            return httpx.Response(200, json={"results": [
                {"title": "Public", "url": "https://docs.python.org/3/",
                 "engine": engine}], "unresponsive_engines": []})
        self.search_response = answer
        first = self.execute()
        second = self.execute()
        third = self.execute()
        self.assertIsInstance(first, list)
        self.assertIsInstance(second, list)
        self.assertEqual(third["status"], "rate_limited")
        self.assertGreater(third["retry_after_seconds"], 0)
        self.assertEqual([params["engines"] for path, params in self.calls
                          if path == "/search"], ["duckduckgo", "qwant"])
        self.assertEqual([path for path, _ in self.calls],
                         ["/config", "/search", "/config", "/search", "/config"])

    def test_paced_default_no_eligible_general_engine_fails_closed(self):
        self.policy(entries={"qwant": 15})
        self.inventory_response = httpx.Response(200, json={"engines": [
            {"name": "qwant", "enabled": True, "categories": ["images"]},
            {"name": "duckduckgo", "enabled": True, "categories": ["general"]}]})
        result = self.execute()
        self.assertEqual(result["status"], "pacing_selection_required")
        self.assertEqual([path for path, _ in self.calls], ["/config"])

    def test_bad_paced_inventory_fails_before_provider_search(self):
        self.policy()
        for payload in ({"engines": [{"name": "duckduckgo", "enabled": True}]},
                        {"engines": [{"name": "duckduckgo", "enabled": True,
                                       "categories": ["general"]},
                                      {"name": "duckduckgo", "enabled": True,
                                       "categories": ["general"]}]},
                        {"engines": "untrusted"}):
            with self.subTest(payload=payload):
                self.calls.clear()
                self.inventory_response = httpx.Response(200, json=payload)
                result = self.execute()
                self.assertEqual(result["status"], "invalid_response")
                self.assertEqual([path for path, _ in self.calls], ["/config"])

    def test_paced_default_one_failed_search_does_not_try_next_engine(self):
        self.policy(entries={"duckduckgo": 60, "qwant": 0})
        self.search_response = httpx.ConnectError("PRIVATE ERROR")
        result = self.execute()
        self.assertEqual(result["status"], "upstream_unavailable")
        self.assertNotIn("PRIVATE ERROR", str(result))
        self.assertEqual([path for path, _ in self.calls], ["/config", "/search"])
        self.assertEqual(self.calls[-1][1]["engines"], "duckduckgo")

    def test_bang_shortcut_refused_with_pacing(self):
        self.policy()
        original = httpx.Client
        calls = []
        def client_factory(**kwargs):
            def handler(request):
                calls.append(request.url.path)
                return httpx.Response(200, json={"results": []})
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=client_factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext())):
            output = bridge.web_search("!ddg public", fresh=True)
        self.assertEqual(output["status"], "pacing_selection_required")
        self.assertEqual(calls, [])

    def test_paced_error_or_unresponsive_response_is_not_retried(self):
        self.policy()
        for response, expected in [
            (httpx.Response(429), "rate_limited"),
            (httpx.Response(200, json={
                "results": [],
                "unresponsive_engines": ["duckduckgo"]}), "upstream_unavailable"),
        ]:
            with self.subTest(expected=expected):
                self.calls.clear()
                self.search_response = response
                result = self.execute(engines="duckduckgo")
                # Only the first test enters its slot; reset isolated cooldown
                # for the next case without altering a real operator policy.
                if result["status"] == "rate_limited" and len(self.calls) == 1:
                    self.assertEqual(self.calls[0][0], "/config")
                else:
                    self.assertEqual(result["status"], expected)
                    self.assertEqual([p for p, _ in self.calls],
                                     ["/config", "/search"])
                for item in self.config.glob(".engine-pacing-*.json"):
                    item.unlink()

    def test_exclamation_inside_word_is_not_a_bang_shortcut(self):
        self.policy()
        result = self.execute(query="foo!bar", engines="duckduckgo")
        self.assertIsInstance(result, list)
        self.assertEqual([p for p, _ in self.calls], ["/config", "/search"])

    def test_one_paced_transport_error_reports_one_attempt_only(self):
        self.policy()
        self.search_response = httpx.ConnectError("PRIVATE_TRANSPORT_DETAIL")
        result = self.execute(engines="duckduckgo")
        self.assertEqual(result["status"], "upstream_unavailable")
        self.assertIn("one paced request", result["reason"])
        self.assertNotIn("PRIVATE_TRANSPORT_DETAIL", repr(result))
        self.assertEqual([p for p, _ in self.calls], ["/config", "/search"])

    def test_corrupt_policy_fails_closed_before_backend_wake(self):
        self.policy().write_text("{malformed")
        self.calls.clear()
        result = self.execute(engines="duckduckgo")
        self.assertEqual(result["status"], "pacing_unavailable")
        self.assertEqual(self.calls, [])

    def test_enabling_policy_does_not_replay_old_fanout_cache(self):
        original = httpx.Client
        def client_factory(**kwargs):
            def handler(request):
                self.calls.append(request.url.path)
                return self.search_response
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=client_factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext())):
            first = bridge.web_search("cache topic", fresh=False)
            self.assertIsInstance(first, list)
            self.policy()
            second = bridge.web_search("cache topic", fresh=False)
        self.assertIsInstance(second, list)
        self.assertEqual(self.calls, ["/search", "/config", "/search"])


if __name__ == "__main__":
    unittest.main()
