"""Read-only installed-Crawl4AI structured extraction compatibility preflight (#300).

With --probe-configs, POST only two hardcoded non-LLM configs to the local,
authenticated v0.9.5+ /config/dump serialization endpoint. This is NOT a
public crawl, extraction run, approval request, network enablement or source
of proof that data extraction works. Neither server JSON nor tokens are printed.
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
MAX_RESPONSE_BYTES = 128 * 1024
STRATEGIES = ("JsonCssExtractionStrategy", "JsonXPathExtractionStrategy")


class ProbeUnavailable(ValueError):
    """The local serialization contract could not be verified."""


def _fixed_payload(strategy: str) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError("Unsupported probe strategy")
    css = strategy == "JsonCssExtractionStrategy"
    schema = {
        "name": "local-search-mcp-preflight",
        "baseSelector": "article" if css else "//article",
        "fields": [{
            "name": "headline",
            "selector": "h2" if css else ".//h2",
            "type": "text",
        }],
    }
    return {
        "type": "CrawlerRunConfig",
        "params": {
            "extraction_strategy": {
                "type": strategy,
                "params": {"schema": schema},
            },
        },
    }


def _echo_supported(data: object, strategy: str, expected: dict) -> bool:
    """Confirm nested *declarative config* preservation, not extraction output."""
    if not isinstance(data, dict) or data.get("type") != "CrawlerRunConfig":
        return False
    params = data.get("params")
    if not isinstance(params, dict):
        return False
    extracted = params.get("extraction_strategy")
    if not isinstance(extracted, dict) or extracted.get("type") != strategy:
        return False
    strategy_params = extracted.get("params")
    if not isinstance(strategy_params, dict):
        return False
    return strategy_params.get("schema") == expected["params"]["extraction_strategy"]["params"]["schema"]


def _probe(token: str, strategy: str) -> bool:
    """One fixed loopback request; no redirects or inherited proxy use."""
    if (not isinstance(token, str) or not token or len(token) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise ProbeUnavailable("Operator token missing or invalid")
    payload = _fixed_payload(strategy)
    request_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    conn = HTTPConnection(HOST, PORT, timeout=4)
    try:
        conn.request("POST", "/config/dump", body=request_bytes, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "local-search-mcp-structured-extraction-preflight/1",
        })
        response = conn.getresponse()
        if response.status != 200:
            raise ProbeUnavailable("Local config-dump endpoint unavailable")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ProbeUnavailable("Oversized config-dump response")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ProbeUnavailable("Invalid config-dump response") from exc
        return _echo_supported(parsed, strategy, payload)
    finally:
        conn.close()


def report(*, probe_configs: bool = False) -> dict:
    result = {
        "mode": "local_read_only_compatibility",
        "backend": "crawl4ai",
        "health": "unavailable",
        "installed_version": None,
        "config_status": "not_probed",
        "strategy_results": {},
        "public_crawls": 0,
        "external_requests": 0,
        "services_started": 0,
        "approvals_modified": False,
        "settings_modified": False,
        "schema_provenance": "fixed_local_probe_only",
    }
    try:
        health = get_json(PORT, "/health")
        if verify_health(health) is not None:
            result["health"] = "unsupported_or_unverified"
            return result
        result["health"] = "supported_and_healthy"
        result["installed_version"] = health["version"]
    except (OSError, ValueError, TypeError, KeyError, HTTPException):
        return result
    if not probe_configs:
        return result

    token = os.environ.get("CRAWL4AI_API_TOKEN", "")
    if not token:
        result["config_status"] = "operator_token_required"
        return result
    for strategy in STRATEGIES:
        key = "css" if strategy == "JsonCssExtractionStrategy" else "xpath"
        try:
            result["strategy_results"][key] = (
                "declarative_config_echo_verified" if _probe(token, strategy)
                else "config_echo_not_verified"
            )
        except (OSError, ValueError, TypeError, HTTPException):
            result["strategy_results"][key] = "config_probe_unavailable"
    result["config_status"] = (
        "both_config_echoes_verified"
        if all(x == "declarative_config_echo_verified"
               for x in result["strategy_results"].values())
        else "inconclusive"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-configs", action="store_true",
        help="opt in to two fixed authenticated local /config/dump POSTs (no crawl)")
    args = parser.parse_args(argv)
    outcome = report(probe_configs=args.probe_configs)
    print(json.dumps(outcome, sort_keys=True, indent=2))
    if outcome["health"] != "supported_and_healthy":
        return 1
    if args.probe_configs and outcome["config_status"] != "both_config_echoes_verified":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
