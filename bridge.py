"""A local, read-only MCP adapter for SearXNG and self-hosted Crawl4AI."""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import Literal
import os
import re
import time

import httpx
from fastmcp import FastMCP
from domain_policy import (evaluate_fetch_url, expired_grant_without_active_fallback,
                           is_domain_currently_allowed)
import domain_policy
from admin_ui import create_dashboard_server
from runtime_ports import MCP_PORT
from crawler_security import local_container_warning, verify_health
import threading
from urllib.parse import urlsplit, urlunsplit
from history_log import logged_tool
import isolated_engine_observations
import document_pages
import document_comparison
import request_cache
import structured_extraction
import jsonld_extraction
import cache_settings
import upstream_settings
from search_results import enrich_search_results, normalized_url
from batch_fetch import fetch_many
from scripts import on_demand_supervisor as on_demand

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
CRAWL4AI_URL = os.getenv("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")
CRAWL4AI_TOKEN = os.getenv("CRAWL4AI_API_TOKEN", "")
mcp = FastMCP("Local SearXNG and Crawl4AI")
# Worker-local guard: batch calls never consume the operator's one-use grant.
# ContextVar is reset in each worker even after errors/cancellation.
_BATCH_PERMANENT_ONLY = ContextVar("local_search_batch_permanent_only", default=False)
# This unauthenticated endpoint is deliberately loopback-only. Validate both
# request authority and browser Origin: binding localhost alone cannot prevent
# a DNS-rebinding attack from an untrusted browser tab.
MCP_BIND_HOST = "127.0.0.1"
MCP_ALLOWED_HOSTS = [f"127.0.0.1:{MCP_PORT}", f"localhost:{MCP_PORT}"]
MCP_ALLOWED_ORIGINS = [f"http://127.0.0.1:{MCP_PORT}", f"http://localhost:{MCP_PORT}"]
CRAWLER_SECURITY_WARNING = local_container_warning()


def _check_public_target(url: str) -> dict | None:
    """Return an actionable approval status without raising a FastMCP traceback."""
    return evaluate_fetch_url(url)


def _search_tokens(value: str, name: str) -> str:
    """Validate a short comma-separated SearXNG category or engine selection."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be comma-separated text")
    if not value:
        return ""
    parts = [part.strip() for part in value.split(",")]
    if (len(parts) > 8 or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", part)
        for part in parts
    ) or len({part.casefold() for part in parts}) != len(parts)):
        raise ValueError(f"{name} must contain 1 to 8 unique names of up to 40 characters")
    return ",".join(parts)


def _search_params(query: str, max_results: int, language: str,
                   categories: str, time_range: str, engines: str,
                   page: int) -> tuple[dict, int]:
    """Translate validated optional arguments to SearXNG's public search API."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        raise ValueError("Query must contain 1 to 300 characters")
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValueError("max_results must be an integer")
    max_results = max(1, min(max_results, 12))  # Preserve old clamping behavior.
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10:
        raise ValueError("page must be an integer from 1 through 10")
    # Match SearXNG's VALID_LANGUAGE_CODE plus its recognized 'auto'/'all'
    # selectors. Broad BCP-47 tags (including es-419) are not accepted by
    # the current upstream search-language parser.
    if not isinstance(language, str) or (
        language and language not in ("all", "auto")
        and not re.fullmatch(r"[a-z]{2,3}(?:-[a-zA-Z]{2})?", language)
    ):
        raise ValueError("language must be en, en-US, auto, all or a supported similar code")
    if not isinstance(time_range, str) or time_range not in ("", "day", "week", "month", "year"):
        raise ValueError("time_range must be day, week, month, or year")
    selected_categories = _search_tokens(categories, "categories")
    selected_engines = _search_tokens(engines, "engines")
    # SearXNG combines explicit engines and categories as a UNION, not an
    # intersection. Never silently query additional, unrequested providers.
    if selected_engines and selected_categories:
        raise ValueError("Choose categories or engines, not both; SearXNG combines them")
    # Internal/external !bangs take precedence over explicit engine selection.
    if (selected_engines or selected_categories) and re.search(
            r"(?<!\S)![^\s]+", query):
        raise ValueError("Explicit engine/category filters cannot be combined with !bang search syntax")
    params = {"q": query.strip(), "format": "json"}
    if language:
        params["language"] = language
    if selected_categories:
        params["categories"] = selected_categories
    if time_range:
        params["time_range"] = time_range
    if selected_engines:
        params["engines"] = selected_engines
    if page != 1:
        params["pageno"] = page
    return params, max_results


def _retry_request(operation, *, method: str = "GET"):
    """At most two attempts, with tighter semantics for browser crawl POSTs.

    Read timeouts and late response losses are NOT retried for POST: Crawl4AI
    may still be running the first expensive browser job after a timeout.
    """
    for attempt in range(2):
        try:
            response = operation()
        except httpx.RequestError as exc:
            can_retry = method != "POST" or isinstance(
                exc, (httpx.ConnectError, httpx.ConnectTimeout))
            if attempt == 0 and can_retry:
                time.sleep(0.15)
                continue
            return None
        # A definite upstream 5xx response is retryable, but ambiguous
        # transport failures after starting a crawl POST are not.
        transient = (429, 500, 502, 503, 504)
        if response.status_code in transient and attempt == 0:
            time.sleep(0.15)
            continue
        return response
    return None


def _upstream_error(response, backend: str) -> dict:
    code = response.status_code if response is not None else None
    if code == 429:
        status, reason = "rate_limited", "The local upstream rate-limited this request. Retry later."
    elif code is not None and code >= 500:
        status, reason = "upstream_unavailable", "The local upstream is temporarily unavailable."
    elif code is not None:
        status, reason = "upstream_rejected", "The local upstream rejected the request or its filters."
    else:
        status, reason = "upstream_unavailable", "The local upstream could not be reached after two bounded attempts."
    result = {"success": False, "status": status, "backend": backend, "reason": reason}
    if code is not None:
        result["http_status"] = code
    return result


def _result_host_filter(value: str) -> tuple[tuple[str, bool], ...]:
    """Validate local result filters: host or *.host (child domains only).

    Reuse the existing offline PSL, private-suffix and ambiguous-IDN rules.
    Returned search links are never approved for fetching by this selector.
    """
    if not isinstance(value, str):
        raise ValueError("allowed_result_domains must be comma-separated hostnames")
    if not value:
        return ()
    entries = value.split(",")
    if len(entries) > 8:
        raise ValueError("allowed_result_domains accepts at most 8 hosts")
    result = []
    seen = set()
    for entry in entries:
        token = entry.strip()
        descendants = token.startswith("*.")
        raw_host = token[2:] if descendants else token
        if not raw_host or "*" in raw_host:
            raise ValueError("allowed_result_domains requires public hostnames")
        try:
            host = domain_policy.normalize_domain(raw_host)
        except (ValueError, RuntimeError) as exc:
            raise ValueError("allowed_result_domains requires valid public hostnames") from exc
        key = (host, descendants)
        if key in seen:
            raise ValueError("allowed_result_domains contains duplicate hosts")
        seen.add(key)
        result.append(key)
    # Selector order has no semantic effect. Canonicalize so equivalent
    # scopes share one in-memory cache entry and cannot duplicate searches.
    return tuple(sorted(result))


def _result_host_allowed(host: str, patterns: tuple[tuple[str, bool], ...]) -> bool:
    return any(host.endswith("." + domain) if descendants else host == domain
               for domain, descendants in patterns)


def _search_failure_status(payload: dict) -> dict:
    """Describe only bounded upstream failure *names*, never error details.

    SearXNG versions may report unresponsive_engines as engine names or
    [engine, exception details] pairs. Exceptions can contain URLs, queries,
    credentials or remote diagnostics, so never copy them into agent output.
    """
    raw = payload.get("unresponsive_engines")
    if raw is None:
        return {
            "coverage": "no_failures_reported", "reported_count": 0,
            "unresponsive_engines": [], "unparsed_count": 0,
            "truncated": False,
        }
    if not isinstance(raw, list):
        return {
            "coverage": "unverifiable", "reported_count": None,
            "unresponsive_engines": [], "unparsed_count": None,
            "truncated": True,
        }
    names = []
    seen = set()
    unparsed = 0
    # Parse a bounded prefix even if a malicious/malformed source returns a
    # massive list. A separate truncation flag avoids false completeness.
    for item in raw[:128]:
        name = item[0] if isinstance(item, (list, tuple)) and item else item
        if (not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,39}", name) is None):
            unparsed += 1
            continue
        if name.casefold() not in seen:
            seen.add(name.casefold())
            if len(names) < 16:
                names.append(name)
    return {
        "coverage": ("partial" if raw else "no_failures_reported"),
        "reported_count": min(len(raw), 128),
        "unresponsive_engines": names,
        "unparsed_count": unparsed,
        "truncated": len(raw) > 128 or len(seen) > len(names),
    }


def _run_search(params: dict, max_results: int, *,
                deduplicate: bool = False,
                ranking: str = "searxng",
                include_published_date: bool = False,
                include_search_status: bool = False,
                include_image_metadata: bool = False,
                allowed_result_domains: tuple[tuple[str, bool], ...] = ()) -> list[dict] | dict:
    try:
        with on_demand.lease("searxng"):
            return _run_search_network(params, max_results, deduplicate=deduplicate,
                                       ranking=ranking,
                                       include_published_date=include_published_date,
                                       include_search_status=include_search_status,
                                       include_image_metadata=include_image_metadata,
                                       allowed_result_domains=allowed_result_domains)
    except on_demand.BackendUnavailable:
        return {"success": False, "status": "upstream_unavailable",
                "backend": "searxng",
                "reason": "On-demand search backend unavailable; check Docker ownership and setup, then retry."}


MAX_SEARCH_CONFIG_BYTES = 512 * 1024


def _verify_explicit_search_selection(client: httpx.Client, params: dict) -> dict | None:
    """Fail closed if explicitly selected engines/categories are unavailable.

    SearXNG silently ignores unknown engine/category names; an empty valid
    selection can fall back to default providers. Use only its existing
    bounded loopback /config read while the on-demand lease is held.
    """
    names = params.get("engines")
    categories = params.get("categories")
    if not names and not categories:
        return None
    failure = {"success": False, "status": "invalid_response", "backend": "searxng",
               "reason": "Could not validate selected search engines/categories; no search was sent."}
    try:
        with client.stream("GET", f"{SEARXNG_URL}/config") as response:
            if response.status_code != 200:
                return failure
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAX_SEARCH_CONFIG_BYTES:
                    return failure
                raw.extend(chunk)
        payload = json.loads(raw)
    except (httpx.RequestError, UnicodeError, ValueError, TypeError):
        return failure
    engines = payload.get("engines") if isinstance(payload, dict) else None
    if not isinstance(engines, list) or len(engines) > 512:
        return failure
    active = set()
    active_categories = set()
    for row in engines:
        if not isinstance(row, dict):
            return failure
        name = row.get("name")
        if (not isinstance(name, str) or not 1 <= len(name) <= 80 or
                type(row.get("enabled")) is not bool):
            return failure
        if row["enabled"]:
            active.add(name)
            if categories:
                groups = row.get("categories")
                if (not isinstance(groups, list) or not 1 <= len(groups) <= 16
                        or any(not isinstance(cat, str) or
                               not 1 <= len(cat) <= 80 or
                               not cat.isprintable() for cat in groups)):
                    return failure
                active_categories.update(groups)
    if not active:
        return failure
    if names and not set(names.split(",")).issubset(active):
        raise ValueError("Requested search engine is unknown or disabled in SearXNG")
    if categories and not set(categories.split(",")).issubset(active_categories):
        raise ValueError("Requested search category is unknown or has no enabled engines in SearXNG")
    return None


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def web_search_capabilities(category: str = "", engine: str = "",
                            max_engines: int = 0,
                            summary_only: bool = False) -> dict:
    """Discover currently enabled local SearXNG engines and categories.

    Performs one bounded loopback /config read and never starts Docker,
    issues a provider search, changes search-engine settings, or approves
    a crawler domain. This is an observed snapshot, not a promise that
    each engine supports every optional search filter.
    """
    for label, value in (("category", category), ("engine", engine)):
        if (not isinstance(value, str) or
                (value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", value))):
            raise ValueError(f"{label} must be one selectable name up to 40 characters")
    if category and engine:
        raise ValueError("Choose a category or an engine filter, not both")
    if type(max_engines) is not int or not 0 <= max_engines <= 100:
        raise ValueError("max_engines must be between 0 and 100; 0 means all")
    if type(summary_only) is not bool:
        raise ValueError("summary_only must be a boolean")
    failure = {
        "success": False, "status": "unavailable",
        "reason": "SearXNG engine inventory unavailable or invalid; "
                  "the backend may be sleeping. No search was sent.",
    }
    try:
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            with client.stream("GET", f"{SEARXNG_URL}/config") as response:
                if response.status_code != 200:
                    return failure
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > MAX_SEARCH_CONFIG_BYTES:
                        return failure
                    raw.extend(chunk)
        payload = json.loads(raw)
    except (httpx.RequestError, UnicodeError, ValueError, TypeError):
        return failure
    rows = payload.get("engines") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 512:
        return failure
    seen = set()
    enabled = []
    omitted_unselectable = 0
    omitted_unselectable_categories = 0
    categories = set()
    for row in rows:
        if not isinstance(row, dict):
            return failure
        name = row.get("name")
        group = row.get("categories")
        # SearXNG /config contains valid upstream names which this MCP
        # deliberately cannot accept as explicit selectors (e.g. "naver (KO)").
        # Reject malformed/control-bearing records, but do not let benign
        # unselectable names — even on disabled engines — hide Yahoo et al.
        if (not isinstance(name, str) or not 1 <= len(name) <= 80
                or not name.isprintable() or "\\" in name
                or name != name.strip()
                or name.casefold() in seen
                or type(row.get("enabled")) is not bool
                or not isinstance(group, list) or not 1 <= len(group) <= 16
                or any(not isinstance(cat, str) or not 1 <= len(cat) <= 40
                       or not cat.isprintable() or "\\" in cat
                       or cat != cat.strip()
                       for cat in group)):
            return failure
        seen.add(name.casefold())
        if row["enabled"]:
            # Report only names web_search(engines=...) can actually select.
            # Printable upstream punctuation is not an engine-selection
            # permission and must never broaden the search-argument grammar.
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", name) is None:
                omitted_unselectable += 1
                continue
            groups = sorted({
                cat for cat in group if re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", cat)
            })
            omitted_unselectable_categories += len(set(group)) - len(groups)
            enabled.append({"name": name, "categories": groups})
            categories.update(groups)
    enabled.sort(key=lambda item: item["name"].casefold())
    full = {
        "success": True, "status": "available",
        "enabled_engines": enabled, "categories": sorted(categories),
        "enabled_engine_count": len(enabled),
        "omitted_unselectable_engine_count": omitted_unselectable,
        "omitted_unselectable_category_count": omitted_unselectable_categories,
        "scope": "enabled engines selectable by web_search in bounded local /config",
    }
    if not (category or engine or max_engines or summary_only):
        return full
    matching = [row for row in enabled
                if (not category or category in row["categories"])
                and (not engine or row["name"] == engine)]
    if (category or engine) and not matching:
        return {"success": False, "status": "not_enabled",
                "reason": "Requested engine or category is not enabled/selectable."}
    matching_count = len(matching)
    chosen = matching[:max_engines] if max_engines else matching
    full.update({
        "enabled_engines": [] if summary_only else chosen,
        "categories": sorted({name for row in matching for name in row["categories"]}),
        "matching_engine_count": matching_count,
        "returned_engine_count": 0 if summary_only else len(chosen),
        "details_omitted": summary_only,
        "truncated": bool(max_engines and len(chosen) < matching_count),
        "filter": {"category": category, "engine": engine},
    })
    return full


def _isolated_default_recovery_params(client: httpx.Client, params: dict,
                                      first: dict) -> dict | None:
    """Select a proven-attempted, locally enabled search engine for one retry.

    SearXNG sometimes fails a combined default query while an individual
    already-attempted engine succeeds. Isolate DuckDuckGo only when the
    *default* request reported it unresponsive, and a bounded local /config
    read confirms it remains enabled. No new engine or backend is enabled,
    no !bang overrides, and explicit user selections are never replaced.
    """
    if params.get("engines") or params.get("categories"):
        return None
    if re.search(r"(?<!\S)![^\s]+", params.get("q", "")):
        return None
    # Examine at most the same bounded 128-entry prefix as the diagnostic
    # parser, independently of its 16-name *display* limit. Never inspect
    # or copy upstream exception text into tool output or history.
    failures = first.get("unresponsive_engines")
    if not isinstance(failures, list):
        return None
    def reported_duckduckgo(item: object) -> bool:
        name = item[0] if isinstance(item, (list, tuple)) and item else item
        return name == "duckduckgo"
    if not any(reported_duckduckgo(item) for item in failures[:128]):
        return None
    fallback = dict(params, engines="duckduckgo")
    try:
        validation = _verify_explicit_search_selection(client, fallback)
    except ValueError:
        return None
    return fallback if validation is None else None


def _run_search_network(params: dict, max_results: int, *,
                        deduplicate: bool = False,
                        ranking: str = "searxng",
                        include_published_date: bool = False,
                        include_search_status: bool = False,
                        include_image_metadata: bool = False,
                        allowed_result_domains: tuple[tuple[str, bool], ...] = ()) -> list[dict] | dict:
    initial_failure = None
    isolated_engine = None
    attempted_engine = None
    with httpx.Client(timeout=upstream_settings.effective_timeout("searxng_seconds"),
                      trust_env=False) as client:
        selection_failure = _verify_explicit_search_selection(client, params)
        if selection_failure is not None:
            return selection_failure
        started_search = time.monotonic()
        response = _retry_request(lambda: client.get(
            f"{SEARXNG_URL}/search", params=params))
        isolated_engine_observations.observe(
            params, response, (time.monotonic() - started_search) * 1000,
            source="explicit")
        # SearXNG can return HTTP 200 with no hits when its providers have
        # failed. Recover once, preserving the request or narrowing an
        # unrestricted default to an already-attempted, enabled engine.
        # Never broaden explicit selection or retry a healthy empty result.
        if response is not None and response.status_code == 200:
            try:
                first = response.json()
            except (ValueError, UnicodeError):
                first = None
            if (isinstance(first, dict)
                    and isinstance(first.get("results"), list)
                    and not first["results"]
                    and isinstance(first.get("unresponsive_engines"), list)
                    and first["unresponsive_engines"]):
                initial_failure = _search_failure_status(first)
                # When the unrestricted default query included DuckDuckGo
                # but failed, isolate that same already-attempted provider.
                # Otherwise retain the existing same-query recovery.
                isolated = _isolated_default_recovery_params(client, params, first)
                retry_params = isolated if isolated is not None else params
                attempted_engine = isolated["engines"] if isolated else None
                time.sleep(0.15)
                retry = None
                retry_started = time.monotonic()
                try:
                    retry = client.get(f"{SEARXNG_URL}/search", params=retry_params)
                    if retry.status_code == 200:
                        retry_payload = retry.json()
                        if (isinstance(retry_payload, dict)
                                and isinstance(retry_payload.get("results"), list)):
                            response = retry
                            if isolated is not None:
                                isolated_engine = isolated["engines"]
                except (httpx.RequestError, ValueError, UnicodeError):
                    # Preserve the first provider-failure result, rather than
                    # leaking network errors or treating a bad retry as data.
                    pass
                finally:
                    isolated_engine_observations.observe(
                        retry_params, retry,
                        (time.monotonic() - retry_started) * 1000,
                        source=("isolated_recovery" if isolated is not None
                                else "explicit"))
    if response is not None and response.status_code in (400, 422) and len(params) > 2:
        raise ValueError("Local SearXNG rejected the requested filters or page")
    if response is None or response.status_code >= 400:
        return _upstream_error(response, "searxng")
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        return {"success": False, "status": "invalid_response", "backend": "searxng",
                "reason": "Local SearXNG returned unreadable JSON."}
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return {"success": False, "status": "invalid_response", "backend": "searxng",
                "reason": "Local SearXNG returned an invalid results structure."}
    rows = payload["results"]
    if params.get("engines") or isolated_engine:
        allowed = (set(params["engines"].split(","))
                   if params.get("engines") else {isolated_engine})
        # A changed backend or upstream fallback must not return unrequested
        # provider results. The preflight above prevents *initiating* such
        # searches in the known SearXNG fallback cases.
        for row in rows:
            if not isinstance(row, dict):
                continue
            reported = row.get("engine")
            attributed = row.get("engines", [])
            if (reported not in allowed or
                    not isinstance(attributed, list) or
                    any(name not in allowed for name in attributed)):
                return {"success": False, "status": "upstream_selection_mismatch",
                        "backend": "searxng",
                        "reason": "SearXNG returned a result from an unselected search engine."}
    if not rows and payload.get("unresponsive_engines"):
        failure = {"success": False, "status": "upstream_unavailable", "backend": "searxng",
                   "reason": "SearXNG returned no results because one or more engines were unresponsive. Retry later or choose another engine."}
        if include_search_status:
            # Existing diagnostic opt-in, now useful for zero-result failures.
            # Never persist raw SearXNG exception details or input payload.
            failure["search_status"] = _search_failure_status(payload)
            if initial_failure is not None:
                failure["search_status"]["recovery"] = {
                    "attempted": True, "recovered": False,
                    "first_attempt": initial_failure,
                    "strategy": ("isolated_engine" if attempted_engine
                                 else "same_query"),
                }
                if attempted_engine:
                    failure["search_status"]["recovery"]["engine"] = attempted_engine
        return failure
    # Search engines may return rows without usable web links. Returning
    # "None", javascript: URLs or invalid Unicode as clickable results makes
    # agent tools appear successful while the returned links cannot be fetched.
    # Validate before the limit so a bad first row does not hide valid results.
    valid_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 2048:
            continue
        if (url != url.strip()
                or any(ord(c) < 0x20 or ord(c) == 0x7f for c in url)):
            continue
        try:
            url.encode("utf-8", errors="strict")
            parsed = urlsplit(url)
            if (normalized_url(url) is None
                    or parsed.port not in (None, 80, 443)):
                continue
            # Match web_fetch's local domain-policy validation without
            # queuing an approval request or performing a DNS lookup.
            hostname = parsed.hostname or ""
            if hostname != hostname.strip():
                continue
            domain_policy.normalize_domain(hostname)
        except (UnicodeError, ValueError):
            continue
        valid_rows.append(row)
    if rows and not valid_rows:
        return {"success": False, "status": "invalid_response", "backend": "searxng",
                "reason": "SearXNG returned results without any usable HTTP(S) links."}
    if allowed_result_domains:
        # Work only on validated SearXNG hits, before deduplication and
        # output limiting. Never follow a link or treat this as fetch approval.
        valid_rows = [
            row for row in valid_rows
            if _result_host_allowed(
                domain_policy.normalize_domain(urlsplit(row["url"]).hostname or ""),
                allowed_result_domains)
        ]
    result = enrich_search_results(
        valid_rows, max_results, deduplicate=deduplicate, ranking=ranking,
        include_published_date=include_published_date,
        include_image_metadata=include_image_metadata,
    )
    if include_search_status:
        diagnostic = _search_failure_status(payload)
        top_level = {
            "partial": "degraded",
            "unverifiable": "unknown",
            "no_failures_reported": "ok",
        }[diagnostic["coverage"]]
        if initial_failure is not None:
            diagnostic["recovery"] = {
                "attempted": True, "recovered": bool(rows),
                "first_attempt": initial_failure,
                "strategy": ("isolated_engine" if attempted_engine
                             else "same_query"),
            }
            if attempted_engine:
                diagnostic["recovery"]["engine"] = attempted_engine
            # A clean second attempt does not erase the observed failure on
            # the first attempt. Keep the diagnostic status honest.
            top_level = "degraded"
        return {"success": True, "status": top_level,
                "results": result, "search_status": diagnostic}
    return result


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
@logged_tool("search")
def web_search(query: str, max_results: int = 8, language: str = "",
               categories: str = "", time_range: str = "",
               engines: str = "", page: int = 1, fresh: bool = False,
               deduplicate: bool = False,
               ranking: Literal["searxng", "source_count"] = "searxng",
               include_published_date: bool = False,
               include_search_status: bool = False,
               allowed_result_domains: str = "",
               include_image_metadata: bool = False,
               record_provider_diagnostics: bool = False) -> list[dict] | dict:
    """Search SearXNG, with optional URL deduplication and source attribution.

    Defaults preserve SearXNG order and the historic four-field result shape.
    Set deduplicate=true to merge equivalent URLs; ranking="source_count"
    requires deduplicate=true and sorts by distinct reporting engine count,
    breaking ties by SearXNG order. ranking="searxng" preserves upstream order.
    Set fresh=true to bypass the in-memory search cache and fetch fresh data.
    Set include_published_date=true for validated upstream date-only metadata;
    it is not a verified publisher timestamp or a guarantee of freshness.
    When SearXNG returns no hits and reports unresponsive engines, retry once.
    For unfiltered defaults, DuckDuckGo may be isolated if it was already
    attempted and remains enabled; all other retries preserve the same query,
    filters and provider selections. Healthy empty results are not retried.
    Set include_search_status=true for an uncached diagnostic wrapper with
    bounded SearXNG-reported unresponsive provider names; absent failure
    reports do not prove exhaustive coverage. Recovery details are opt-in.
    Set record_provider_diagnostics=true *together* with include_search_status
    to opt in to private history retention of sanitized provider names and
    coverage only for this individual call. Default history never includes
    unresponsive provider details. Neither option measures provider uptime.
    Set include_image_metadata=true for validated, HTTPS-only image and
    thumbnail candidates from upstream image results; no downloads or
    crawler approvals. This does not verify image content or licenses.
    allowed_result_domains filters only already-returned results:
    "gov.me" matches only that host, "*.gov.me" matches child hosts only.
    It never changes provider selection or domain approvals.
    Choose categories OR engines: SearXNG combines both into a union.
    Explicit engine/category calls require locally verified enabled providers
    in /config; unsupported selectors fail closed before provider searches.
    """
    if not isinstance(fresh, bool):
        raise ValueError("fresh must be a boolean")
    if not isinstance(deduplicate, bool):
        raise ValueError("deduplicate must be a boolean")
    if not isinstance(include_published_date, bool):
        raise ValueError("include_published_date must be a boolean")
    if not isinstance(include_search_status, bool):
        raise ValueError("include_search_status must be a boolean")
    if type(record_provider_diagnostics) is not bool:
        raise ValueError("record_provider_diagnostics must be a boolean")
    if record_provider_diagnostics and not include_search_status:
        raise ValueError("record_provider_diagnostics requires include_search_status=true")
    if not isinstance(include_image_metadata, bool):
        raise ValueError("include_image_metadata must be a boolean")
    if not isinstance(ranking, str) or ranking not in ("searxng", "source_count"):
        raise ValueError("ranking must be searxng or source_count")
    if ranking == "source_count" and not deduplicate:
        raise ValueError("source_count ranking requires deduplicate=true")
    params, max_results = _search_params(
        query, max_results, language, categories, time_range, engines, page,
    )
    allowed_hosts = _result_host_filter(allowed_result_domains)
    # Avoid caching obviously sensitive-looking queries. No queries are saved
    # to disk by this cache; the separate opt-in history remains unchanged.
    private = bool(re.search(
        r"@|https?://|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S+|(?:password|token|secret|api[_-]?key|bearer|cookie)\s*[:=]",
        params["q"], flags=re.IGNORECASE))
    # Do not replay a cached "all providers healthy" snapshot as current
    # provider health. Diagnostic calls are always fresh and not cached.
    key = None if private or include_search_status else (
        "search", SEARXNG_URL, tuple(sorted(params.items())),
        max_results, deduplicate, ranking, include_published_date,
        include_image_metadata, allowed_hosts,
    )
    return request_cache.run(
        "search", key, lambda: _run_search(
            params, max_results, deduplicate=deduplicate, ranking=ranking,
            include_published_date=include_published_date,
            include_search_status=include_search_status,
            include_image_metadata=include_image_metadata,
            allowed_result_domains=allowed_hosts),
        ttl=lambda: cache_settings.effective_ttl('search_seconds'), fresh=fresh, should_store=lambda r: isinstance(r, list),
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def web_search_batch(queries: list[str], max_results: int = 5,
                           language: str = "", categories: str = "",
                           time_range: str = "", engines: str = "",
                           fresh: bool = False) -> dict:
    """Search 1–4 distinct public queries using existing web_search safeguards.

    At most two underlying searches run concurrently. Each item uses existing
    engine/category validation, cache, lease, attribution and private audit.
    Cancellation skips queued tasks but cannot stop already running threads.
    """
    if (not isinstance(queries, list) or not 1 <= len(queries) <= 4
            or any(not isinstance(query, str) for query in queries)):
        raise ValueError("queries must be a list of 1–4 public query strings")
    if type(max_results) is not int or not 1 <= max_results <= 8:
        raise ValueError("max_results must be between 1 and 8 for batch search")
    if type(fresh) is not bool:
        raise ValueError("fresh must be a boolean")
    cleaned = []
    for query in queries:
        # A search batch never delegates to SearXNG bang shortcuts, even
        # without explicit filters. Those could override provider selection
        # and broaden the queried engines unexpectedly.
        if re.search(r"(?<!\S)![^\s]+", query):
            raise ValueError("Bang provider shortcuts are not supported in batch search")
        params, _ = _search_params(query, max_results, language, categories,
                                   time_range, engines, 1)
        cleaned.append(params["q"])
    if len({query.casefold() for query in cleaned}) != len(cleaned):
        raise ValueError("queries must be distinct")
    semaphore = asyncio.Semaphore(2)

    async def run_one(query: str) -> dict:
        async with semaphore:
            try:
                output = await asyncio.to_thread(
                    web_search, query, max_results, language, categories,
                    time_range, engines, 1, fresh)
            except Exception:
                return {"query": query, "success": False, "status": "search_failed",
                        "reason": "The search could not complete."}
            if isinstance(output, list):
                return {"query": query, "success": True,
                        "result_count": len(output), "results": output}
            if isinstance(output, dict):
                return {"query": query, "success": False,
                        "status": str(output.get("status", "search_failed"))[:64],
                        "reason": str(output.get("reason", "Search unavailable"))[:200]}
            return {"query": query, "success": False, "status": "invalid_response",
                    "reason": "Search returned an invalid result."}

    results = await asyncio.gather(*(run_one(query) for query in cleaned))
    return {"success": all(item["success"] for item in results),
            "query_count": len(results), "results": results}


def _empty_extraction_diagnostic(page: dict, final_url: str) -> dict:
    """No usable Markdown was extracted; any access-gate hint is heuristic.

    Inspect only the verified final URL's final path segment and a short
    exact-match document title. Never return upstream diagnostic bodies,
    arbitrary metadata or page text from an empty/blocked crawl.
    """
    result = {
        "url": final_url, "success": False, "status": "empty_extraction",
        "reason": "Crawl4AI reported success but returned no usable page text.",
    }
    try:
        segment = urlsplit(final_url).path.rstrip("/").rsplit("/", 1)[-1].casefold()
    except ValueError:
        segment = ""
    if segment in ("login", "log-in", "signin", "sign-in", "captcha",
                   "challenge", "verify"):
        result["possible_access_gate"] = {
            "evidence": "final_url_path", "confidence": "heuristic",
        }
        return result
    meta = page.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        title = page.get("title")
    if isinstance(title, str) and len(title) <= 200 and title.strip().casefold() in (
            "just a moment...", "just a moment", "attention required!",
            "verify you are human", "security verification",
            "sign in", "log in", "login required"):
        result["possible_access_gate"] = {
            "evidence": "exact_document_title", "confidence": "heuristic",
        }
    return result


def _crawl_metadata(page: dict, final_url: str) -> dict:
    """Return only short, safe, untrusted public-page metadata."""
    raw = page.get("metadata")
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for key in ("title", "description"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            value = page.get(key)
        if isinstance(value, str) and value.strip():
            # Crawl4AI may return both very large metadata and unpaired
            # surrogates. Bound the string before UTF-8 normalization, so
            # the encoder only allocates for the portion we will return.
            limit = 240 if key == "title" else 1000
            cleaned = value.strip()[:limit].encode("utf-8", errors="replace").decode("utf-8")
            result[key] = cleaned
    canonical = raw.get("canonical_url") or raw.get("canonical")
    if isinstance(canonical, str):
        try:
            requested, target = urlsplit(final_url), urlsplit(canonical)
            if (target.scheme in ("http", "https") and target.hostname
                    and target.hostname.lower() == (requested.hostname or "").lower()
                    and target.username is None and target.password is None
                    and target.port in (None, 80, 443)):
                canonical_url = urlunsplit(
                    (target.scheme, target.netloc, target.path, "", "")
                )[:2000]
                # A malformed URL must not produce invalid JSON or a broken
                # canonical link. Unlike display text, do not rewrite URLs.
                canonical_url.encode("utf-8", errors="strict")
                result["canonical_url"] = canonical_url
        except (ValueError, UnicodeError):
            pass
    return result


def _run_fetch(url: str, mode: str, *,
               once_proof: tuple[str, str] | None = None,
               include_links: bool = False, include_tables: bool = False,
               include_outline: bool = False,
               include_alternatives: bool = False,
               compact_chars: int = document_pages.PAGE_CHARS,
               retain_for_evidence: bool = False,
               include_jsonld: bool = False) -> dict:
    try:
        with on_demand.lease("crawl4ai"):
            if (include_links or include_tables or include_outline or include_alternatives
                    or retain_for_evidence or include_jsonld or compact_chars != document_pages.PAGE_CHARS):
                return _run_fetch_network(
                    url, mode, once_proof=once_proof,
                    include_links=include_links, include_tables=include_tables,
                    include_outline=include_outline, include_alternatives=include_alternatives,
                    compact_chars=compact_chars, retain_for_evidence=retain_for_evidence,
                    include_jsonld=include_jsonld)
            return _run_fetch_network(url, mode, once_proof=once_proof)
    except on_demand.BackendUnavailable:
        return {"url": url, "success": False, "status": "upstream_unavailable",
                "reason": "On-demand crawler unavailable; verify Docker ownership and the live egress probe, then retry."}


def _run_fetch_network(url: str, mode: str, *,
                       once_proof: tuple[str, str] | None = None,
                       include_links: bool = False, include_tables: bool = False,
                       include_outline: bool = False,
                       include_alternatives: bool = False,
                       compact_chars: int = document_pages.PAGE_CHARS,
                       retain_for_evidence: bool = False,
                       include_jsonld: bool = False) -> dict:
    """Crawl only after the caller has passed current local policy checks."""
    headers = {"Authorization": f"Bearer {CRAWL4AI_TOKEN}"} if CRAWL4AI_TOKEN else {}
    crawler_params = {"stream": False}
    if mode == "fit":
        # Stock Crawl4AI only generates fit Markdown when a content filter
        # is configured. These are upstream-allowlisted safe config classes.
        crawler_params["markdown_generator"] = {
            "type": "DefaultMarkdownGenerator",
            "params": {"content_filter": {
                "type": "PruningContentFilterLXML", "params": {}
            }},
        }
    payload = {
        "urls": [url],
        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": crawler_params},
    }
    with httpx.Client(timeout=upstream_settings.effective_timeout("crawl_seconds"),
                      trust_env=False) as client:
        try:
            health = _retry_request(lambda: client.get(
                f"{CRAWL4AI_URL}/health", timeout=8))
            if health is None:
                return {"url": url, "success": False, "status": "blocked",
                        "reason": "Crawl4AI security check unavailable: upstream connection error"}
            health.raise_for_status()
            health_error = verify_health(health.json())
        except (httpx.HTTPError, ValueError) as exc:
            return {"url": url, "success": False, "status": "blocked", "reason": f"Crawl4AI security check unavailable: {type(exc).__name__}"}
        if health_error:
            return {"url": url, "success": False, "status": "blocked", "reason": health_error}
        response = _retry_request(lambda: client.post(
            f"{CRAWL4AI_URL}/crawl", json=payload, headers=headers), method="POST")
        if response is None or response.status_code >= 400:
            return {"url": url, **_upstream_error(response, "crawl4ai")}
        try:
            result = response.json()
        except (ValueError, UnicodeError):
            return {"url": url, "success": False, "status": "invalid_response",
                    "reason": "Crawl4AI returned unreadable JSON."}
    if isinstance(result, dict) and "results" in result:
        results = result["results"]
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return {"url": url, "success": False, "status": "invalid_response",
                    "reason": "Crawl4AI returned an empty or invalid page list."}
        page = results[0]
    elif isinstance(result, list):
        page = result[0] if result and isinstance(result[0], dict) else None
    else:
        page = result
    if not isinstance(page, dict):
        return {"url": url, "success": False, "status": "invalid_response",
                "reason": "Crawl4AI returned an invalid page structure."}
    reported_success = page.get("success", result.get("success") if isinstance(result, dict) else None)
    if not isinstance(reported_success, bool):
        return {"url": url, "success": False, "status": "invalid_response",
                "reason": "Crawl4AI omitted a valid success flag from its page result."}
    # CrawlResult.url is the REQUESTED URL; CrawlResult.redirected_url records
    # the final navigation, including JavaScript redirects. Never assume that
    # the original URL proves the final destination is approved.
    redirected_url = page.get("redirected_url")
    original_url = page.get("url", url)
    # An explicit empty or malformed redirected_url is not proof of a safe
    # destination. Only an absent/None field preserves legacy behavior.
    if not isinstance(original_url, str) or not original_url or (
        redirected_url is not None and (
            not isinstance(redirected_url, str) or not redirected_url
        )
    ):
        return {"url": url, "success": False, "status": "blocked", "reason": "Unexpected Crawl4AI destination URL"}
    final_url = original_url if redirected_url is None else redirected_url
    # Defense in depth: never return content from an unapproved redirect.
    # Browser/container egress controls must independently block unsafe traffic.
    if final_url != url:
        if once_proof is not None:
            # A same-host redirect is part of the claimed operation; it must
            # still resolve exclusively to public unicast addresses and use
            # a supported URL/port. Different hosts need their own ordinary
            # (reusable) approval, never a second one-use claim.
            try:
                same_host = (domain_policy.normalize_domain(
                    urlsplit(final_url).hostname or "") == once_proof[0])
            except (ValueError, TypeError):
                same_host = False
            if same_host:
                destination_ok = (domain_policy._public_url_host(final_url)
                                  == once_proof[0])
            else:
                destination_ok = _check_public_target(final_url) is None
        else:
            destination_ok = _check_public_target(final_url) is None
        if not destination_ok:
            return {"url": url, "success": False, "status": "blocked",
                    "reason": "Crawl4AI redirected to a destination that is not approved"}
    # Crawl4AI's 'success' means its pipeline ran, not that the remote
    # document exists. A page can contain a fully extracted HTTP 404 body.
    # For redirects, the final response code wins over the initial 3xx.
    def http_status(value):
        return value if type(value) is int and 100 <= value <= 599 else None

    first_status = http_status(page.get("status_code"))
    final_status = http_status(page.get("redirected_status_code"))
    # Do not mistake a successfully followed 302->200 for an HTTP error.
    # An initial redirect without a final code is not final status proof.
    effective_status = final_status if final_status is not None else (
        None if first_status is not None and 300 <= first_status < 400
        and redirected_url is not None else first_status)
    if effective_status is not None and 400 <= effective_status <= 599:
        return {"url": final_url, "success": False, "status": "http_error",
                "http_status": effective_status,
                "reason": f"Target page returned HTTP {effective_status}; content was not returned."}
    if not reported_success:
        # Upstream error_message may contain tokens, paths, and private URLs.
        # Never echo it (nor potentially misleading partially extracted page
        # text/metadata) to the agent.
        return {"url": final_url, "success": False, "status": "crawl_failed",
                "reason": "Crawl4AI could not fetch or extract this page."}
    markdown_response = page.get("markdown", "")
    if isinstance(markdown_response, dict):
        raw = markdown_response.get("raw_markdown")
        fit = markdown_response.get("fit_markdown")
    else:
        raw, fit = markdown_response, page.get("fit_markdown")
    if mode == "fit":
        markdown = fit
    elif isinstance(raw, str) and not raw.strip() and isinstance(fit, str) and fit.strip():
        # Blank raw markup must not conceal a usable fitted extraction.
        markdown = fit
    else:
        markdown = raw or fit or ""
    if mode == "fit" and (markdown is None or markdown == ""):
        return {"url": final_url, "success": False, "status": "unavailable_extraction",
                "reason": "Crawl4AI did not provide fit Markdown for this page"}
    if not isinstance(markdown, str):
        return {"url": final_url, "success": False, "status": "invalid_extraction",
                "reason": "Crawl4AI returned an invalid Markdown representation"}
    if not markdown.strip():
        return _empty_extraction_diagnostic(page, final_url)
    if len(markdown) > document_pages.MAX_DOCUMENT_CHARS:
        return {"url": final_url, "success": False, "status": "document_too_large",
                "reason": "Document exceeds the bounded extraction limit; content was not silently truncated"}
    # Invalid surrogate code points cannot safely be encoded as UTF-8 in MCP
    # JSON responses. Replace them deterministically before chunking.
    markdown = markdown.encode("utf-8", errors="replace").decode("utf-8")
    success = reported_success
    metadata = _crawl_metadata(page, final_url)
    output = {
        "url": final_url,
        "success": success,
        "markdown": markdown[:compact_chars],
        "truncated": len(markdown) > compact_chars,
    }
    if metadata:
        output["metadata"] = metadata
    if include_outline:
        output.update(document_pages.build_outline(
            markdown, first_page_chars=compact_chars))
    if include_jsonld:
        # CrawlResult.html is already returned by THIS authorized crawl.
        # Parse only a bounded prefix, never execute scripts or visit URLs.
        output.update(jsonld_extraction.extract(page))
    if include_links or include_tables or include_alternatives:
        # Alternative format groups can be pruned from fit Markdown. Inspect
        # the same crawl's bounded raw text when available, without changing
        # the mode-selected Markdown returned to the caller.
        alternatives_source = markdown
        if include_alternatives and isinstance(raw, str) and raw:
            alternatives_source = raw[:document_pages.MAX_DOCUMENT_CHARS]
            alternatives_source = alternatives_source.encode(
                "utf-8", errors="replace").decode("utf-8")
        # Only parse already fetched, approved data; never visit listed URLs.
        output.update(structured_extraction.extract(
            page, final_url, include_links=include_links, include_tables=include_tables,
            include_alternatives=include_alternatives, markdown=alternatives_source))
    if include_links and success is True:
        link_info = output.get("structured_info", {}).get("links", {})
        if link_info.get("truncated"):
            retained = structured_extraction.prepare_link_continuation(
                page, final_url, output.get("links", []))
            if retained is not None:
                link_id = document_pages.save_links(
                    final_url, retained, source_url=url, once_proof=once_proof)
                output["link_continuation"] = {
                    "link_id": link_id,
                    "retained_links": len(retained["entries"]),
                    "source_links_observed": retained["source_links_observed"],
                    "retained_truncated": retained["retained_truncated"],
                    "input_scan_capped": retained["input_scan_capped"],
                    "source_completeness": "unknown",
                }
    if include_tables and success is True:
        table_info = output.get("structured_info", {}).get("tables", {})
        needs_pages = (table_info.get("truncated") or
                       any(item.get("rows_truncated") or item.get("columns_truncated")
                           for item in table_info.get("table_limits", [])))
        if needs_pages:
            payload = structured_extraction.prepare_table_continuation(page)
            if payload is not None:
                table_id = document_pages.save_tables(
                    final_url, payload, source_url=url, once_proof=once_proof)
                output["table_continuation"] = {
                    "table_id": table_id,
                    "source_tables_observed": payload["source_tables_observed"],
                    "retained_table_indices": [entry["table_index"]
                                               for entry in payload["entries"]],
                    "retention_truncated": payload["retention_truncated"],
                    "source_completeness": "unknown",
                }
    if len(markdown) > compact_chars and success is not False:
        token, page_count = document_pages.save(
            final_url, markdown, metadata, source_url=url, once_proof=once_proof,
            first_page_chars=compact_chars)
        output.update({"document_id": token, "page": 1,
                       "page_count": page_count, "next_page": 2})
    elif len(markdown) > compact_chars:
        return {"url": final_url, "success": False, "status": "crawl_failed",
                "reason": "Crawl4AI reported failure; no incomplete document was cached"}
    elif retain_for_evidence:
        # Unlike ordinary short fetches, explicitly opted-in text is held
        # temporarily under the same authorization, TTL and memory limits.
        token, count = document_pages.save(
            final_url, markdown, metadata, source_url=url, once_proof=once_proof,
            first_page_chars=compact_chars, allow_short=True)
        output.update({"document_id": token, "page": 1, "page_count": count,
                       "retained_for_evidence": True})
    return output


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
@logged_tool("fetch")
def web_fetch(url: str, mode: str = "raw", fresh: bool = False,
              include_links: bool = False, include_tables: bool = False,
              include_outline: bool = False,
              compact_chars: int = document_pages.PAGE_CHARS,
              include_retrieval_info: bool = False,
              include_alternatives: bool = False,
              retain_for_evidence: bool = False,
              include_jsonld: bool = False) -> dict:
    """Fetch an approved page; bounded memory cache for public short pages.

    Authorization runs before every cache hit. Fresh skips both cache and
    concurrent request coalescing. Long paged responses are never cached.
    """
    if mode not in ("raw", "fit"):
        raise ValueError("mode must be raw or fit")
    if not isinstance(fresh, bool):
        raise ValueError("fresh must be a boolean")
    if not all(isinstance(flag, bool) for flag in
               (include_links, include_tables, include_outline, include_alternatives)):
        raise ValueError("include_links, include_tables, include_outline and include_alternatives must be booleans")
    if (type(compact_chars) is not int or
            not document_pages.MIN_FIRST_PAGE_CHARS <= compact_chars <= document_pages.PAGE_CHARS):
        raise ValueError("compact_chars must be an integer between 2048 and 24000")
    if not isinstance(include_retrieval_info, bool):
        raise ValueError("include_retrieval_info must be a boolean")
    if type(retain_for_evidence) is not bool:
        raise ValueError("retain_for_evidence must be a boolean")
    if type(include_jsonld) is not bool:
        raise ValueError("include_jsonld must be a boolean")
    before = request_cache.policy_epoch()
    once_proof = None
    # Ordinary permanent grants take precedence: a one-use grant must not be
    # burned for a request that was already authorized. Never use single-use
    # capabilities from a batch worker: its later aggregation requires
    # independently reusable permission for every result.
    if not _BATCH_PERMANENT_ONLY.get() and not CRAWLER_SECURITY_WARNING:
        try:
            hostname = urlsplit(url).hostname if isinstance(url, str) else None
            if (hostname and not is_domain_currently_allowed(hostname)
                    and domain_policy._once_grant_available(hostname)):
                once_proof = domain_policy._claim_once_for_public_url(url)
        except (TypeError, ValueError):
            once_proof = None
    if once_proof is None:
        policy_result = _check_public_target(url)
        if policy_result is not None:
            return policy_result
    else:
        # Staging the claimed grant may have invalidated the cache/policy epoch
        # after this request began but before the atomic claim. A successful
        # claim establishes a new authorization boundary: use its current
        # epoch, then still fail closed on later mutations and on any expired,
        # replaced, revoked or denied proof at final content release.
        before = request_cache.policy_epoch()
    if before != request_cache.policy_epoch():
        return {"url": url, "success": False, "status": "blocked",
                "reason": "Domain approval changed during the request. Retry after reviewing permissions."}
    if CRAWLER_SECURITY_WARNING:
        return {"url": url, "success": False, "status": "blocked",
                "reason": CRAWLER_SECURITY_WARNING}
    retrieval = None
    if once_proof is not None:
        # One-use access never reads, stores, refreshes or coalesces a reusable
        # fetch cache entry. Proof stays local to this invocation.
        if include_links or include_tables or include_outline or include_alternatives or retain_for_evidence or include_jsonld or compact_chars != document_pages.PAGE_CHARS:
            response = _run_fetch(
                url, mode, once_proof=once_proof,
                include_links=include_links, include_tables=include_tables,
                include_outline=include_outline, include_alternatives=include_alternatives,
                compact_chars=compact_chars, retain_for_evidence=retain_for_evidence,
                include_jsonld=include_jsonld)
        else:
            response = _run_fetch(url, mode, once_proof=once_proof)
        if include_retrieval_info:
            retrieval = request_cache._provenance(
                request_cache._retrieval_stamp(), "uncached")
    else:
        # Opt-in structured data must not reuse the legacy Markdown-only
        # cache entry or populate it with mismatched response shape.
        opt_in = (include_links or include_tables or include_outline or include_alternatives or
                  retain_for_evidence or include_jsonld or compact_chars != document_pages.PAGE_CHARS)
        key = None if opt_in else request_cache.fetch_key(url, mode)
        load = (lambda: _run_fetch(
            url, mode, include_links=include_links, include_tables=include_tables,
            include_outline=include_outline, include_alternatives=include_alternatives,
            compact_chars=compact_chars, retain_for_evidence=retain_for_evidence,
            include_jsonld=include_jsonld)
        ) if opt_in else (lambda: _run_fetch(url, mode))
        cached = request_cache.run(
            "fetch", key, load,
            ttl=lambda: cache_settings.effective_ttl('fetch_seconds'), fresh=fresh,
            with_provenance=include_retrieval_info,
            should_store=lambda r: (
                isinstance(r, dict) and r.get("success") is True
                and r.get("truncated") is False
                and "document_id" not in r
                and request_cache.fetch_key(r.get("url", ""), mode) is not None
                # A cross-host redirect depends on the target host's approval.
                # Never serve it later from the original host's cache key after
                # the target's time-limited rule expires.
                and urlsplit(r["url"]).hostname == urlsplit(url).hostname
            ),
        )
        if include_retrieval_info:
            response, retrieval = cached
        else:
            response = cached
    if before != request_cache.policy_epoch():
        return {"url": url, "success": False, "status": "blocked",
                "reason": "Domain approval changed during the request. Retry after reviewing permissions."}
    if isinstance(response, dict) and response.get("success") is True:
        link_info = response.get("link_continuation")
        if link_info is not None:
            link_id = link_info.get("link_id") if isinstance(link_info, dict) else None
            if not document_pages.link_handle_exists(link_id):
                return {"url": url, "success": False, "status": "continuation_evicted",
                        "reason": "The temporary link continuation was evicted during this fetch. Retry the URL."}
        table_info = response.get("table_continuation")
        if table_info is not None:
            table_id = table_info.get("table_id") if isinstance(table_info, dict) else None
            if not document_pages.table_handle_exists(table_id):
                return {"url": url, "success": False, "status": "continuation_evicted",
                        "reason": "The temporary table continuation was evicted during this fetch. Retry the URL."}
    if once_proof is not None:
        # A consumed private grant authorizes this *one* fetch, not a policy
        # rule. Verify it again after the crawl and before releasing ANY page
        # text or continuation handle. Redirects must still have an independently
        # approved destination if they cross the original hostname.
        blocked = {"url": url, "success": False, "status": "blocked",
                   "reason": "Single-use authorization is no longer valid."}
        try:
            source = domain_policy.normalize_domain(urlsplit(url).hostname or "")
            target = domain_policy.normalize_domain(
                urlsplit(response.get("url", url)).hostname or "")
            if (source != once_proof[0] or
                    not domain_policy._once_proof_current(*once_proof) or
                    (source != target and not is_domain_currently_allowed(target))):
                return blocked
        except (ValueError, TypeError, AttributeError):
            return blocked
        if before != request_cache.policy_epoch():
            return blocked
        if include_retrieval_info and isinstance(response, dict) and response.get("success") is True:
            return {**response, "retrieval_info": retrieval}
        return response
    if (isinstance(response, dict) and
            any(k in response for k in ("markdown", "metadata", "document_id"))):
        # Failed crawls can still contain Markdown or metadata. Never release
        # that content after a timed grant expires.
        # Expiration is time-based, not an operator mutation, so it must not
        # bump the global policy epoch and block unrelated batch URLs. Recheck
        # the original and final host immediately before returning content.
        # The final host matters for redirects even when their results are
        # intentionally never cached across domains.
        try:
            first_host = urlsplit(url).hostname
            last_host = urlsplit(response.get("url", url)).hostname
            if (not first_host or not last_host or
                    expired_grant_without_active_fallback(first_host) or
                    (last_host != first_host and
                     expired_grant_without_active_fallback(last_host))):
                return {"url": url, "success": False, "status": "blocked",
                        "reason": "A domain permission expired during the fetch. "
                                  "Request local approval and retry."}
        except (ValueError, TypeError):
            return {"url": url, "success": False, "status": "blocked",
                    "reason": "Invalid final domain in crawler response."}
    if include_retrieval_info and isinstance(response, dict) and response.get("success") is True:
        return {**response, "retrieval_info": retrieval}
    return response


def _current_permissions_for_fetched_urls(source_url: str, final_url: str) -> bool:
    """Check effective domain decisions without resubmitting/recrawling URLs.

    Initial fetch authorization already checked DNS. Subsequent content
    release needs fresh policy decisions, not additional DNS or new pending
    queue requests that would inflate usage counts.
    """
    try:
        source = urlsplit(source_url)
        final = urlsplit(final_url)
        if (source.scheme not in ("http", "https") or
                final.scheme not in ("http", "https") or
                not source.hostname or not final.hostname):
            return False
        return (is_domain_currently_allowed(source.hostname) and
                (source.hostname == final.hostname or
                 is_domain_currently_allowed(final.hostname)))
    except (ValueError, TypeError):
        return False


def _batch_currently_authorized(requested_url: str, result: dict) -> bool:
    """Revalidate effective source and redirect policies after all workers."""
    return _current_permissions_for_fetched_urls(requested_url, result.get("url"))


def _batch_fetch_without_once(url: str, mode: str, fresh: bool,
                              include_links: bool = False, include_tables: bool = False,
                              include_outline: bool = False,
                              compact_chars: int = document_pages.PAGE_CHARS,
                              include_retrieval_info: bool = False,
                              include_alternatives: bool = False) -> dict:
    """Preserve web_fetch logging, but prohibit one-use grants in batch workers."""
    token = _BATCH_PERMANENT_ONLY.set(True)
    try:
        if (include_links or include_tables or include_outline or include_alternatives or
                compact_chars != document_pages.PAGE_CHARS or include_retrieval_info):
            return web_fetch(url, mode, fresh,
                             include_links=include_links, include_tables=include_tables,
                             include_outline=include_outline, compact_chars=compact_chars,
                             include_retrieval_info=include_retrieval_info,
                             include_alternatives=include_alternatives)
        return web_fetch(url, mode, fresh)
    finally:
        _BATCH_PERMANENT_ONLY.reset(token)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
async def web_fetch_batch(urls: list[str], mode: str = "raw",
                          fresh: bool = False, include_links: bool = False,
                          include_tables: bool = False,
                          include_outline: bool = False,
                          compact_chars: int = document_pages.PAGE_CHARS,
                          include_retrieval_info: bool = False,
                          include_alternatives: bool = False) -> dict:
    """Fetch 1–8 approved URLs, at most 3 concurrently, with ordered results.

    Each unique URL uses the existing guarded web_fetch path. If the MCP call
    is cancelled, pending jobs are skipped; browser jobs already running
    cannot be forcibly stopped.
    """
    if not all(isinstance(flag, bool) for flag in
               (include_links, include_tables, include_outline, include_alternatives)):
        raise ValueError("include_links, include_tables, include_outline and include_alternatives must be booleans")
    if (type(compact_chars) is not int or
            not document_pages.MIN_FIRST_PAGE_CHARS <= compact_chars <= document_pages.PAGE_CHARS):
        raise ValueError("compact_chars must be an integer between 2048 and 24000")
    if not isinstance(include_retrieval_info, bool):
        raise ValueError("include_retrieval_info must be a boolean")
    cancelled = threading.Event()
    opt_in = (include_links or include_tables or include_outline or include_alternatives or
              compact_chars != document_pages.PAGE_CHARS)
    fetcher = (_batch_fetch_without_once
               if not opt_in and not include_retrieval_info
               else lambda url, mode, fresh: _batch_fetch_without_once(
                   url, mode, fresh,
                   include_links=include_links, include_tables=include_tables,
                   include_outline=include_outline, compact_chars=compact_chars,
                   include_retrieval_info=include_retrieval_info,
                   include_alternatives=include_alternatives))
    try:
        return await asyncio.to_thread(
            fetch_many, urls, mode, fresh, fetcher,
            cancelled=cancelled,
            authorize_result=_batch_currently_authorized)
    except asyncio.CancelledError:
        cancelled.set()
        raise


def _authorized_saved_document(
        result: dict, source_url: str | None,
        once_proof: tuple[str, str] | None, epoch: int) -> dict:
    """Single release gate for continuation pages AND in-document search.

    A temporary bearer handle alone is not reusable approval. Revalidate the
    source and final domains and any consumed one-use proof before disclosing
    page text or matching excerpts; never queue a new approval or re-crawl.
    """
    if not result.get("success"):
        return result
    blocked = {"success": False, "status": "blocked",
               "reason": "Document permission changed; fetch the approved URL again."}
    if not source_url:
        return blocked
    if once_proof is not None:
        try:
            source_host = domain_policy.normalize_domain(
                urlsplit(source_url).hostname or "")
            target_host = domain_policy.normalize_domain(
                urlsplit(result["url"]).hostname or "")
            valid = (source_host == once_proof[0]
                     and domain_policy._once_proof_current(*once_proof)
                     and bool(target_host)
                     and (target_host == source_host
                          or is_domain_currently_allowed(target_host)))
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            return blocked
    elif not _current_permissions_for_fetched_urls(source_url, result["url"]):
        return {"success": False, "status": "blocked",
                "reason": "The original or final domain is no longer approved. "
                          "Request local approval and retry this document."}
    if request_cache.policy_epoch() != epoch:
        return blocked
    return result


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@logged_tool("page")
def web_fetch_page(document_id: str, page: int = 2) -> dict:
    """Read a previously fetched continuation page; no crawler or file access.

    The document_id is an unguessable temporary bearer capability. It is
    never stored in local request history and expires after 15 minutes.
    """
    epoch = request_cache.policy_epoch()
    result, source_url, once_proof = document_pages.read_with_security_context(
        document_id, page)
    return _authorized_saved_document(result, source_url, once_proof, epoch)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@logged_tool("page")
def web_table_page(table_id: str, table_index: int, row_offset: int = 0,
                   max_rows: int = 10, column_offset: int = 0,
                   max_columns: int = 8) -> dict:
    """Continue already-extracted tables; never recrawl, reapprove or follow links.

    Indices refer to extracted source table order; offsets count retained rows,
    not original HTML rows. Completeness of the publisher source is unknown.
    """
    epoch = request_cache.policy_epoch()
    result, source_url, once_proof = document_pages.read_table_with_security_context(
        table_id, table_index, row_offset, max_rows, column_offset, max_columns)
    return _authorized_saved_document(result, source_url, once_proof, epoch)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@logged_tool("page")
def web_links_page(link_id: str, offset: int = 0, max_links: int = 10) -> dict:
    """Read retained links from an already-approved crawl without recrawling.

    Temporary bearer handle expires after 15 minutes. Every page rechecks
    current source/final permissions and any private one-use grant proof.
    Discovered links are untrusted and are NOT approvals to fetch them.
    """
    epoch = request_cache.policy_epoch()
    result, source_url, once_proof = document_pages.read_links_with_security_context(
        link_id, offset, max_links)
    return _authorized_saved_document(result, source_url, once_proof, epoch)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@logged_tool("page")
def web_find(document_id: str, query: str, max_matches: int = 10,
             start_offset: int = 0) -> dict:
    """Find bounded, literal case-insensitive excerpts in a fetched document.

    When has_more is true, pass next_offset as start_offset to continue without
    repeating overlapping matches. Offsets count Python Unicode characters in
    the fetched Markdown (zero-based), not bytes or source HTML positions.
    Only unexpired web_fetch handles are accepted; no network, filesystem or
    permanent index. Source/final grants and one-use proofs are rechecked on
    every call just as for web_fetch_page. Never audit bearer handles or text.
    """
    epoch = request_cache.policy_epoch()
    result, source_url, once_proof = document_pages.find_with_security_context(
        document_id, query, max_matches, start_offset)
    return _authorized_saved_document(result, source_url, once_proof, epoch)


def _citation_url(value: str) -> tuple[str, bool]:
    """Remove URL userinfo, query and fragment but keep an explicit port.

    Unlike private history's URL sanitizer, a source citation must not
    silently switch an approved unusual HTTP(S) port to the scheme default.
    Query values are never included in the returned evidence metadata.
    """
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return "", False
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"  # Defensive; public-IP targets are denied upstream.
        if parsed.port is not None:
            host += f":{parsed.port}"
        # Never emit a truncated citation that points to another resource.
        # An unusually long approved path makes evidence unavailable rather
        # than silently creating an incorrect URL.
        clean = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        return (clean if len(clean) <= 8192 else ""), bool(parsed.query)
    except (TypeError, ValueError, UnicodeError):
        return "", False


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@logged_tool("page")
def web_extract_section(document_id: str, start_offset: int,
                        max_chars: int = document_pages.DEFAULT_SECTION_CHARS,
                        include_evidence: bool = False) -> dict:
    """Read bounded Markdown and optionally assemble precise citation evidence.

    Evidence is an unverified quote from the existing fetched Markdown, not
    proof of publisher accuracy or current webpage contents. The source and
    final hosts are reauthorized BEFORE evidence metadata is released.
    """
    if type(include_evidence) is not bool:
        raise ValueError("include_evidence must be a boolean")
    epoch = request_cache.policy_epoch()
    result, source_url, once_proof = document_pages.section_with_security_context(
        document_id, start_offset, max_chars)
    authorized = _authorized_saved_document(result, source_url, once_proof, epoch)
    if not include_evidence or authorized.get("success") is not True:
        return authorized
    # At most 1200 code points of text are duplicated in evidence; the
    # ordinary section result and its default 3072-character cap are intact.
    quote = authorized["text"][:1200]
    final_url = authorized["url"]
    source = source_url or final_url
    source_display, source_had_query = _citation_url(source)
    final_display, final_had_query = _citation_url(final_url)
    if not source_display or not final_display:
        return {**authorized, "evidence": {
            "available": False,
            "reason": "A source or final URL could not be represented without "
                      "altering the source identity within the 8192-character cap.",
        }}
    return {**authorized, "evidence": {
        "quotation": quote,
        "quote_start_offset": authorized["start_offset"],
        "quote_end_offset": authorized["start_offset"] + len(quote),
        "quote_truncated": len(quote) < len(authorized["text"]),
        "page": authorized["page"],
        "offset_units": "unicode_code_points_in_normalized_markdown",
        "source_url": source_display,
        "final_url": final_display,
        "query_parameters_removed": source_had_query or final_had_query,
        "provenance": "already_authorized_crawl_markdown",
        "publisher_claims_verified": False,
        "source_completeness": "unknown",
        "retrieval_timestamp": "not_recorded_in_temporary_document",
    }}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@logged_tool("page")
def web_compare_documents(left_document_id: str, right_document_id: str,
                          max_changes: int = 12, context_chars: int = 120) -> dict:
    """Compare exactly two already-authorized temporary Markdown snapshots.

    No URL crawling, new approval, durable revision archive or background
    watching. Both source/final permissions and one-use proofs are checked
    independently against one policy epoch, before AND after computation.
    No partial text or source URLs are released if either side is invalid.
    """
    # Validate limits before touching any private document capabilities.
    if type(max_changes) is not int or not 1 <= max_changes <= 12:
        raise ValueError("max_changes must be from 1 to 12")
    if type(context_chars) is not int or not 1 <= context_chars <= 120:
        raise ValueError("context_chars must be from 1 to 120")
    epoch = request_cache.policy_epoch()
    left, right = document_pages.snapshot_pair_with_security_context(
        left_document_id, right_document_id)
    if left is None or right is None:
        return {"success": False, "status": "expired_or_unknown",
                "reason": "A temporary document is unavailable; fetch both approved URLs again."}
    denied = {"success": False, "status": "blocked",
              "reason": "One or both temporary documents are no longer authorized."}

    def both_authorized() -> bool:
        for url, _content, source, proof in (left, right):
            allowed = _authorized_saved_document(
                {"success": True, "url": url}, source, proof, epoch)
            if allowed.get("success") is not True:
                return False
        return request_cache.policy_epoch() == epoch

    if not both_authorized():
        return denied
    try:
        outcome = document_comparison.compare(
            left[1], right[1], max_changes=max_changes,
            context_chars=context_chars)
    except ValueError:
        return {"success": False, "status": "comparison_limit",
                "reason": "The captured text exceeds the bounded comparison limits."}
    if (not both_authorized()
            or not document_pages.pair_handles_current(
                left_document_id, right_document_id)
            or request_cache.policy_epoch() != epoch):
        return denied

    # Only reference complete, query-stripped URL identities. Never silently
    # truncate citation paths: a missing overlong label is marked unavailable.
    refs = {}
    for side, item in (("left", left), ("right", right)):
        final, _content, source, _proof = item
        src, _ = _citation_url(source or final)
        dest, _ = _citation_url(final)
        refs[side + "_source_url"] = src if len(src) <= 512 else ""
        refs[side + "_final_url"] = dest if len(dest) <= 512 else ""
        refs[side + "_source_identity_available"] = bool(
            refs[side + "_source_url"] and refs[side + "_final_url"])
    outcome.update(refs)
    # The 6 KiB budget includes the entire escaped JSON object. A
    # Unicode-heavy URL can expand sixfold in JSON; omit citation identities
    # FIRST to retain useful diff excerpts, never truncate a valid URL path.
    def serialized_size() -> int:
        return len(json.dumps(outcome, ensure_ascii=True).encode("utf-8"))

    if serialized_size() > document_comparison.MAX_OUTPUT_BYTES:
        for side in ("left", "right"):
            outcome[side + "_source_url"] = ""
            outcome[side + "_final_url"] = ""
            outcome[side + "_source_identity_available"] = False
    while (serialized_size() > document_comparison.MAX_OUTPUT_BYTES
           and outcome["changes"]):
        outcome["changes"].pop()
        outcome["changes_truncated"] = True
    if not both_authorized() or not document_pages.pair_handles_current(
            left_document_id, right_document_id):
        return denied
    return outcome


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def web_metrics(hours: Literal[1, 24, 168] = 24) -> dict:
    """Read private-text-free operation and cache statistics (last 1/24/168h).

    Operation outcomes come from the local, retention-controlled history index.
    Cache hit/miss/eviction counts are process-lifetime and reset on restart.
    This tool cannot modify permissions or expose queries, URLs, or page text.
    """
    import sqlite3
    import history_index

    if type(hours) is not int or hours not in (1, 24, 168):
        raise ValueError("hours must be 1, 24, or 168")
    cache = request_cache.metrics_snapshot()
    try:
        history = history_index.operational_metrics(hours)
    except (OSError, sqlite3.Error, ValueError):
        return {"success": False, "status": "history_unavailable",
                "reason": "Local aggregated history could not be read.",
                "cache": cache}
    return {"success": True, "history": history, "cache": cache}


def serve_mcp() -> None:
    """Launch with explicit Host/Origin validation, including via local tunnel."""
    # Arm already-running verified backends on bridge startup even before the
    # first tool call. This starts an idle timer, never a Docker container.
    try:
        on_demand._manager()
    except on_demand.BackendUnavailable:
        # Keep the transport reachable; later tool calls return safe errors.
        pass
    mcp.run(
        transport="http",
        host=MCP_BIND_HOST,
        port=MCP_PORT,
        stateless_http=True,
        host_origin_protection=True,
        allowed_hosts=MCP_ALLOWED_HOSTS,
        allowed_origins=MCP_ALLOWED_ORIGINS,
    )


if __name__ == "__main__":
    # Bind synchronously: dashboard failure must not leave a functioning MCP
    # server with no local approval interface.
    dashboard = create_dashboard_server()
    thread = threading.Thread(target=dashboard.serve_forever, name="local-search-dashboard", daemon=True)
    thread.start()
    print("Local dashboard: http://127.0.0.1:8791/", flush=True)
    try:
        serve_mcp()
    finally:
        dashboard.shutdown()
        dashboard.server_close()
