"""Offline regressions for the native deep-crawl Docker opt-in safety gate.

All Docker calls are mocked: these tests never start containers, fetch pages,
alter settings or enable the Crawl4AI feature flag.
"""
from __future__ import annotations

import subprocess
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import httpx
import bridge
import crawler_security
from scripts import on_demand_supervisor


class NativeDeepCrawlGuardTests(unittest.TestCase):
    def probe(self, environment, *, ids=("a" * 64,)):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(tuple(command))
            self.assertEqual(command[0], "docker")
            self.assertEqual(kwargs["timeout"], 4)
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["check"])
            self.assertTrue(kwargs["text"])
            if command[1] == "ps":
                self.assertIn("publish=11235", command)
                body = "\n".join(ids) + ("\n" if ids else "")
            elif command[1] == "inspect":
                self.assertEqual(len(ids), 1)
                self.assertEqual(command[2], ids[0])
                body = "\n".join(environment) + "\n"
            else:
                self.fail("Unexpected Docker operation")
            return subprocess.CompletedProcess(command, 0, body, "")

        with (patch("shutil.which", return_value="/usr/bin/docker"),
              patch("subprocess.run", side_effect=fake_run)):
            result = crawler_security.local_container_warning()
        return result, calls

    def test_deep_crawl_disabled_or_absent_preserves_normal_fetches(self):
        for value in (None, "", "false", "False", "0", "no", "off"):
            variables = ["CRAWL4AI_API_TOKEN=SECRET_DO_NOT_EXPOSE"]
            if value is not None:
                variables.append("CRAWL4AI_ALLOW_DEEP_CRAWL=" + value)
            with self.subTest(value=value):
                warning, calls = self.probe(variables)
            self.assertIsNone(warning)
            self.assertEqual([call[1] for call in calls], ["ps", "inspect"])

    def test_native_deep_crawl_opt_in_is_blocked_and_never_leaks_secrets(self):
        for value in ("true", "True", "1", "yes", "on", "unknown", " true "):
            with self.subTest(value=value):
                warning, _ = self.probe([
                    "CRAWL4AI_API_TOKEN=SECRET_DO_NOT_EXPOSE",
                    "CRAWL4AI_ALLOW_DEEP_CRAWL=" + value,
                ])
                self.assertIsNotNone(warning)
                self.assertIn("CRAWL4AI_ALLOW_DEEP_CRAWL", warning)
                self.assertIn("issue #299", warning)
                self.assertNotIn("SECRET_DO_NOT_EXPOSE", warning)
                self.assertNotIn("CRAWL4AI_ALLOW_DEEP_CRAWL=" + value, warning)

    def test_duplicate_or_malformed_native_flag_fails_closed(self):
        for flags in (
            ["CRAWL4AI_ALLOW_DEEP_CRAWL=false",
             "CRAWL4AI_ALLOW_DEEP_CRAWL=true"],
            ["CRAWL4AI_ALLOW_DEEP_CRAWL=false",
             "CRAWL4AI_ALLOW_DEEP_CRAWL=false"],
            ["CRAWL4AI_ALLOW_DEEP_CRAWL"],
        ):
            with self.subTest(flags=len(flags)):
                warning, _ = self.probe(flags)
            self.assertIsNotNone(warning)
            self.assertIn("Native Crawl4AI deep crawling", warning)

    def test_other_unsafe_settings_remain_blocked(self):
        for key in ("CRAWL4AI_ALLOW_INTERNAL_URLS", "CRAWL4AI_ALLOW_INSECURE_TLS"):
            with self.subTest(key=key):
                warning, _ = self.probe([key + "=true"])
                self.assertIsNotNone(warning)
                self.assertIn(key, warning)

    def test_ambiguous_published_crawler_never_inspects_first_match(self):
        warning, calls = self.probe([], ids=("a" * 64, "b" * 64))
        self.assertIsNotNone(warning)
        self.assertIn("Ambiguous", warning)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "ps")

    def test_sleeping_crawler_is_not_mistaken_for_verified_safe(self):
        warning, calls = self.probe([], ids=())
        self.assertIsNone(warning)
        self.assertEqual(len(calls), 1)
        # A missing running container is not proof of a safe flag. The
        # on-demand wake path must check local_container_warning again.

    def test_cold_wake_health_gate_rejects_unsafe_flag_without_http(self):
        with (patch.object(on_demand_supervisor, "local_container_warning",
                           return_value="Native Crawl4AI deep crawling is enabled"),
              patch.object(on_demand_supervisor, "get_json",
                           side_effect=AssertionError("Health must not be queried"))):
            self.assertFalse(on_demand_supervisor._healthy("crawl4ai"))


    def test_running_bridge_rechecks_flags_after_container_replacement(self):
        """A once-safe container must not authorize a later unsafe one."""
        url = "https://example.com/public"
        inspected = {"warning": None}
        requests = []
        original_client = httpx.Client

        def handler(request):
            requests.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(
                    200, json={"status": "ok", "version": "0.9.5"})
            if request.url.path == "/crawl":
                return httpx.Response(200, json={"results": [{
                    "url": url, "success": True, "markdown": "Approved page"
                }]})
            self.fail("Unexpected upstream route")

        def client(**kwargs):
            return original_client(
                transport=httpx.MockTransport(handler), trust_env=False)

        with (patch.object(bridge, "local_container_warning",
                           side_effect=lambda: inspected["warning"]),
              patch.object(bridge.httpx, "Client", side_effect=client)):
            first = bridge._run_fetch_network(url, "raw")
            inspected["warning"] = (
                "Native Crawl4AI deep crawling is enabled; disable "
                "CRAWL4AI_ALLOW_DEEP_CRAWL until issue #299 is resolved")
            second = bridge._run_fetch_network(url, "raw")

        self.assertTrue(first["success"])
        self.assertEqual(first["markdown"], "Approved page")
        self.assertFalse(second["success"])
        self.assertEqual(second["status"], "blocked")
        self.assertNotIn("markdown", second)
        self.assertEqual(requests, ["/health", "/crawl", "/health"])

    def test_public_web_fetch_blocks_runtime_flag_without_crawl_post(self):
        """Bridge import and cache state cannot hide a newly unsafe flag."""
        url = "https://example.com/articles"
        requests = []
        original_client = httpx.Client

        def handler(request):
            requests.append(request.url.path)
            self.assertEqual(request.url.path, "/health")
            return httpx.Response(200, json={"status": "ok", "version": "0.9.5"})

        def client(**kwargs):
            return original_client(
                transport=httpx.MockTransport(handler), trust_env=False)

        with (patch.object(bridge, "_check_public_target", return_value=None),
              patch.object(bridge, "CRAWLER_SECURITY_WARNING", None),
              patch.object(bridge, "local_container_warning",
                           return_value="Native Crawl4AI deep crawling is enabled"),
              patch.object(bridge.on_demand, "lease",
                           side_effect=lambda service: nullcontext()),
              patch.object(bridge.httpx, "Client", side_effect=client)):
            result = bridge.web_fetch(url, fresh=True)

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("markdown", result)
        self.assertEqual(requests, ["/health"])


if __name__ == "__main__":
    unittest.main()
