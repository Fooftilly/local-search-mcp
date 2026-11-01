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

    def execute(self, query="public example", *, batch=False, **kwargs):
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
            if batch:
                return asyncio.run(bridge.web_search_batch([query], fresh=True, **kwargs))
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
        for arguments in ({"engines": "duckduckgo,qwant"},
                          {"engines": "qwant"},
                          {"categories": "images,news"}):
            with self.subTest(arguments=arguments):
                self.calls.clear()
                result = self.execute(**arguments)
                self.assertEqual(result["status"], "pacing_selection_required")
                self.assertEqual(self.calls, [])

    def test_auto_routing_does_not_guess_optional_filter_support(self):
        self.policy()
        for options in ({"language": "en"}, {"time_range": "month"},
                        {"page": 2}):
            with self.subTest(options=options):
                self.calls.clear()
                result = self.execute(**options)
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

    def test_unfiltered_paced_rejection_remains_structured_for_direct_and_batch(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                for state in self.config.glob(".engine-pacing-*.json"):
                    state.unlink()
                self.calls.clear()
                self.policy()
                self.search_response = httpx.Response(400)
                output = self.execute(batch=batch)
                if batch:
                    self.assertFalse(output["success"])
                    self.assertEqual(output["results"][0]["status"], "upstream_rejected")
                else:
                    self.assertEqual(output["status"], "upstream_rejected")
                    self.assertEqual(output["http_status"], 400)
                self.assertEqual([p for p, _ in self.calls], ["/config", "/search"])

    def test_paced_default_one_failed_search_does_not_try_next_engine(self):
        self.policy(entries={"duckduckgo": 60, "qwant": 0})
        self.search_response = httpx.ConnectError("PRIVATE ERROR")
        result = self.execute()
        self.assertEqual(result["status"], "upstream_unavailable")
        self.assertNotIn("PRIVATE ERROR", str(result))
        self.assertEqual([path for path, _ in self.calls], ["/config", "/search"])
        self.assertEqual(self.calls[-1][1]["engines"], "duckduckgo")

    def test_paced_single_category_uses_only_dedicated_engine(self):
        self.policy(entries={"bing images": 30, "duckduckgo": 15})
        self.inventory_response = httpx.Response(200, json={"engines": [
            {"name": "duckduckgo", "enabled": True, "categories": ["general", "images"]},
            {"name": "bing images", "enabled": True, "categories": ["images"]},
            {"name": "google images", "enabled": False, "categories": ["images"]},
        ]})
        self.search_response = httpx.Response(200, json={"results": [
            {"title": "Image", "url": "https://example.com/image",
             "engine": "bing images"}], "unresponsive_engines": []})
        with patch.object(bridge.isolated_engine_observations, "observe",
                          wraps=bridge.isolated_engine_observations.observe) as observed:
            result = self.execute(categories="images", include_image_metadata=True)
        self.assertIsInstance(result, list)
        self.assertEqual([name for name, _ in self.calls], ["/config", "/search"])
        self.assertEqual(self.calls[-1][1]["engines"], "bing images")
        self.assertNotIn("categories", self.calls[-1][1])
        self.assertEqual(observed.call_args.kwargs["source"], "paced_category")

    def test_mixed_category_engines_cannot_expand_scope(self):
        self.policy(entries={"duckduckgo": 15})
        self.inventory_response = httpx.Response(200, json={"engines": [
            {"name": "duckduckgo", "enabled": True, "categories": ["general", "images"]}]})
        output = self.execute(categories="images")
        self.assertEqual(output["status"], "pacing_selection_required")
        self.assertEqual([path for path, _ in self.calls], ["/config"])

    def test_paced_category_must_be_policy_listed_enabled_and_exact(self):
        self.policy(entries={"bing images": 15})
        for entries in ([
            {"name": "bing images", "enabled": False, "categories": ["images"]}],
            [{"name": "Bing Images", "enabled": True, "categories": ["images"]}],
            [{"name": "bing images", "enabled": True, "categories": ["videos"]}],
            [{"name": "bing images", "enabled": True, "categories": ["images", "general"]}],
        ):
            with self.subTest(entries=entries):
                self.calls.clear()
                self.inventory_response = httpx.Response(200, json={"engines": entries})
                output = self.execute(categories="images")
                # A completely disabled inventory is itself an invalid
                # routing source; eligible-but-unlisted names are a selection
                # failure. Neither case may send a provider search.
                expected = ("invalid_response" if not any(
                    row["enabled"] for row in entries) else "pacing_selection_required")
                self.assertEqual(output["status"], expected)
                self.assertEqual([path for path, _ in self.calls], ["/config"])

    def test_pacing_requires_case_exact_policy_name_for_explicit_search(self):
        self.policy(entries={"Bing Images": 0})
        self.inventory_response = httpx.Response(200, json={"engines": [
            {"name": "Bing Images", "enabled": True, "categories": ["images"]}]})
        # Case-insensitive engine labels would share a cooldown slot but the
        # bridge still must not send any provider the policy did not name.
        out = self.execute(engines="bing images")
        self.assertEqual(out["status"], "pacing_selection_required")
        self.assertEqual(self.calls, [])

    def test_category_pacing_skips_busy_dedicated_engine(self):
        self.policy(entries={"bing images": 30, "imageonly": 30})
        self.inventory_response = httpx.Response(200, json={"engines": [
            {"name": "bing images", "enabled": True, "categories": ["images"]},
            {"name": "imageonly", "enabled": True, "categories": ["images"]}]})
        def answer(request):
            engine = request.url.params["engines"]
            return httpx.Response(200, json={"results": [
                {"title": "Photo", "url": "https://example.com/image",
                 "engine": engine}]})
        self.search_response = answer
        first = self.execute(categories="images")
        second = self.execute(categories="images")
        third = self.execute(categories="images")
        self.assertIsInstance(first, list)
        self.assertIsInstance(second, list)
        self.assertEqual(third["status"], "rate_limited")
        self.assertGreater(third["retry_after_seconds"], 0)
        search_calls = [params for name, params in self.calls if name == "/search"]
        self.assertEqual([p["engines"] for p in search_calls], ["bing images", "imageonly"])
        self.assertTrue(all("categories" not in p for p in search_calls))

    def test_category_pacing_never_retries_after_transport_failure(self):
        self.policy(entries={"bing images": 30, "imageonly": 30})
        self.inventory_response = httpx.Response(200, json={"engines": [
            {"name": "bing images", "enabled": True, "categories": ["images"]},
            {"name": "imageonly", "enabled": True, "categories": ["images"]}]})
        self.search_response = httpx.ConnectError("PRIVATE_NETWORK_DETAIL")
        result = self.execute(categories="images")
        self.assertEqual(result["status"], "upstream_unavailable")
        self.assertNotIn("PRIVATE_NETWORK_DETAIL", repr(result))
        self.assertEqual([path for path, _ in self.calls], ["/config", "/search"])

    def test_filtered_category_auto_routing_stays_disabled(self):
        self.policy(entries={"bing images": 30})
        for kwargs in ({"categories": "images", "language": "en"},
                       {"categories": "images", "page": 2},
                       {"categories": "images", "time_range": "month"}):
            with self.subTest(kwargs=kwargs):
                self.calls.clear()
                out = self.execute(**kwargs)
                self.assertEqual(out["status"], "pacing_selection_required")
                self.assertEqual(self.calls, [])

    def test_case_colliding_inventory_fails_before_search(self):
        self.policy(entries={"bing images": 30})
        self.inventory_response = httpx.Response(200, json={"engines": [
            {"name": "bing images", "enabled": True, "categories": ["images"]},
            {"name": "Bing Images", "enabled": True, "categories": ["images"]}]})
        result = self.execute(categories="images")
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual([path for path, _ in self.calls], ["/config"])

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
                if request.url.path == "/config":
                    return httpx.Response(200, json={"engines": [
                        {"name": "duckduckgo", "enabled": True,
                         "categories": ["general"]}]})
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
