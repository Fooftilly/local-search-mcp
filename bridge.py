"""A local, read-only MCP adapter for SearXNG and self-hosted Crawl4AI."""
from __future__ import annotations

import os

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


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
@logged_tool("search")
def web_search(query: str, max_results: int = 8) -> list[dict]:
    """Search the public web through the user's local SearXNG instance."""
    query = query.strip()
    if not 1 <= len(query) <= 300:
        raise ValueError("Query must contain 1 to 300 characters")
    max_results = max(1, min(max_results, 12))
    with httpx.Client(timeout=25, trust_env=False) as client:
        response = client.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"})
        response.raise_for_status()
        results = response.json().get("results", [])
    return [
        {
            "title": str(r.get("title", ""))[:240],
            "url": str(r.get("url", "")),
            "snippet": str(r.get("content", ""))[:1100],
            "engine": str(r.get("engine", "")),
        }
        for r in results[:max_results]
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
