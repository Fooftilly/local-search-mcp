#!/usr/bin/env python3
"""Bounded, read-only local health snapshots. Never reads private credentials."""
from __future__ import annotations

import argparse
import json
import socket
import sys
from urllib.request import ProxyHandler, Request, build_opener

from crawler_security import verify_health

HOST = "127.0.0.1"
TIMEOUT = 3


def get_json(url: str) -> object:
    """Get JSON from a fixed loopback endpoint, with a strict response limit."""
    # These local upstreams expose HTTP over loopback only, without TLS.
    # Never honor proxy environment variables for local health traffic.
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, headers={"User-Agent": "local-search-mcp-health/1"}), timeout=TIMEOUT) as response:
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
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(f"http://{HOST}:8791/", headers={"User-Agent": "local-search-mcp-health/1"}), timeout=TIMEOUT) as response:  # NOSONAR (loopback-only HTTP API)
            return response.status == 200
    except (OSError, ValueError):
        return False


def snapshot() -> dict:
    """Report each dependency independently, distinguishing degraded from offline."""
    states = {}
    try:
        response = get_json(f"http://{HOST}:8888/search?q=local-search-mcp-health&format=json")  # NOSONAR (loopback-only SearXNG)
        states["searxng"] = isinstance(response, dict) and isinstance(response.get("results"), list)
    except (OSError, ValueError, TypeError):
        states["searxng"] = False
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
