#!/usr/bin/env python3
"""Bounded, read-only local health snapshots. Never reads private credentials."""
from __future__ import annotations

import argparse
import json
import socket
import sys
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from crawler_security import verify_health

HOST = "127.0.0.1"
TIMEOUT = 3


class NoRedirect(HTTPRedirectHandler):
    """Reject unexpected redirects from loopback services."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        """Do not follow redirects, including to non-loopback addresses."""
        return None


def local_request(url: str):
    """Use only fixed loopback URLs without proxying or following redirects."""
    if not url.startswith(f"http://{HOST}:"):
        raise ValueError("Health request target is not loopback")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    return opener.open(Request(url, headers={"User-Agent": "local-search-mcp-health/1"}), timeout=TIMEOUT)


def searxng_ok() -> bool:
    """Use SearXNG /healthz, avoiding real upstream search requests and rate limits."""
    try:
        with local_request(f"http://{HOST}:8888/healthz") as response:  # NOSONAR (loopback-only HTTP API)
            return response.status == 200 and response.read(50).strip() == b"OK"
    except (OSError, ValueError):
        return False


def get_json(url: str) -> object:
    """Get JSON from a fixed loopback endpoint, with a strict response limit."""
    # These local upstreams expose HTTP over loopback only, without TLS.
    # Never honor proxy environment variables for local health traffic.
    with local_request(url) as response:
        if response.status != 200:
            raise ValueError("Unexpected health HTTP status")
        return json.loads(response.read(200_000))


def listening(port: int) -> bool:
    """Test a local TCP listener without executing tools or accessing secrets."""
    try:
        with socket.create_connection((HOST, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def dashboard_ok() -> bool:
    """Require the loopback management dashboard to return HTTP 200."""
    try:
        with local_request(f"http://{HOST}:8791/") as response:  # NOSONAR (loopback-only HTTP API)
            return response.status == 200
    except (OSError, ValueError):
        return False


def snapshot() -> dict:
    """Report each dependency independently, distinguishing degraded from offline."""
    states = {}
    states["searxng"] = searxng_ok()
    try:
        states["crawl4ai"] = verify_health(get_json(f"http://{HOST}:11235/health")) is None  # NOSONAR (loopback-only Crawl4AI)
    except (OSError, ValueError, TypeError):
        states["crawl4ai"] = False
    states["mcp_listener"] = listening(8790)
    states["dashboard_http"] = dashboard_ok()
    states["tunnel_listener"] = listening(8080)
    status = "healthy" if all(states.values()) else ("degraded" if states["mcp_listener"] else "unavailable")
    return {
        "status": status,
        "checks": states,
        "note": "TCP listeners do not prove remote tunnel connectivity or MCP-tool correctness.",
    }


def main(argv: list[str] | None = None) -> int:
    """Print a structured snapshot; failures are visible to systemd timers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = snapshot()
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Overall: {result['status']}")
        for name, good in result["checks"].items():
            print(f"{name}: {'healthy' if good else 'unavailable'}")
        print(result["note"])
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    sys.exit(main())
