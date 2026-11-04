"""Safe negative-path smoke checks for the two OPTIONAL loopback MCP ingress hops.

Uses only fixed 127.0.0.1 destinations and requests without Authorization.
Does not read tokens, change services, wake Docker, or reach Cloudflare.
A passing result is NOT proof of positive-path auth or remote identity.
"""
from __future__ import annotations

import argparse
import json

import httpx

from remote_agent_cloudflare_origin import (
    StartupRefused, default_config_path, read_private_hostname,
)

SCHEMA = "local-search-mcp/ingress-negative-probe/v1"
HOSTS = {"gateway": "127.0.0.1:18790", "origin": None}
PORTS = {"gateway": 18790, "origin": 18791}
TIMEOUT = httpx.Timeout(connect=0.7, read=0.7, write=0.7, pool=0.7)
# These are safe GET-only denials: not a tool invocation or configuration write.
CASES = (
    ("missing_bearer", "/mcp", None, None, 401),
    ("foreign_host", "/mcp", "forbidden.invalid", None, 403),
    ("foreign_origin", "/mcp", None, "https://forbidden.invalid", 403),
    ("unexpected_query", "/mcp?probe=1", None, None, 404),
)


def local_negative_probe(*, transport: httpx.BaseTransport | None = None) -> dict:
    """Inspect refusal behavior of already-running, fixed-loopback listeners.

    Never emit the configured private hostname, local filesystem paths, raw
    response body, headers, URLs, credentials or transport exception text.
    A missing origin config is *not* a passed origin check.
    """
    report = {
        "schema": SCHEMA,
        "mode": "read-only",
        "status": "incomplete",
        "negative_path_only": True,
        "remote_access_verified": False,
        "positive_auth_verified": False,
        "requests_sent": 0,
        "origin_configured": False,
        "checks": [],
    }
    try:
        hostname = read_private_hostname(default_config_path())
        report["origin_configured"] = True
    except (StartupRefused, OSError, ValueError):
        hostname = None

    with httpx.Client(
        trust_env=False, follow_redirects=False,
        timeout=TIMEOUT, transport=transport,
    ) as client:
        for target in ("gateway", "origin"):
            if target == "origin" and hostname is None:
                report["checks"].append({
                    "target": "origin", "check": "local_configuration",
                    "status": "not_configured",
                })
                continue
            host = HOSTS["gateway"] if target == "gateway" else hostname
            for name, path, alternate_host, origin, expected in CASES:
                headers = {"Host": alternate_host or host}
                if origin is not None:
                    headers["Origin"] = origin
                report["requests_sent"] += 1
                try:
                    # Do not read response bodies: a wrong/malicious listener
                    # cannot force us to ingest unbounded content or secrets.
                    with client.stream(
                        "GET", f"http://127.0.0.1:{PORTS[target]}{path}",
                        headers=headers,
                    ) as response:
                        status = ("passed" if response.status_code == expected
                                  else "unexpected_status")
                except httpx.HTTPError:
                    status = "unavailable"
                report["checks"].append({
                    "target": target, "check": name, "status": status,
                })

    outcomes = {entry["status"] for entry in report["checks"]}
    if "unexpected_status" in outcomes:
        report["status"] = "failed"
    elif outcomes == {"passed"}:
        report["status"] = "passed"
    else:
        report["status"] = "incomplete"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.parse_args(argv)
    report = local_negative_probe()
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
