"""Local, read-only Crawl4AI v0.9.5 deep-crawl config gate preflight (#299).

Only GET the existing loopback /health endpoint by default. An explicit
--probe-config sends ONE fixed, authenticated /config/dump configuration
serialization request to the same already-running local server. It never
sends a URL, calls /crawl, enables deep crawling or changes approvals.
Accepting a config does NOT authorize navigation of discovered URLs.
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
MAX_RESPONSE_BYTES = 64 * 1024
_STRATEGY = "BFSDeepCrawlStrategy"


def _fixed_payload() -> dict:
    # There is intentionally no source URL, mutable operator parameter,
    # external-domain option, JS, custom scorer, or arbitrary filter.
    return {
        "type": "CrawlerRunConfig",
        "params": {
            "deep_crawl_strategy": {
                "type": _STRATEGY,
                "params": {
                    "max_depth": 1,
                    "max_pages": 2,
                    "include_external": False,
                },
            },
        },
    }


def _fixed_echo_matches(value: object) -> bool:
    """Only an exact fixed strategy echo counts as config compatibility.

    Additional serialized defaults are allowed, but a changed budget or
    external-link policy invalidates this observation. Neither result
    demonstrates that /crawl can safely follow approved links.
    """
    if type(value) is not dict or value.get("type") != "CrawlerRunConfig":
        return False
    params = value.get("params")
    if type(params) is not dict:
        return False
    strategy = params.get("deep_crawl_strategy")
    if type(strategy) is not dict or strategy.get("type") != _STRATEGY:
        return False
    details = strategy.get("params")
    if type(details) is not dict:
        return False
    return (type(details.get("max_depth")) is int
            and details["max_depth"] == 1
            and type(details.get("max_pages")) is int
            and details["max_pages"] == 2
            and details.get("include_external") is False)


def _probe_config(token: str) -> str:
    """Probe the loopback config serializer; never follow redirects."""
    if (type(token) is not str or not 1 <= len(token) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        return "operator_token_required"
    body = json.dumps(_fixed_payload(), separators=(",", ":")).encode("utf-8")
    connection = HTTPConnection(HOST, PORT, timeout=4)
    try:
        connection.request("POST", "/config/dump", body=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "local-search-mcp-deep-crawl-preflight/1",
        })
        response = connection.getresponse()
        if response.status == 400:
            # Could be the secure default-off gate OR a schema rejection.
            # Never infer the exact server flag state from status alone.
            return "rejected_400_reason_unknown"
        if response.status != 200:
            return "unavailable"
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return "unavailable"
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            return "unavailable"
        return ("fixed_config_echo_verified" if _fixed_echo_matches(result)
                else "unexpected_config_echo")
    except (OSError, HTTPException, ValueError, TypeError):
        return "unavailable"
    finally:
        connection.close()


def report(*, probe_config: bool = False) -> dict:
    result = {
        "mode": "read_only_deep_crawl_compatibility",
        "health": "unavailable",
        "installed_version": None,
        "config_status": "not_probed",
        "discovered_url_authorization": "not_verified",
        "deep_crawl_ready": False,
        "public_crawls": 0,
        "external_requests": 0,
        "services_started": 0,
        "approvals_modified": False,
        "settings_modified": False,
    }
    try:
        health = get_json(PORT, "/health")
        if verify_health(health) is not None:
            result["health"] = "unsupported_or_unverified"
            return result
        result["health"] = "supported_and_healthy"
        result["installed_version"] = health["version"]
    except (OSError, HTTPException, ValueError, TypeError, KeyError):
        return result

    if probe_config:
        result["config_status"] = _probe_config(
            os.environ.get("CRAWL4AI_API_TOKEN", ""))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-config", action="store_true",
        help="Send one fixed local authenticated /config/dump request; never crawl")
    args = parser.parse_args(argv)
    outcome = report(probe_config=args.probe_config)
    print(json.dumps(outcome, sort_keys=True, indent=2))
    accepted_outcomes = {"fixed_config_echo_verified",
                         "rejected_400_reason_unknown"}
    if outcome["health"] != "supported_and_healthy":
        return 1
    if args.probe_config and outcome["config_status"] not in accepted_outcomes:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
