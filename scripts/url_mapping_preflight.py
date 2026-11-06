"""Read-only local Crawl4AI URL-mapping capability preflight (#298).

Inspects a fixed loopback health endpoint, and, only with --inspect-routes,
the authenticated OpenAPI route inventory. Does not fetch public URLs, launch
services, modify settings, request domain permissions or perform a crawl.
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
import json
import os
import sys

from crawler_security import verify_health
from health_cli import get_json

HOST = "127.0.0.1"
PORT = 11235
MAX_SCHEMA_BYTES = 1024 * 1024
MAX_ROUTES = 256
METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
# Discovery-like names only identify *candidates*. The server-side auth,
# runtime contract and per-target security must be reviewed separately.
DISCOVERY_PATH_SEGMENTS = frozenset({
    "map", "mapping", "sitemap", "sitemaps", "seed", "seeding", "scan",
    "url-map", "url-mapping", "url-seed", "url-seeding",
})


class SchemaUnavailable(ValueError):
    """A bounded authenticated local route inventory was not verified."""


def _route_candidates(data: object) -> dict:
    """Inspect only advertised method/path names, never trust operation details."""
    if not isinstance(data, dict) or not isinstance(data.get("paths"), dict):
        raise SchemaUnavailable("Invalid OpenAPI response")
    paths = data["paths"]
    if len(paths) > MAX_ROUTES:
        raise SchemaUnavailable("Too many OpenAPI paths")
    matches: list[dict] = []
    for path, operations in paths.items():
        if (not isinstance(path, str) or not path.startswith("/")
                or path.startswith("//") or len(path) > 160
                or any(ord(ch) < 32 or ord(ch) > 126 for ch in path)
                or not isinstance(operations, dict)):
            raise SchemaUnavailable("Invalid OpenAPI route")
        names = [segment.casefold() for segment in path.split("/") if segment]
        if not any(segment in DISCOVERY_PATH_SEGMENTS for segment in names):
            continue
        methods = sorted(key.upper() for key in operations if key in METHODS)
        if methods:
            matches.append({"path": path, "methods": methods})
    return {
        "route_status": "candidates_require_review" if matches else "no_advertised_mapping_route",
        "mapping_route_candidates": matches,
        "advertised_path_count": len(paths),
    }


def _read_openapi(token: str) -> dict:
    """Fixed loopback-only GET with no inherited proxies or redirect handling.

    Never print the token, the response body, response headers or exceptions.
    """
    if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise SchemaUnavailable("Missing or invalid operator credential")
    conn = HTTPConnection(HOST, PORT, timeout=3)
    try:
        conn.request("GET", "/openapi.json", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "local-search-mcp-url-mapping-preflight/1",
        })
        response = conn.getresponse()
        if response.status != 200:
            raise SchemaUnavailable("OpenAPI response unavailable")
        content = response.read(MAX_SCHEMA_BYTES + 1)
        if len(content) > MAX_SCHEMA_BYTES:
            raise SchemaUnavailable("OpenAPI response too large")
        try:
            document = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise SchemaUnavailable("Invalid OpenAPI JSON") from exc
        return _route_candidates(document)
    finally:
        conn.close()


def report(*, inspect_routes: bool = False) -> dict:
    result = {
        "mode": "read-only", "backend": "crawl4ai", "scope": "fixed_loopback",
        "external_requests": 0, "crawl_requests": 0, "services_started": 0,
        "configuration_modified": False, "approvals_modified": False,
        "health": "unavailable", "route_status": "not_inspected",
        "installed_version": None,
    }
    try:
        health = get_json(PORT, "/health")
        if verify_health(health) is not None:
            result["health"] = "unverified_or_unsupported"
            return result
        result["health"] = "supported_and_healthy"
        result["installed_version"] = health["version"]
    except (OSError, ValueError, TypeError, KeyError, HTTPException):
        return result
    if not inspect_routes:
        return result
    token = os.environ.get("CRAWL4AI_API_TOKEN", "")
    if not token:
        result["route_status"] = "operator_token_required"
        return result
    try:
        result.update(_read_openapi(token))
    except (OSError, ValueError, TypeError, HTTPException):
        result["route_status"] = "schema_unavailable_or_invalid"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-routes", action="store_true",
                        help="read authenticated local /openapi.json using the existing environment token")
    args = parser.parse_args(argv)
    output = report(inspect_routes=args.inspect_routes)
    print(json.dumps(output, indent=2, sort_keys=True))
    if output["health"] != "supported_and_healthy":
        return 1
    if args.inspect_routes and output["route_status"] in (
            "operator_token_required", "schema_unavailable_or_invalid"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
