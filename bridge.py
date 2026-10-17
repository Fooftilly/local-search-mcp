"""A local, read-only MCP adapter for SearXNG and self-hosted Crawl4AI."""
from __future__ import annotations

import os
import re

import httpx
from fastmcp import FastMCP
from domain_policy import evaluate_fetch_url
from admin_ui import create_dashboard_server
from crawler_security import local_container_warning, verify_health
import threading
from history_log import logged_tool

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


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
@logged_tool("search")
def web_search(query: str, max_results: int = 8, language: str = "",
               categories: str = "", time_range: str = "",
               engines: str = "", page: int = 1) -> list[dict]:
    """Search locally via SearXNG, optionally filtering language, source and date.

    categories and engines are comma-separated names configured on the local
    instance. Filters may be ignored by upstream engines without that ability.
    """
    params, max_results = _search_params(
        query, max_results, language, categories, time_range, engines, page,
    )
    with httpx.Client(timeout=25, trust_env=False) as client:
        response = client.get(f"{SEARXNG_URL}/search", params=params)
        if response.status_code in (400, 422) and len(params) > 2:
            raise ValueError("Local SearXNG rejected the requested filters or page")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
        raise ValueError("Local SearXNG returned an invalid results structure")
    return [
        {
            "title": str(r.get("title", ""))[:240],
            "url": str(r.get("url", "")),
            "snippet": str(r.get("content", ""))[:1100],
            "engine": str(r.get("engine", "")),
        }
        for r in payload.get("results", [])[:max_results]
        if isinstance(r, dict)
    ]


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
@logged_tool("fetch")
def web_fetch(url: str) -> dict:
    """Fetch a permitted public web page with the user's local Crawl4AI server."""
    policy_result = _check_public_target(url)
    if policy_result is not None:
        return policy_result
    if CRAWLER_SECURITY_WARNING:
        return {"url": url, "success": False, "status": "blocked", "reason": CRAWLER_SECURITY_WARNING}
    headers = {"Authorization": f"Bearer {CRAWL4AI_TOKEN}"} if CRAWL4AI_TOKEN else {}
    payload = {
        "urls": [url],
        "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
        "crawler_config": {"type": "CrawlerRunConfig", "params": {"stream": False}},
    }
    with httpx.Client(timeout=75, trust_env=False) as client:
        try:
            health = client.get(f"{CRAWL4AI_URL}/health", timeout=8)
            health.raise_for_status()
            health_error = verify_health(health.json())
        except (httpx.HTTPError, ValueError) as exc:
            return {"url": url, "success": False, "status": "blocked", "reason": f"Crawl4AI security check unavailable: {type(exc).__name__}"}
        if health_error:
            return {"url": url, "success": False, "status": "blocked", "reason": health_error}
        response = client.post(f"{CRAWL4AI_URL}/crawl", json=payload, headers=headers)
        response.raise_for_status()
        result = response.json()
    results = result.get("results", []) if isinstance(result, dict) else result
    page = results[0] if isinstance(results, list) and results else result
    if not isinstance(page, dict):
        raise RuntimeError("Unrecognized Crawl4AI response")
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
    markdown = page.get("markdown", "")
    if isinstance(markdown, dict):
        markdown = markdown.get("raw_markdown") or markdown.get("fit_markdown") or ""
    return {
        "url": final_url,
        "success": page.get("success", result.get("success") if isinstance(result, dict) else None),
        "markdown": str(markdown)[:24000],
        "truncated": len(str(markdown)) > 24000,
    }


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
