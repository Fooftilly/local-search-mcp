"""Read-only owner-verified SearXNG error diagnostics, no provider queries."""
import json
import unittest
from unittest.mock import patch

import httpx

from scripts import engine_error_diagnostics as diag


ENABLED = [
    {"name": "bing", "enabled": True, "categories": ("general", "web")},
    {"name": "duckduckgo web", "enabled": True, "categories": ("general",)},
    {"name": "qwant", "enabled": True, "categories": ("general", "web")},
    {"name": "brave", "enabled": True, "categories": ("general",)},
    {"name": "google", "enabled": True, "categories": ("general",)},
    {"name": "inactive", "enabled": False, "categories": ("general",)},
]


class EngineErrorDiagnosticsTests(unittest.TestCase):
    def _run(self, response=None, *, requested=None, inventory=ENABLED):
        original = httpx.Client
        calls = []

        def handler(request):
            calls.append((request.method, str(request.url)))
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url),
                             "http://127.0.0.1:8888/stats/errors")
            return response if response is not None else httpx.Response(
                200, json={
                    "bing": [{"exception_classname": "KeyError",
                              "percentage": 30, "secondary": False,
                              "log_message": "PRIVATE_API_TOKEN",
                              "log_parameters": ["PRIVATE_QUERY"],
                              "filename": "/private/path"}],
                    "qwant": [{"exception_classname": "SearxEngineAccessDeniedException",
                               "percentage": 60.0, "secondary": False}],
                    "brave": [{"exception_classname": "SearxEngineCaptchaException",
                               "percentage": 50, "secondary": False}],
                    "other": [{"exception_classname": "PRIVATE_API_TOKEN",
                               "percentage": 90}],
                })

        def factory(**kwargs):
            self.assertIs(kwargs.get("trust_env"), False)
            self.assertIs(kwargs.get("follow_redirects"), False)
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with (patch.object(diag.engine_dashboard, "verified_inventory",
                           return_value=(inventory, "revision")) as verified,
              patch.object(diag.engine_pacing_dashboard, "snapshot",
                           return_value={"engines": {
                               "bing": 30, "duckduckgo web": 15, "qwant": 60}}) as policy,
              patch.object(diag.httpx, "Client", side_effect=factory)):
            result = diag.report(requested)
        return result, calls, verified, policy

    def test_report_restricts_to_policy_engines_and_redacts_error_context(self):
        result, calls, verified, policy = self._run()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)
        verified.assert_called_once_with()
        policy.assert_called_once_with()
        self.assertEqual([row["engine"] for row in result["engine_errors"]],
                         ["bing", "duckduckgo web", "qwant"])
        bing, ddg, qwant = result["engine_errors"]
        self.assertEqual(bing["errors"][0]["category"], "parser_or_response")
        self.assertEqual(bing["errors"][0]["reported_percentage"], 30)
        self.assertEqual(ddg["status"], "no_error_records")
        self.assertEqual(qwant["errors"][0]["category"], "access_denied")
        self.assertEqual(result["provider_searches_sent"], 0)
        self.assertEqual(result["services_started"], 0)
        self.assertFalse(result["settings_modified"])
        for marker in ("PRIVATE", "/private", "other", "google"):
            self.assertNotIn(marker, json.dumps(result))

    def test_explicit_brave_probe_uses_only_selected_engine_stats(self):
        result, calls, _, policy = self._run(requested=["brave"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["engine_errors"][0]["engine"], "brave")
        self.assertEqual(result["engine_errors"][0]["errors"][0]["category"], "captcha")
        policy.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_large_policy_is_bounded_and_omissions_are_explicit(self):
        configured = {f"engine-{i:02d}": i for i in range(9)}
        inventory = [
            {"name": name, "enabled": True, "categories": ("general",)}
            for name in configured
        ]
        original = httpx.Client
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={
                name: [] for name in configured
            })

        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with (patch.object(diag.engine_dashboard, "verified_inventory",
                           return_value=(inventory, "revision")),
              patch.object(diag.engine_pacing_dashboard, "snapshot",
                           return_value={"engines": configured}),
              patch.object(diag.httpx, "Client", side_effect=factory)):
            result = diag.report()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["configured_engines_omitted"], 1)
        self.assertEqual(len(result["engine_errors"]), 8)
        self.assertEqual([row["engine"] for row in result["engine_errors"]],
                         sorted(configured)[:8])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["provider_searches_sent"], 0)

    def test_refuse_unknown_disabled_or_google_names_without_request(self):
        for names in (["google"], ["inactive"], ["not-enabled"]):
            with self.subTest(names=names):
                result, calls, _, policy = self._run(requested=names)
                self.assertEqual(result["status"],
                                 "invalid_selection" if names == ["google"]
                                 else "engine_unavailable")
                self.assertEqual(calls, [])
                policy.assert_not_called()
        for names in (["brave", "BRAVE"], ["bing", "bing"],
                      ["brave"] * 9, ["bad,name"], [" x"]):
            with self.subTest(names=names):
                result, calls, verified, _ = self._run(requested=names)
                self.assertEqual(result["status"], "invalid_selection")
                verified.assert_not_called()
                self.assertEqual(calls, [])

    def test_reject_redirect_html_bad_json_oversized_and_bad_error_rows(self):
        examples = [
            httpx.Response(302, headers={"location": "https://malicious.example/"}),
            httpx.Response(200, text="PRIVATE", headers={"content-type": "text/html"}),
            httpx.Response(200, content=b"{not-json",
                           headers={"content-type": "application/json"}),
            httpx.Response(200, json={"bing": "not_a_list"}),
            httpx.Response(200, json={"bing": [{"percentage": True}]}),
            httpx.Response(200, json={"bing": [{"percentage": -1}]}),
            httpx.Response(200, json={"bing": [{"percentage": 101}]}),
            httpx.Response(200, json={"bing": [{"secondary": "secret"}]}),
            httpx.Response(200, json={"bing": [None]}),
            httpx.Response(200, content=b"x" * (diag._MAX_BYTES + 1),
                           headers={"content-type": "application/json"}),
        ]
        for response in examples:
            with self.subTest(code=response.status_code, size=len(response.content)):
                result, calls, _, _ = self._run(response)
                self.assertEqual(result["status"], "stats_unavailable")
                self.assertEqual(result["engine_errors"], [])
                self.assertEqual(len(calls), 1)
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_unknown_exception_strings_do_not_appear_in_output(self):
        response = httpx.Response(200, json={"bing": [
            {"exception_classname": "PRIVATE_API_TOKEN", "percentage": 5},
            {"log_message": "PRIVATE_QUERY", "percentage": 0},
            {"exception_classname": "requests.exceptions.Timeout",
             "percentage": 10},
        ]})
        result, _, _, _ = self._run(response)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [row["category"] for row in result["engine_errors"][0]["errors"]],
            ["other", "unclassified", "timeout"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("requests", json.dumps(result))

    def test_bad_policy_or_missing_inventory_does_not_query_stats(self):
        with (patch.object(diag.engine_pacing_dashboard, "snapshot",
                           side_effect=OSError("PRIVATE_PATH")),
              patch.object(diag.engine_dashboard, "verified_inventory",
                           side_effect=AssertionError("must not verify"))):
            report = diag.report()
        self.assertEqual(report["status"], "policy_unavailable")
        self.assertNotIn("PRIVATE", json.dumps(report))
        with (patch.object(diag.engine_pacing_dashboard, "snapshot",
                           return_value={"engines": {"bing": 30}}),
              patch.object(diag.engine_dashboard, "verified_inventory",
                           side_effect=OSError("PRIVATE_CONTAINER"))):
            report = diag.report()
        self.assertEqual(report["status"], "inventory_unavailable")
        self.assertNotIn("PRIVATE", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
