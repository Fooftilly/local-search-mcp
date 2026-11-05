"""Offline contract and privacy checks for anonymous Exa MCP search (#258)."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import free_provider_pilot as pilot


def exa_tool(*, required=("query",), count_type="number", name="web_search_exa"):
    return SimpleNamespace(name=name, inputSchema={
        "type": "object", "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "numResults": {"type": count_type},
        },
        "required": list(required),
    })


def text_result(text, *, is_error=False):
    return SimpleNamespace(
        is_error=is_error, data=None,
        content=[SimpleNamespace(text=text)],
    )


class OfflineClient:
    def __init__(self, tools, result):
        self.tools = tools
        self.result = result
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_tools(self):
        self.calls.append(("tools/list",))
        return self.tools

    async def call_tool(self, name, args, *, raise_on_error=True):
        self.calls.append(("tools/call", name, args, raise_on_error))
        return self.result


class AnonymousExaPilotTests(unittest.TestCase):
    def test_fixed_free_only_url_and_config_have_no_auth_or_paid_tools(self):
        settings = pilot.PROVIDERS["exa"]
        self.assertEqual(settings["url"],
                         "https://mcp.exa.ai/mcp?tools=web_search_exa")
        self.assertEqual(settings["listed_tools"], frozenset({"web_search_exa"}))
        self.assertEqual(settings["limit_key"], "numResults")
        self.assertFalse(any(word in settings["url"] for word in (
            "exaApiKey", "agent_run", "advanced", "login", "oauth")))
        self.assertEqual(pilot.client_config()["mcpServers"]["exa-keyless"],
                         {"type": "http", "url": settings["url"]})
        with (patch.object(pilot, "Client",
                           side_effect=AssertionError("no network")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(pilot.main([]), 0)
        preview = json.loads(out.getvalue())
        self.assertEqual(len(preview["providers"]), 3)
        self.assertEqual(preview["provider_calls_attempted"], 0)
        self.assertTrue(any(x["name"] == "exa" and not x["requires_key"]
                            for x in preview["providers"]))

    def test_schema_allows_only_bounded_keyless_search_without_extra_required(self):
        self.assertTrue(pilot._schema_ok([exa_tool()], "exa"))
        self.assertTrue(pilot._schema_ok([exa_tool(count_type="integer")], "exa"))
        cases = (
            [exa_tool(required=("query", "objective"))],
            [exa_tool(count_type="string")],
            [exa_tool(name="agent_run")],
            [exa_tool(), exa_tool(name="web_fetch_exa")],
            [],
        )
        for value in cases:
            with self.subTest(value=str(value)):
                self.assertFalse(pilot._schema_ok(value, "exa"))

    def test_exa_text_results_count_only_urls_and_never_output_content(self):
        result = text_result(
            "Title: PRIVATE_TITLE\n"
            "URL: https://docs.python.org/3/?token=PRIVATE_QUERY\n"
            "Published: Never\nAuthor: Nobody\nHighlights:\nPRIVATE_SNIPPET\n"
            "URL: https://example.org/highlight-should-not-count\n\n"
            "---\n\nMarkdown paragraph after a highlight divider.\n\n"
            "---\n\n"
            "Title: Second\nURL: https://www.python.org/about\n"
            "Published: Never\nAuthor: Nobody\n"
        )
        parsed = pilot._redacted_exa_result(result)
        self.assertEqual(parsed, {"status": "results", "result_count": 2,
                                  "distinct_hosts": 2})
        for secret in ("PRIVATE_TITLE", "PRIVATE_QUERY", "PRIVATE_SNIPPET"):
            self.assertNotIn(secret, repr(parsed))

    def test_exa_empty_and_throttled_text_are_not_conflated(self):
        empty = "No search results found. Please try a different query."
        self.assertEqual(pilot._redacted_exa_result(text_result(empty)),
                         {"status": "empty", "result_count": 0,
                          "distinct_hosts": 0})
        throttled = (
            "You've hit Exa's free MCP rate limit. To continue without "
            "limits, create your own Exa API key. PRIVATE_UPSTREAM_URL"
        )
        self.assertEqual(pilot._redacted_exa_result(text_result(throttled)),
                         {"status": "rate_limited", "result_count": None})
        self.assertEqual(pilot._redacted_exa_result(
            text_result(throttled, is_error=True))["status"], "tool_error")

    def test_exa_untrusted_text_malformed_urls_and_excess_are_not_success(self):
        def header(url):
            return f"Title: A result\nURL: {url}\nPublished: Never\nAuthor: Nobody"

        cases = (
            "Search is unavailable - PRIVATE_ERROR",
            header("file:///etc/passwd"),
            header("https://user:secret@internal.example/"),
            header("https://[invalid"),
            header("https://example.org:9443/file"),
            header("javascript:alert(1)"),
            "\n\n---\n\n".join([header("https://example.org")] * 4),
            header("https://example.org") + "X" * (pilot.MAX_TEXT_BYTES + 1),
        )
        for value in cases:
            with self.subTest(value=value[:80]):
                self.assertEqual(pilot._redacted_exa_result(text_result(value)),
                                 {"status": "unverified_response",
                                  "result_count": None})
        self.assertEqual(pilot._redacted_exa_result(
            text_result(header("https://example.org"))).get("result_count"), 1)

    def test_one_call_only_with_exact_bounded_args_and_handlers(self):
        fake = OfflineClient([exa_tool()], text_result(
            "Title: Example\nURL: https://example.org\n"
            "Published: Never\nAuthor: Nobody\n"))
        settings = []
        def make_client(_transport, **kwargs):
            settings.append(kwargs)
            return fake
        with (patch.object(pilot, "StreamableHttpTransport",
                           return_value=object()) as transport,
              patch.object(pilot, "Client", side_effect=make_client)):
            output = asyncio.run(pilot._one_request("exa", "science"))
        self.assertEqual(output["status"], "results")
        self.assertEqual(output["result_count"], 1)
        self.assertEqual(output["tool_calls_attempted"], 1)
        self.assertEqual(fake.calls, [
            ("tools/list",),
            ("tools/call", "web_search_exa",
             {"query": pilot.PRESETS["science"], "numResults": 3}, False),
        ])
        self.assertEqual(transport.call_args.args,
                         (pilot.PROVIDERS["exa"]["url"],))
        self.assertEqual(transport.call_args.kwargs["headers"], {})
        self.assertIsNone(transport.call_args.kwargs["auth"])
        self.assertIs(transport.call_args.kwargs["httpx_client_factory"],
                      pilot._transport_client)
        self.assertIs(settings[0]["log_handler"], pilot._discard_server_log)
        self.assertIs(settings[0]["progress_handler"],
                      pilot._discard_server_progress)

    def test_wrong_live_schema_does_not_send_search(self):
        fake = OfflineClient(
            [exa_tool(required=("query", "objective"))],
            text_result("PRIVATE"))
        with (patch.object(pilot, "StreamableHttpTransport",
                           return_value=object()),
              patch.object(pilot, "Client", return_value=fake)):
            output = asyncio.run(pilot._one_request("exa", "docs"))
        self.assertEqual(output["status"], "unexpected_free_tool_contract")
        self.assertEqual(output["tool_calls_attempted"], 0)
        self.assertEqual(fake.calls, [("tools/list",)])

    def test_attended_throttle_is_one_known_call_and_no_secret_output(self):
        async def limited(provider, profile):
            self.assertEqual((provider, profile), ("exa", "docs"))
            return {
                "status": "rate_limited", "tool_calls_attempted": 1,
                "result_count": None, "local_call_ms": 80,
                "private": "PRIVATE_RATE_LIMIT_DIAGNOSTIC",
            }
        with (patch.object(pilot.os, "geteuid", return_value=1000),
              contextlib.redirect_stderr(io.StringIO()) as err):
            report = pilot.sample("exa", "docs",
                                  confirm=lambda _: "FREE", perform=limited)
        self.assertEqual(report["status"], "rate_limited")
        self.assertEqual(report["tool_calls_attempted"], 1)
        self.assertIsNone(report["result_count"])
        self.assertNotIn("PRIVATE_RATE_LIMIT_DIAGNOSTIC",
                         repr(report) + err.getvalue())
        self.assertNotIn(pilot.PRESETS["docs"], repr(report))


if __name__ == "__main__":
    unittest.main()
