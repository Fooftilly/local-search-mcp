#!/usr/bin/env python3
"""Read-only local health checks; fixed loopback ports, no proxies or redirects."""
from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
import json
import socket
import sys

from crawler_security import verify_health

HOST = "127.0.0.1"
TIMEOUT = 3


def local_get(port: int, path: str, max_bytes: int = 200_000) -> tuple[int, bytes]:
    """Query a fixed local HTTP server directly without proxy or redirect handling."""
    if port not in (8888, 11235, 8791) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Unknown health destination")
    connection = HTTPConnection(HOST, port, timeout=TIMEOUT)
    try:
        connection.request("GET", path, headers={"User-Agent": "local-search-mcp-health/1"})
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("Health response exceeded limit")
        return response.status, body
    finally:
        connection.close()


def get_json(port: int, path: str) -> object:
    """Parse a bounded JSON response from an approved loopback endpoint."""
    status, body = local_get(port, path)
    if status != 200:
        raise ValueError("Unexpected health HTTP status")
    return json.loads(body)


def listening(port: int) -> bool:
    """Test a fixed local TCP listener without executing tools or reading secrets."""
    try:
        with socket.create_connection((HOST, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def dashboard_ok() -> bool:
    """Check the dashboard's small health endpoint, not its full HTML page."""
    try:
        status, body = local_get(8791, "/healthz", max_bytes=1024)
        return status == 200 and json.loads(body) == {"status": "ok"}
    except (OSError, ValueError, HTTPException):
        return False


def searxng_ok() -> bool:
    """Use the SearXNG health endpoint instead of issuing recurring web searches."""
    try:
        status, body = local_get(8888, "/healthz", max_bytes=50)
        return status == 200 and body.strip() == b"OK"
    except (OSError, ValueError, HTTPException):
        return False


def snapshot() -> dict:
    """Distinguish an unavailable bridge from degraded upstream dependencies."""
    states = {"searxng": searxng_ok()}
    try:
        states["crawl4ai"] = verify_health(get_json(11235, "/health")) is None
    except (OSError, ValueError, TypeError, HTTPException):
        states["crawl4ai"] = False
    states["mcp_listener"] = listening(8790)
    states["dashboard_http"] = dashboard_ok()
    states["tunnel_listener"] = listening(8080)
    expected_sleeping = []
    # An opted-in stopped backend is expected, not evidence that the MCP
    # bridge/tunnel needs restarting. Never wake anything from health checks.
    try:
        from scripts import on_demand_settings, on_demand_docker
        if on_demand_settings.read()["enabled"]:
            for backend in ("searxng", "crawl4ai"):
                if not states[backend]:
                    target = on_demand_docker._safe_target(backend)
                    if not target.running:
                        expected_sleeping.append(backend)
    except (OSError, ValueError, RuntimeError, ImportError):
        expected_sleeping = []
    required = [value for name, value in states.items()
                if name not in expected_sleeping]
    status = ("healthy" if all(states.values()) else
              "sleeping" if expected_sleeping and all(required) else
              "degraded" if states["mcp_listener"] else "unavailable")
    return {
        "status": status,
        "expected_sleeping_backends": expected_sleeping,
        "checks": states,
        "note": "TCP listener checks do not prove remote tunnel registration or MCP-tool correctness.",
    }


def main(argv: list[str] | None = None) -> int:
    """Print health as text or JSON and exit nonzero when a dependency is down."""
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
    return 0 if result["status"] in ("healthy", "sleeping") else 1


if __name__ == "__main__":
    sys.exit(main())
