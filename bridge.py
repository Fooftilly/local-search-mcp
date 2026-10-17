"""A local, read-only MCP adapter for SearXNG and self-hosted Crawl4AI."""
from __future__ import annotations

import os
import re
import time

import httpx
from fastmcp import FastMCP
from domain_policy import evaluate_fetch_url
from admin_ui import create_dashboard_server
from crawler_security import local_container_warning, verify_health
import threading
from urllib.parse import urlsplit, urlunsplit
from history_log import logged_tool
import document_pages
import request_cache

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
CRAWL4AI_URL = os.getenv("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")
CRAWL4AI_TOKEN = os.getenv("CRAWL4AI_API_TOKEN", "")
mcp = FastMCP("Local SearXNG and Crawl4AI")
# This unauthenticated endpoint is deliberately loopback-only. Validate both
# request authority and browser Origin: binding localhost alone cannot prevent
# a DNS-rebinding attack from an untrusted browser tab.
MCP_BIND_HOST = "127.0.0.1"
MCP_PORT = 8790
MCP_ALLOWED_HOSTS = ["127.0.0.1:8790", "localhost:8790"]
MCP_ALLOWED_ORIGINS = ["http://127.0.0.1:8790", "http://localhost:8790"]
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


def _run_search(params: dict, max_results: int) -> list[dict] | dict:
    with httpx.Client(timeout=25, trust_env=False) as client:
        response = _retry_request(lambda: client.get(
            f"{SEARXNG_URL}/search", params=params))
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
    if not payload.get("results") and payload.get("unresponsive_engines"):
        return {"success": False, "status": "upstream_unavailable", "backend": "searxng",
                "reason": "SearXNG returned no results because one or more engines were unresponsive. Retry later or choose another engine."}
    return [
        {
            "title": str(r.get("title", ""))[:240],
            "url": str(r.get("url", "")),
            "snippet": str(r.get("content", ""))[:1100],
            "engine": str(r.get("engine", "")),
        }
        for r in [row for row in payload.get("results", []) if isinstance(row, dict)][:max_results]
    ]


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
@logged_tool("search")
def web_search(query: str, max_results: int = 8, language: str = "",
               categories: str = "", time_range: str = "",
               engines: str = "", page: int = 1, fresh: bool = False) -> list[dict] | dict:
    """Search SearXNG with bounded, memory-only caching; fresh bypasses cache."""
    if not isinstance(fresh, bool):
        raise ValueError("fresh must be a boolean")
    params, max_results = _search_params(
        query, max_results, language, categories, time_range, engines, page,
    )
    # Avoid caching obviously sensitive-looking queries. No queries are saved
    # to disk by this cache; the separate opt-in history remains unchanged.
    private = bool(re.search(
        r"@|https?://|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S+|(?:password|token|secret|api[_-]?key|bearer|cookie)\s*[:=]",
        params["q"], flags=re.IGNORECASE))
    key = None if private else (
        "search", SEARXNG_URL, tuple(sorted(params.items())), max_results,
    )
    return request_cache.run(
        "search", key, lambda: _run_search(params, max_results),
        ttl=request_cache.SEARCH_TTL, fresh=fresh, should_store=lambda r: isinstance(r, list),
    )

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


def _run_fetch(url: str, mode: str) -> dict:
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
    with httpx.Client(timeout=75, trust_env=False) as client:
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
    if final_url != url and _check_public_target(final_url) is not None:
        return {"url": url, "success": False, "status": "blocked", "reason": "Crawl4AI redirected to a destination that is not approved"}
    markdown_response = page.get("markdown", "")
    if isinstance(markdown_response, dict):
        raw = markdown_response.get("raw_markdown")
        fit = markdown_response.get("fit_markdown")
    else:
        raw, fit = markdown_response, page.get("fit_markdown")
    if mode == "fit":
        markdown = fit
    else:
        markdown = raw or fit or ""
    if mode == "fit" and (markdown is None or markdown == ""):
        return {"url": final_url, "success": False, "status": "unavailable_extraction",
                "reason": "Crawl4AI did not provide fit Markdown for this page"}
    if not isinstance(markdown, str):
        return {"url": final_url, "success": False, "status": "invalid_extraction",
                "reason": "Crawl4AI returned an invalid Markdown representation"}
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
        "markdown": markdown[:document_pages.PAGE_CHARS],
        "truncated": len(markdown) > document_pages.PAGE_CHARS,
    }
    if metadata:
        output["metadata"] = metadata
    if len(markdown) > document_pages.PAGE_CHARS and success is not False:
        token, page_count = document_pages.save(final_url, markdown, metadata)
        output.update({"document_id": token, "page": 1,
                       "page_count": page_count, "next_page": 2})
    elif len(markdown) > document_pages.PAGE_CHARS:
        return {"url": final_url, "success": False, "status": "crawl_failed",
                "reason": "Crawl4AI reported failure; no incomplete document was cached"}
    return output


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
@logged_tool("fetch")
def web_fetch(url: str, mode: str = "raw", fresh: bool = False) -> dict:
    """Fetch an approved page; bounded memory cache for public short pages.

    Authorization runs before every cache hit. Fresh skips both cache and
    concurrent request coalescing. Long paged responses are never cached.
    """
    if mode not in ("raw", "fit"):
        raise ValueError("mode must be raw or fit")
    if not isinstance(fresh, bool):
        raise ValueError("fresh must be a boolean")
    before = request_cache.policy_epoch()
    policy_result = _check_public_target(url)
    if policy_result is not None:
        return policy_result
    if before != request_cache.policy_epoch():
        return {"url": url, "success": False, "status": "blocked",
                "reason": "Domain approval changed during the request. Retry after reviewing permissions."}
    if CRAWLER_SECURITY_WARNING:
        return {"url": url, "success": False, "status": "blocked",
                "reason": CRAWLER_SECURITY_WARNING}
    key = request_cache.fetch_key(url, mode)
    response = request_cache.run(
        "fetch", key, lambda: _run_fetch(url, mode),
        ttl=request_cache.FETCH_TTL, fresh=fresh,
        should_store=lambda r: (
            isinstance(r, dict) and r.get("success") is True
            and r.get("truncated") is False
            and "document_id" not in r
            and request_cache.fetch_key(r.get("url", ""), mode) is not None
        ),
    )
    if before != request_cache.policy_epoch():
        return {"url": url, "success": False, "status": "blocked",
                "reason": "Domain approval changed during the request. Retry after reviewing permissions."}
    return response


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@logged_tool("page")
def web_fetch_page(document_id: str, page: int = 2) -> dict:
    """Read a previously fetched continuation page; no crawler or file access.

    The document_id is an unguessable temporary bearer capability. It is
    never stored in local request history and expires after 15 minutes.
    """
    return document_pages.read(document_id, page)


def serve_mcp() -> None:
    """Launch with explicit Host/Origin validation, including via local tunnel."""
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
