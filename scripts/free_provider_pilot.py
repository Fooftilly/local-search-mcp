"""Attended, keyless, free-only external MCP search comparison (#258).

This is NOT a tool in the guarded local MCP bridge. By design it cannot
fetch URLs, modify engine settings, send arbitrary queries, authenticate,
start services, enroll for billing or perform unattended provider requests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from scripts.public_search_queries import PRESETS

# Fixed HTTPS origins; these are separate third-party hosted MCPs, never
# alternatives for the guarded bridge's web_fetch or private-domain policy.
PROVIDERS = {
    "you": {
        "url": "https://api.you.com/mcp?profile=free&tools=you-search",
        "tool": "you-search",
        "listed_tools": frozenset({"you-search", "you-discover"}),
        "limit_key": "count",
        "free": "Keyless profile: up to 100 search/discover requests per day",
    },
    "firecrawl": {
        "url": "https://mcp.firecrawl.dev/v2/mcp",
        "tool": "firecrawl_search",
        "listed_tools": frozenset({
            "firecrawl_search", "firecrawl_scrape", "firecrawl_parse"}),
        "limit_key": "limit",
        "free": ("Keyless: rate-limited free search; separate signed-up "
                 "Free plan renews 1,000 credits monthly"),
    },
    "exa": {
        # Exa's official MCP supports anonymous rate-limited search. Scope
        # discovery to search only; web_fetch_exa and paid agent_run stay absent.
        "url": "https://mcp.exa.ai/mcp?tools=web_search_exa",
        "tool": "web_search_exa",
        "listed_tools": frozenset({"web_search_exa"}),
        "limit_key": "numResults",
        "free": "Anonymous MCP search is free and rate-limited; quota unspecified",
    },
}
MAX_RESULTS = 3
MAX_TEXT_BYTES = 64 * 1024
MAX_TOOLS = 4
MAX_SECONDS = 30
FORBIDDEN_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "x-api-key",
    "payment-signature", "x-payment", "x-auth-token",
})


def preview() -> dict:
    """Pure offline declaration, no discovery, provider traffic or host reads."""
    return {
        "mode": "offline-preview",
        "status": "ready_for_explicit_consent",
        "providers": [
            {"name": key, "endpoint": value["url"], "tool": value["tool"],
             "free_allowance": value["free"], "requires_key": False}
            for key, value in PROVIDERS.items()
        ],
        "presets": list(PRESETS),
        "max_tool_calls_per_invocation": 1,
        "max_results_requested": MAX_RESULTS,
        "provider_calls_attempted": 0,
        "local_mcp_bridge_modified": False,
        "third_party_boundary": True,
        "note": ("Live sampling sends one fixed public query directly to an "
                 "external MCP provider. It bypasses the local bridge's "
                 "private history, domain approvals and root egress guard."),
    }


def client_config() -> dict:
    """Manual separate-client configuration; no file writes or activation."""
    return {
        "mcpServers": {
            "you-free": {"type": "http", "url": PROVIDERS["you"]["url"]},
            "firecrawl-keyless": {
                "type": "http", "url": PROVIDERS["firecrawl"]["url"]},
            "exa-keyless": {
                "type": "http", "url": PROVIDERS["exa"]["url"]},
        },
    }


def _transport_client(*, headers: dict | None = None,
                      timeout: httpx2.Timeout | None = None,
                      auth: httpx2.Auth | None = None,
                      follow_redirects: bool = False) -> httpx2.AsyncClient:
    """Reject accidental credentials/proxies/redirects in this public trial."""
    if auth is not None or any(
            str(key).lower() in FORBIDDEN_HEADERS for key in (headers or {})):
        raise ValueError("credentials_not_permitted_in_keyless_trial")
    # FastMCP 4 passes follow_redirects=True to custom client factories.
    # Accept its keyword for API compatibility, but never honor it: a
    # redirect would leave the explicitly pinned keyless MCP endpoint.
    # Use the SDK v2's httpx2 client, not the separate httpx v0.28 client.
    # Ignore the SDK timeout request in favor of stricter fixed bounds.
    return httpx2.AsyncClient(
        headers=headers or {}, auth=None, trust_env=False,
        follow_redirects=False, verify=True,
        timeout=httpx2.Timeout(12, connect=5, read=12, write=5),
    )


def _schema_ok(tools: object, provider: str) -> bool:
    """No paid-capability discovery or tool-schema widening is accepted."""
    if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS:
        return False
    spec = PROVIDERS[provider]
    found = {}
    for tool in tools:
        name = getattr(tool, "name", None)
        if (not isinstance(name, str) or name in found
                or name not in spec["listed_tools"]):
            return False
        found[name] = tool
    requested = found.get(spec["tool"])
    if requested is None:
        return False
    schema = getattr(requested, "inputSchema", None)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    props = schema.get("properties")
    required = schema.get("required", [])
    if not (
        isinstance(props, dict) and "query" in props
        and spec["limit_key"] in props
        and isinstance(props["query"], dict)
        and props["query"].get("type") == "string"
        and isinstance(props[spec["limit_key"]], dict)
        and props[spec["limit_key"]].get("type") in (
            ("integer", "number") if provider == "exa" else ("integer",))
        and isinstance(required, list)
        and set(required).issubset({"query"})
    ):
        return False
    # Some valid You.com search-only contracts do not expose extraction.
    # Send 'none' only if explicitly advertised; never send undocumented
    # arguments when additionalProperties is false.
    if provider == "you" and "extraction" in props:
        setting = props["extraction"]
        if (not isinstance(setting, dict)
                or setting.get("type") != "string"
                or ("enum" in setting
                    and "none" not in setting["enum"])):
            return False
    return True


def _safe_hits(data: object) -> list[dict] | None:
    """Only count structured URL-bearing results; never expose their content."""
    if isinstance(data, str):
        if len(data.encode("utf-8")) > MAX_TEXT_BYTES:
            return None
        try:
            data = json.loads(data)
        except (ValueError, UnicodeError):
            return None
    if isinstance(data, dict):
        if data.get("success") is False:
            return None
        choices = [data.get("results"), data.get("data"), data.get("web")]
        for key in ("results", "data"):
            nested = data.get(key)
            if isinstance(nested, dict):
                choices.extend((nested.get("web"), nested.get("results")))
    else:
        choices = [data]
    for candidate in choices:
        if not isinstance(candidate, list) or len(candidate) > MAX_RESULTS:
            continue
        accepted = True
        for row in candidate:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                accepted = False
                break
            try:
                parts = urlsplit(row["url"])
                if (parts.scheme not in ("http", "https")
                        or not parts.hostname or parts.username or parts.password
                        or parts.port not in (None, 80, 443)):
                    accepted = False
                    break
            except ValueError:
                accepted = False
                break
        if accepted:
            return candidate
    return None


def _redacted_result(result: object) -> dict:
    if getattr(result, "is_error", True) is not False:
        return {"status": "tool_error", "result_count": None}
    data = getattr(result, "data", None)
    hits = _safe_hits(data)
    if hits is None:
        content = getattr(result, "content", [])
        if (isinstance(content, list) and len(content) == 1
                and isinstance(getattr(content[0], "text", None), str)):
            hits = _safe_hits(content[0].text)
    if hits is None:
        return {"status": "unverified_response", "result_count": None}
    hosts = {urlsplit(row["url"]).hostname.casefold() for row in hits}
    return {"status": "results" if hits else "empty",
            "result_count": len(hits), "distinct_hosts": len(hosts)}


_EXA_URL_LINE = re.compile(r"^URL:[ \t]*(https?://\S+)[ \t]*$", re.ASCII)


def _redacted_exa_result(result: object) -> dict:
    """Count Exa's official text results without printing page content.

    Exa web_search_exa returns Title/URL/Published/Highlights blocks, not
    necessarily structured JSON. An anonymous quota refusal can also be a
    successful MCP response containing explanatory text rather than isError.
    """
    structured = _redacted_result(result)
    if structured["status"] != "unverified_response":
        return structured
    content = getattr(result, "content", None)
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(getattr(content[0], "text", None), str)):
        return structured
    raw = content[0].text
    if len(raw.encode("utf-8")) > MAX_TEXT_BYTES:
        return structured
    # This documented free-tier throttle sometimes arrives with MCP isError
    # unset. Never misreport it as an empty or successful search.
    if raw.startswith("You've hit Exa's free MCP rate limit."):
        return {"status": "rate_limited", "result_count": None}
    if raw.strip() == "No search results found. Please try a different query.":
        return {"status": "empty", "result_count": 0, "distinct_hosts": 0}

    # Exa formats each search hit as a result header containing Title,
    # URL, Published and Author, separated from other hits by a divider.
    # A page highlight can itself contain 'URL:' lines; do not scan the
    # entire body for URLs or count highlight text as additional results.
    blocks = raw.split("\n\n---\n\n")
    if not 1 <= len(blocks) <= MAX_RESULTS:
        return structured
    hosts = []
    for block in blocks:
        lines = block.splitlines()
        if (len(lines) < 4 or not lines[0].startswith("Title: ")
                or not lines[2].startswith("Published: ")
                or not lines[3].startswith("Author: ")):
            return structured
        match = _EXA_URL_LINE.fullmatch(lines[1])
        if match is None:
            return structured
        try:
            parsed = urlsplit(match.group(1))
            if (parsed.scheme not in ("https", "http")
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port not in (None, 80, 443)):
                return structured
        except ValueError:
            return structured
        hosts.append(parsed.hostname.casefold())
    return {"status": "results", "result_count": len(hosts),
            "distinct_hosts": len(set(hosts))}


async def _discard_server_log(_message: object) -> None:
    """Never forward untrusted MCP logging notification contents to stderr."""
    return None


async def _discard_server_progress(
        _progress: float, _total: float | None, _message: str | None) -> None:
    """Provider progress text may include URLs, errors or result snippets."""
    return None


async def _one_request(provider: str, preset: str) -> dict:
    """One MCP initialization, one tools/list and at most one tools/call."""
    spec = PROVIDERS[provider]
    transport = StreamableHttpTransport(
        spec["url"], headers={}, auth=None,
        httpx_client_factory=_transport_client,
    )
    # FastMCP's default log handler writes server-controlled data to Python
    # logging; its default progress handler can also log provider messages.
    # Neither may bypass the pilot's deliberately redacted output contract.
    async with Client(
            transport, log_handler=_discard_server_log,
            progress_handler=_discard_server_progress) as client:
        tools = await client.list_tools()
        if not _schema_ok(tools, provider):
            return {"status": "unexpected_free_tool_contract",
                    "tool_calls_attempted": 0, "result_count": None}
        args = {"query": PRESETS[preset], spec["limit_key"]: MAX_RESULTS}
        if provider == "you":
            declared = next(t for t in tools if t.name == spec["tool"])
            if "extraction" in declared.inputSchema["properties"]:
                # Never enable full-page extraction, and never send an
                # undeclared argument to a strict input schema.
                args["extraction"] = "none"
        before = time.monotonic()
        result = await client.call_tool(spec["tool"], args, raise_on_error=False)
        elapsed = max(0, round((time.monotonic() - before) * 1000))
        return {"tool_calls_attempted": 1, "local_call_ms": elapsed,
                **(_redacted_exa_result(result) if provider == "exa"
                   else _redacted_result(result))}


def sample(provider: str, preset: str, *, confirm=input,
           perform=_one_request) -> dict:
    """One operator-approved public query, no background or caller query text."""
    result = {
        "mode": "attended-external",
        "provider": provider if provider in PROVIDERS else "invalid",
        "profile": preset if preset in PRESETS else "invalid",
        "status": "refused", "tool_calls_attempted": 0,
        "result_count": None, "third_party_boundary": True,
        "remote_requests_may_have_started": False,
        "local_mcp_bridge_modified": False,
    }
    if provider not in PROVIDERS or preset not in PRESETS or os.geteuid() == 0:
        return result
    print(
        f"Provider: {provider}. One fixed PUBLIC query will be sent directly "
        "to the provider's keyless external MCP. This is outside local "
        "domain approval, root egress isolation and private query history. "
        "No API key, OAuth, cookies, payment credentials or paid fallback. "
        "The provider's own anonymous free quota applies. Type FREE to proceed.",
        file=sys.stderr,
    )
    try:
        approved = confirm("Confirmation: ")
    except (EOFError, KeyboardInterrupt):
        approved = ""
    if approved != "FREE":
        result["status"] = "cancelled"
        return result
    # Fail closed on network/protocol/schema exceptions. Never print
    # third-party response text, URLs, upstream exceptions or credentials.
    # A failed MCP exchange might still have sent the tool request before
    # the transport disconnected. Never claim zero upstream calls from an
    # exception; accurately mark the accounting as unknown in that case.
    result["status"] = "connection_failed"
    result["tool_calls_attempted"] = None
    result["remote_requests_may_have_started"] = True
    try:
        outcome = asyncio.run(asyncio.wait_for(
            perform(provider, preset), timeout=MAX_SECONDS))
        if (not isinstance(outcome, dict)
                or type(outcome.get("tool_calls_attempted")) is not int
                or outcome["tool_calls_attempted"] not in (0, 1)
                or outcome.get("status") not in (
                    "results", "empty", "tool_error", "rate_limited",
                    "unverified_response", "unexpected_free_tool_contract")):
            return result
        result.update({
            "status": outcome["status"],
            "tool_calls_attempted": outcome["tool_calls_attempted"],
            "result_count": outcome.get("result_count")
            if type(outcome.get("result_count")) is int
            and 0 <= outcome["result_count"] <= MAX_RESULTS else None,
        })
        if (type(outcome.get("distinct_hosts")) is int
                and 0 <= outcome["distinct_hosts"] <= MAX_RESULTS):
            result["distinct_hosts"] = outcome["distinct_hosts"]
        if (type(outcome.get("local_call_ms")) is int
                and 0 <= outcome["local_call_ms"] <= MAX_SECONDS * 1000):
            result["local_call_ms"] = outcome["local_call_ms"]
    except Exception:
        pass
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--sample", action="store_true")
    actions.add_argument("--client-config", action="store_true")
    parser.add_argument("--provider", choices=tuple(PROVIDERS))
    parser.add_argument("--profile", choices=tuple(PRESETS), default="docs")
    args = parser.parse_args(argv)
    if args.client_config:
        result = client_config()
    elif args.sample:
        if args.provider is None:
            parser.error("--sample requires --provider")
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            result = {"status": "tty_required", "tool_calls_attempted": 0}
        else:
            result = sample(args.provider, args.profile)
    else:
        result = preview()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("status") in (
        "ready_for_explicit_consent", "results", "empty") or args.client_config else 1


if __name__ == "__main__":
    raise SystemExit(main())
