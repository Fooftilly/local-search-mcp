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

    def policy(self, enabled=True):
        self.config.mkdir(mode=0o700, exist_ok=True)
        path = self.config / engine_pacing.FILENAME
        path.write_text(json.dumps({
            "schema": engine_pacing.SCHEMA, "enabled": enabled,
            "engines": {"duckduckgo": {"min_spacing_seconds": 60}},
        }))
        path.chmod(0o600)
        return path

    def execute(self, **kwargs):
        original = httpx.Client

        def handler(request):
            self.calls.append((request.url.path, dict(request.url.params)))
            if request.url.path == "/config":
                return httpx.Response(200, json={"engines": [
                    {"name": "duckduckgo", "enabled": True,
                     "categories": ["general"]},
                    {"name": "qwant", "enabled": True,
                     "categories": ["general"]},
                ]})
            self.assertEqual(request.url.path, "/search")
            return self.search_response

        def client_factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with (patch.object(bridge.httpx, "Client", side_effect=client_factory),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda _: nullcontext()),
              patch.object(bridge.time, "sleep", return_value=None)):
            return bridge.web_search("public example", fresh=True, **kwargs)

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
        for arguments in ({}, {"categories": "general"},
                          {"engines": "duckduckgo,qwant"},
                          {"engines": "qwant"}):
            with self.subTest(arguments=arguments):
                self.calls.clear()
                result = self.execute(**arguments)
                self.assertEqual(result["status"], "pacing_selection_required")
                self.assertEqual(self.calls, [])

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
        self.assertEqual(second["status"], "pacing_selection_required")
        self.assertEqual(self.calls, ["/search"])


if __name__ == "__main__":
    unittest.main()
