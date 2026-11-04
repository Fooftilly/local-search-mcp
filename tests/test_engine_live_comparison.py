"""CI contract for explicitly attended, low-volume routing observations."""
from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import call, patch
import unittest

from scripts import engine_live_comparison as probe


def prepared(names=("bing", "duckduckgo web", "qwant"), *,
             status="ready_for_attended_comparison",
             routing="fixed", enabled=False):
    return {
        "status": status, "routing": routing,
        "pacing_enabled": enabled,
        "plans": [{"scope": "general", "fixed": list(names)}],
    }


class LiveEngineComparisonTests(unittest.TestCase):
    def test_default_preview_never_contacts_provider_or_changes_policy(self):
        with (patch.object(probe.engine_routing_preflight, "report",
                           return_value=prepared()) as reader,
              patch.object(probe.bridge, "web_search",
                           side_effect=AssertionError("no live search"))):
            preview = probe.preview()
        reader.assert_called_once_with()
        self.assertEqual(preview["status"], "ready_for_operator_confirmation")
        self.assertEqual(preview["web_search_calls_attempted"], 0)
        self.assertEqual(preview["candidate_engines"],
                         ["bing", "duckduckgo web", "qwant"])
        self.assertEqual(preview["max_possible_upstream_search_requests"], 6)
        self.assertFalse(preview["policy_modified"])

    def test_denied_preflight_never_returns_unsafe_candidates(self):
        malformed = (
            prepared(names=[]),
            prepared(names=["bing"] * 2),
            prepared(names=["bing", "qwant", "brave", "duckduckgo web"]),
            prepared(names=["google"]),
            prepared(names=["google news"]),
            prepared(names=["unsafe,malicious"]),
            prepared(status="operator_inventory_unavailable"),
            prepared(status="raw_inventory_unavailable"),
            prepared(names=["bing"], routing="invalid"),
        )
        for item in malformed:
            with self.subTest(item=item), patch.object(
                    probe.engine_routing_preflight, "report", return_value=item):
                preview = probe.preview()
            self.assertEqual(preview["status"], "preflight_unavailable")
            self.assertEqual(preview["candidate_engines"], [])
            self.assertEqual(preview["web_search_calls_attempted"], 0)

    def test_absent_or_wrong_confirmation_sends_nothing(self):
        for word in ("", "search", "YES"):
            with (self.subTest(word=word),
                  patch.object(probe.os, "geteuid", return_value=1000),
                  patch.object(probe, "_verified_plan",
                               return_value=(["bing"], "fixed", False)),
                  patch.object(probe.bridge, "web_search",
                               side_effect=AssertionError("no searches"))):
                result = probe.sample("docs", confirm=lambda prompt: word)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["web_search_calls_attempted"], 0)
            self.assertFalse(result["public_query_may_be_retained_in_private_history"])

    def test_refuses_root_and_unrecognized_presets(self):
        with (patch.object(probe.os, "geteuid", return_value=0),
              patch.object(probe, "_verified_plan",
                           side_effect=AssertionError("no host inventory"))):
            self.assertEqual(probe.sample(
                "docs", confirm=lambda _: "SEARCH")["status"], "refused")
        with (patch.object(probe.os, "geteuid", return_value=1000),
              patch.object(probe, "_verified_plan",
                           side_effect=AssertionError("no host inventory"))):
            self.assertEqual(probe.sample(
                "password=PRIVATE", confirm=lambda _: "SEARCH")["status"], "refused")

    def test_attended_sampling_is_bounded_sequential_and_sanitized(self):
        observed = []
        responses = [
            {"success": True, "status": "degraded", "results": [
                {"title": "PRIVATE_RESULT", "url": "https://example.org/SECRET"}]},
            {"success": True, "status": "ok", "results": []},
            {"success": False, "status": "rate_limited",
             "reason": "PRIVATE_UPSTREAM_EXCEPTION"},
        ]
        def fake_search(query, **kwargs):
            observed.append((query, kwargs))
            return responses[len(observed) - 1]
        with (patch.object(probe.os, "geteuid", return_value=1000),
              patch.object(probe, "_verified_plan",
                           return_value=(["bing", "duckduckgo web", "qwant"],
                                         "evidence", True)) as inventory,
              patch.object(probe.bridge, "web_search", side_effect=fake_search),
              patch.object(probe.time, "sleep") as throttle):
            result = probe.sample("science", confirm=lambda prompt: "SEARCH")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["web_search_calls_attempted"], 3)
        self.assertEqual(inventory.call_count, 4)
        self.assertEqual(throttle.call_args_list, [
            call(probe.BETWEEN_CALL_SECONDS), call(probe.BETWEEN_CALL_SECONDS)])
        self.assertEqual([r["engine"] for r in result["measurements"]],
                         ["bing", "duckduckgo web", "qwant"])
        self.assertEqual([r["status"] for r in result["measurements"]],
                         ["results", "empty", "rate_limited"])
        self.assertEqual(
            [r["result_count"] for r in result["measurements"]], [1, 0, None])
        self.assertEqual(result["measurements"][0]["searxng_reported_status"],
                         "degraded")
        self.assertEqual(
            [q for q, _ in observed], [probe.PRESETS["science"]] * 3)
        self.assertEqual(
            [k["engines"] for _, k in observed],
            ["bing", "duckduckgo web", "qwant"])
        for _, kwargs in observed:
            self.assertTrue(kwargs["fresh"])
            self.assertTrue(kwargs["include_search_status"])
            self.assertFalse(kwargs["record_provider_diagnostics"])
            self.assertEqual(kwargs["max_results"], 3)
        report = json.dumps(result)
        for value in ("PRIVATE_RESULT", "PRIVATE_UPSTREAM_EXCEPTION",
                      "SECRET", probe.PRESETS["science"]):
            self.assertNotIn(value, report)
        self.assertFalse(result["policy_modified"])

    def test_policy_drift_stops_after_first_call(self):
        with (patch.object(probe.os, "geteuid", return_value=1000),
              patch.object(probe, "_verified_plan", side_effect=[
                  (["bing", "qwant"], "fixed", False),
                  (["bing", "qwant"], "fixed", False),
                  (["bing"], "fixed", False),
              ]),
              patch.object(probe.bridge, "web_search", return_value=[],
                           ) as search,
              patch.object(probe.time, "sleep") as sleep):
            result = probe.sample("docs", confirm=lambda _: "SEARCH")
        self.assertEqual(result["status"], "preflight_changed")
        self.assertEqual(result["web_search_calls_attempted"], 1)
        search.assert_called_once()
        sleep.assert_not_called()

    def test_unexpected_exception_stops_and_never_echoes_error(self):
        with (patch.object(probe.os, "geteuid", return_value=1000),
              patch.object(probe, "_verified_plan",
                           return_value=(["bing", "qwant"], "fixed", False)),
              patch.object(probe.bridge, "web_search",
                           side_effect=OSError("PRIVATE_TOKEN_OR_URL")) as search):
            result = probe.sample("docs", confirm=lambda _: "SEARCH")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["measurements"][0]["status"], "error")
        self.assertEqual(search.call_count, 1)
        self.assertNotIn("PRIVATE_TOKEN_OR_URL", repr(result))

    def test_malformed_response_is_never_counted_as_success(self):
        for value in (
            None, [False], {"success": True, "status": "ok",
                            "results": [{"title": "SAFE"}, "bad"]},
            {"success": True, "status": "ok", "results": "SECRET"},
            {"success": True, "status": "ok", "results": [1, 2, 3, 4]},
            {"status": "ok", "results": [{"title": "SECRET"}]},
        ):
            with self.subTest(value=value):
                self.assertEqual(probe._outcome(value)["status"],
                                 "invalid_response")

    def test_cli_in_noninteractive_environment_cannot_contact_providers(self):
        with (patch.object(probe.sys.stdin, "isatty", return_value=False),
              patch.object(probe.bridge, "web_search",
                           side_effect=AssertionError("must not execute")),
              patch.object(probe, "_verified_plan",
                           side_effect=AssertionError("must not inspect")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            code = probe.main(["--sample", "--profile", "docs"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "tty_required")

    def test_cli_without_sample_flag_only_reads_preflight(self):
        with (patch.object(probe, "preview", return_value={
                  "mode": "read-only", "status": "ready_for_operator_confirmation",
                  "web_search_calls_attempted": 0,
              }),
              patch.object(probe.bridge, "web_search",
                           side_effect=AssertionError("must not execute")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            code = probe.main([])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["web_search_calls_attempted"], 0)


if __name__ == "__main__":
    unittest.main()
