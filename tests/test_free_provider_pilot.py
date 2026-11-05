"""Offline regressions for attended keyless provider trials (#258)."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json

from fastmcp.client.transports import http as fastmcp_http
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import free_provider_pilot as pilot
from scripts import engine_live_comparison


def tool(name, *, limit_key="count", extra_required=(),
         with_extraction=False):
    properties = {
        "query": {"type": "string"},
        limit_key: {"type": "integer"},
    }
    if with_extraction:
        properties["extraction"] = {
            "type": "string", "enum": ["none", "highlights", "full_page"]}
    return SimpleNamespace(name=name, inputSchema={
        "type": "object", "additionalProperties": False,
        "properties": properties,
        "required": ["query", *extra_required],
    })


class FakeClient:
    def __init__(self, items, result=None):
        self.items = items
        self.result = result or SimpleNamespace(
            is_error=False, data={"results": {"web": [
                {"url": "https://docs.python.org/3/"},
                {"url": "https://www.python.org/about"},
                {"url": "https://docs.python.org/3/library"},
            ]}})
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def list_tools(self):
        self.calls.append(("tools/list",))
        return self.items

    async def call_tool(self, name, args, *, raise_on_error=True):
        self.calls.append(("tools/call", name, args, raise_on_error))
        return self.result


class FreeProviderPilotTests(unittest.TestCase):
    def test_offline_preview_and_config_are_exact_and_nonmutating(self):
        with (patch.object(pilot, "_one_request",
                           side_effect=AssertionError("no network")),
              patch.object(pilot, "StreamableHttpTransport",
                           side_effect=AssertionError("no transport")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(pilot.main([]), 0)
        view = json.loads(output.getvalue())
        self.assertEqual(view["provider_calls_attempted"], 0)
        self.assertTrue(view["third_party_boundary"])
        self.assertEqual(view["max_tool_calls_per_invocation"], 1)
        self.assertEqual(len(view["providers"]), 2)
        self.assertFalse(view["local_mcp_bridge_modified"])
        self.assertEqual(view["presets"], list(engine_live_comparison.PRESETS))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pilot.main(["--client-config"]), 0)
        config = json.loads(output.getvalue())
        self.assertEqual(set(config["mcpServers"]),
                         {"you-free", "firecrawl-keyless"})
        self.assertTrue(config["mcpServers"]["you-free"]["url"].endswith(
            "?profile=free&tools=you-search"))
        self.assertEqual(config["mcpServers"]["firecrawl-keyless"]["url"],
                         "https://mcp.firecrawl.dev/v2/mcp")
        self.assertNotIn("Authorization", str(config))
        self.assertNotIn("Bearer", str(config))

    def test_noninteractive_and_invalid_flags_never_call_any_provider(self):
        with (patch.object(pilot.sys.stdin, "isatty", return_value=False),
              patch.object(pilot, "_one_request",
                           side_effect=AssertionError("must be offline")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            code = pilot.main(["--sample", "--provider", "you"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"],
                         "tty_required")
        with self.assertRaises(SystemExit):
            pilot.main(["--sample", "--client-config"])
        with self.assertRaises(SystemExit):
            pilot.main(["--sample"])
        with self.assertRaises(SystemExit):
            pilot.main(["--sample", "--provider", "paid"])
        with self.assertRaises(SystemExit):
            pilot.main(["--sample", "--provider", "you",
                        "--profile", "secret_query"])

    def test_refuses_root_unknown_inputs_and_wrong_consent(self):
        with (patch.object(pilot.os, "geteuid", return_value=0),
              patch.object(pilot, "_one_request",
                           side_effect=AssertionError("no provider"))):
            self.assertEqual(pilot.sample(
                "you", "docs", confirm=lambda _: "FREE")["status"], "refused")
        with (patch.object(pilot.os, "geteuid", return_value=1000),
              patch.object(pilot, "_one_request",
                           side_effect=AssertionError("no provider"))):
            self.assertEqual(pilot.sample(
                "EXA", "docs", confirm=lambda _: "FREE")["status"], "refused")
            self.assertEqual(pilot.sample(
                "you", "my private query", confirm=lambda _: "FREE")["status"],
                "refused")
            for answer in ("free", "YES", "", "FREE "):
                with self.subTest(answer=answer):
                    self.assertEqual(pilot.sample(
                        "you", "docs", confirm=lambda _: answer)["status"],
                        "cancelled")

    def test_hardcoded_hosts_tools_and_query_preset_are_fixed(self):
        self.assertEqual(set(pilot.PROVIDERS), {"you", "firecrawl"})
        for spec in pilot.PROVIDERS.values():
            self.assertTrue(spec["url"].startswith("https://"))
            self.assertNotIn("oauth", spec["url"])
            self.assertNotIn("api_key", spec["url"])
            self.assertNotIn("google", spec["url"])
        self.assertEqual(pilot.PRESETS, engine_live_comparison.PRESETS)
        self.assertFalse(any("password" in q.casefold()
                             for q in pilot.PRESETS.values()))

    def test_factory_disables_proxies_redirects_and_auth(self):
        dummy = object()
        with patch.object(pilot.httpx2, "AsyncClient",
                          return_value=dummy) as maker:
            self.assertIs(pilot._transport_client(
                headers={"accept": "application/json"},
                follow_redirects=True), dummy)
        self.assertFalse(maker.call_args.kwargs["trust_env"])
        self.assertFalse(maker.call_args.kwargs["follow_redirects"])
        self.assertTrue(maker.call_args.kwargs["verify"])
        self.assertIsNone(maker.call_args.kwargs["auth"])
        for headers in (
            {"Authorization": "SECRET"}, {"Cookie": "SECRET"},
            {"X-Payment": "SECRET"}, {"Payment-Signature": "SECRET"},
            {"X-API-Key": "SECRET"},
        ):
            with self.subTest(headers=list(headers)), self.assertRaises(ValueError):
                pilot._transport_client(headers=headers)

    def test_real_fastmcp_transport_factory_contract_is_redirect_safe(self):
        """Exercise the actual FastMCP 4 connect_session -> factory boundary.

        No network and no fake StreamableHttpTransport constructor: entering
        the actual transport invokes our real SDK-compatible client factory.
        A fake stream and session prevent remote initialize or MCP calls.
        """
        witnessed = []

        @contextlib.asynccontextmanager
        async def offline_stream(url, *, http_client, max_sse_event_size):
            witnessed.append(("stream", url, http_client.follow_redirects,
                              max_sse_event_size))
            yield None, None

        class OfflineSession:
            def __init__(self, read, write, **kwargs):
                self.arguments = (read, write, kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        def checked_factory(**kwargs):
            witnessed.append(("factory", kwargs))
            actual = pilot._transport_client(**kwargs)
            self.assertFalse(actual.follow_redirects)
            self.assertIsNone(actual.auth)
            return actual

        transport = pilot.StreamableHttpTransport(
            pilot.PROVIDERS["you"]["url"], headers={}, auth=None,
            httpx_client_factory=checked_factory,
        )
        opts = SimpleNamespace(
            forward_incoming_headers=False, session_class=OfflineSession)

        async def run():
            async with transport.connect_session(transport_options=opts) as session:
                self.assertIsInstance(session, OfflineSession)

        with patch.object(fastmcp_http, "streamable_http_client", offline_stream):
            asyncio.run(run())
        self.assertEqual(witnessed[0][0], "factory")
        self.assertTrue(witnessed[0][1]["follow_redirects"])
        self.assertIsNone(witnessed[0][1]["auth"])
        self.assertEqual(
            witnessed[1], ("stream", pilot.PROVIDERS["you"]["url"],
                            False, transport.max_sse_event_size))

    def test_real_fastmcp_transport_rejects_injected_credentials(self):
        """The actual transport must not smuggle a bearer to the factory."""
        transport = pilot.StreamableHttpTransport(
            pilot.PROVIDERS["you"]["url"], headers={"Authorization": "SECRET"},
            auth=None, httpx_client_factory=pilot._transport_client)
        opts = SimpleNamespace(
            forward_incoming_headers=False,
            session_class=lambda *args, **kwargs: None)
        async def run():
            async with transport.connect_session(transport_options=opts):
                self.fail("Unauthorized transport unexpectedly connected")
        with self.assertRaisesRegex(ValueError, "credentials_not_permitted"):
            asyncio.run(run())

    def test_schema_accepts_only_keyless_allowlisted_tool_set(self):
        good = [tool("you-search"), tool("you-discover")]
        self.assertTrue(pilot._schema_ok(good, "you"))
        self.assertTrue(pilot._schema_ok(
            [tool("you-search", with_extraction=True)], "you"))
        self.assertTrue(pilot._schema_ok([
            tool("firecrawl_search", limit_key="limit"),
            tool("firecrawl_scrape", limit_key="limit"),
            tool("firecrawl_parse", limit_key="limit")], "firecrawl"))
        invalid = (
            good + [tool("you-finance")],
            good + [tool("you-search")],
            [tool("you-search", extra_required=("credit_card",))],
            [tool("you-search", limit_key="invalid_count")],
            [],
            None,
        )
        for case in invalid:
            with self.subTest(case=case):
                self.assertFalse(pilot._schema_ok(case, "you"))
        self.assertFalse(pilot._schema_ok(
            [tool("firecrawl_crawl", limit_key="limit")], "firecrawl"))

    def test_remote_client_only_searches_one_free_tool_with_bounded_args(self):
        you = FakeClient([tool("you-search")])
        firecrawl = FakeClient([tool("firecrawl_search", limit_key="limit")])
        clients = [you, firecrawl]
        constructors = []
        client_settings = []
        def transport(*args, **kwargs):
            constructors.append((args, kwargs))
            return object()
        with (patch.object(pilot, "StreamableHttpTransport",
                           side_effect=transport),
              patch.object(pilot, "Client",
                           side_effect=lambda _transport, **kwargs: (
                               client_settings.append(kwargs), clients.pop(0))[1])):
            first = __import__("asyncio").run(pilot._one_request("you", "docs"))
            second = __import__("asyncio").run(
                pilot._one_request("firecrawl", "science"))
        self.assertEqual(first["status"], "results")
        self.assertEqual(first["result_count"], 3)
        self.assertEqual(first["distinct_hosts"], 2)
        self.assertEqual(second["status"], "results")
        self.assertEqual(you.calls, [
            ("tools/list",),
            ("tools/call", "you-search", {
                "query": pilot.PRESETS["docs"], "count": 3}, False)])
        self.assertEqual(firecrawl.calls, [
            ("tools/list",),
            ("tools/call", "firecrawl_search", {
                "query": pilot.PRESETS["science"], "limit": 3}, False)])
        self.assertEqual(
            [x[0][0] for x in constructors],
            [pilot.PROVIDERS["you"]["url"],
             pilot.PROVIDERS["firecrawl"]["url"]])
        self.assertTrue(all(x[1]["headers"] == {} and x[1]["auth"] is None
                            for x in constructors))
        self.assertTrue(all(x[1]["httpx_client_factory"] is pilot._transport_client
                            for x in constructors))
        self.assertEqual(len(client_settings), 2)
        for options in client_settings:
            self.assertIs(options["log_handler"], pilot._discard_server_log)
            self.assertIs(options["progress_handler"],
                          pilot._discard_server_progress)

    def test_optional_extraction_none_only_if_declared(self):
        fake = FakeClient([tool("you-search", with_extraction=True)])
        with (patch.object(pilot, "StreamableHttpTransport",
                           return_value=object()),
              patch.object(pilot, "Client", return_value=fake)):
            report = __import__("asyncio").run(pilot._one_request("you", "docs"))
        self.assertEqual(report["status"], "results")
        self.assertEqual(fake.calls[1], (
            "tools/call", "you-search", {
                "query": pilot.PRESETS["docs"], "count": 3,
                "extraction": "none"}, False))
        # A strict schema advertising only query/count must not receive
        # an unknown extraction field; covered by the preceding test.

    def test_actual_fastmcp_notifications_never_log_provider_text(self):
        """Exercise the genuine FastMCP Client's callback wiring offline."""
        token = "UPSTREAM_PRIVATE_NOTIFY_TOKEN"
        transport = pilot.StreamableHttpTransport(
            pilot.PROVIDERS["you"]["url"], auth=None,
            httpx_client_factory=pilot._transport_client)
        # Constructor only; no network or transport connect. Inspect the
        # callback FastMCP 4.1.6 actually registers with ClientSession.
        client = pilot.Client(
            transport, log_handler=pilot._discard_server_log,
            progress_handler=pilot._discard_server_progress)
        notification = SimpleNamespace(
            level="error", logger="untrusted-provider",
            data={"message": token, "details": "https://secret.example/"})
        with (contextlib.redirect_stderr(io.StringIO()) as err,
              contextlib.redirect_stdout(io.StringIO()) as out,
              patch("fastmcp.client.logging.from_server_logger") as log,
              patch("fastmcp.client.progress.logger") as progress_log):
            asyncio.run(client._session_kwargs["logging_callback"](
                notification))
            asyncio.run(client._progress_handler(1.0, 2.0, token))
        self.assertNotIn(token, err.getvalue() + out.getvalue())
        self.assertNotIn("secret.example", err.getvalue() + out.getvalue())
        log.error.assert_not_called()
        log.warning.assert_not_called()
        progress_log.debug.assert_not_called()

    def test_definite_free_quota_tool_error_retains_known_call_count(self):
        quota_error = SimpleNamespace(
            is_error=True, data={"error": "PRIVATE_PROVIDER_QUOTA_DETAILS"},
            content=[SimpleNamespace(text="SECRET")])
        fake = FakeClient([tool("you-search")], result=quota_error)
        with (patch.object(pilot, "StreamableHttpTransport",
                           return_value=object()),
              patch.object(pilot, "Client", return_value=fake)):
            outcome = asyncio.run(pilot._one_request("you", "docs"))
        self.assertEqual(outcome["status"], "tool_error")
        self.assertEqual(outcome["tool_calls_attempted"], 1)
        self.assertIsNone(outcome["result_count"])
        self.assertEqual(fake.calls[-1], (
            "tools/call", "you-search",
            {"query": pilot.PRESETS["docs"], "count": 3}, False))
        self.assertNotIn("PRIVATE_PROVIDER_QUOTA_DETAILS", repr(outcome))
        async def completed(provider, profile):
            return outcome
        with patch.object(pilot.os, "geteuid", return_value=1000):
            report = pilot.sample("you", "docs",
                                  confirm=lambda _: "FREE", perform=completed)
        self.assertEqual(report["status"], "tool_error")
        self.assertEqual(report["tool_calls_attempted"], 1)
        self.assertNotIn("PRIVATE_PROVIDER_QUOTA_DETAILS", repr(report))

    def test_schema_mismatch_aborts_before_remote_search(self):
        fake = FakeClient([tool("firecrawl_crawl", limit_key="limit")])
        with (patch.object(pilot, "StreamableHttpTransport",
                           return_value=object()),
              patch.object(pilot, "Client", return_value=fake)):
            result = __import__("asyncio").run(
                pilot._one_request("firecrawl", "docs"))
        self.assertEqual(result["status"], "unexpected_free_tool_contract")
        self.assertEqual(result["tool_calls_attempted"], 0)
        self.assertEqual(fake.calls, [("tools/list",)])

    def test_results_are_sanitized_even_with_private_remote_fields(self):
        response = SimpleNamespace(is_error=False, data={"results": {"web": [
            {"url": "https://python.org/PRIVATE_SECRET",
             "title": "INTERNAL_DATA", "snippet": "DO_NOT_LOG"},
        ]}})
        sanitized = pilot._redacted_result(response)
        self.assertEqual(sanitized["result_count"], 1)
        self.assertEqual(sanitized["distinct_hosts"], 1)
        for secret in ("PRIVATE_SECRET", "INTERNAL_DATA", "DO_NOT_LOG"):
            self.assertNotIn(secret, json.dumps(sanitized))
        malformed = SimpleNamespace(
            is_error=False, data={"results": "SECRET"},
            content=[SimpleNamespace(text="PRIVATE")])
        self.assertEqual(pilot._redacted_result(malformed)["status"],
                         "unverified_response")
        self.assertEqual(pilot._redacted_result(SimpleNamespace(
            is_error=True, data={"error": "SECRET"}))["status"], "tool_error")

    def test_structured_and_text_result_formats_are_bounded(self):
        for data in (
            {"data": {"web": [{"url": "https://example.org/page"}]}},
            {"data": [{"url": "https://example.org/page"}]},
            [{"url": "https://example.org/page"}],
        ):
            self.assertEqual(len(pilot._safe_hits(data)), 1)
        for value in (
            {"results": [{"url": "file:///etc/passwd"}]},
            {"results": [{"url": "https://[malformed"}]},
            {"results": [{"url": "https://user:password@example.org"}]},
            {"results": [{"url": "https://example.org:5555"}]},
            {"results": [{"url": "https://example.org"}] * 4},
            "not-json" * 30000,
            {"success": False, "data": [{"url": "https://example.org"}]},
        ):
            self.assertIsNone(pilot._safe_hits(value))
        from_text = SimpleNamespace(
            is_error=False, data=None,
            content=[SimpleNamespace(text=json.dumps({
                "web": [{"url": "https://example.org"}]}))])
        self.assertEqual(pilot._redacted_result(from_text)["result_count"], 1)

    def test_one_attended_call_no_content_leaks_or_followup_calls(self):
        async def ok(provider, profile):
            self.assertEqual((provider, profile), ("you", "science"))
            return {"status": "results", "result_count": 2, "distinct_hosts": 1,
                    "local_call_ms": 500, "tool_calls_attempted": 1,
                    "private": "UNTRUSTED_SEARCH_RESULT"}
        with patch.object(pilot.os, "geteuid", return_value=1000):
            result = pilot.sample("you", "science", confirm=lambda _: "FREE",
                                  perform=ok)
        self.assertEqual(result["status"], "results")
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["tool_calls_attempted"], 1)
        self.assertNotIn("UNTRUSTED_SEARCH_RESULT", str(result))
        self.assertNotIn(pilot.PRESETS["science"], str(result))

    def test_no_exception_details_or_arbitrary_network_retry(self):
        attempts = []
        async def broken(provider, profile):
            attempts.append((provider, profile))
            raise RuntimeError("PRIVATE_TOKEN_TEST")
        with patch.object(pilot.os, "geteuid", return_value=1000):
            result = pilot.sample("firecrawl", "docs",
                                  confirm=lambda _: "FREE", perform=broken)
        self.assertEqual(attempts, [("firecrawl", "docs")])
        self.assertEqual(result["status"], "connection_failed")
        self.assertNotIn("PRIVATE_TOKEN_TEST", repr(result))
        self.assertIsNone(result["tool_calls_attempted"])
        self.assertTrue(result["remote_requests_may_have_started"])


if __name__ == "__main__":
    unittest.main()
