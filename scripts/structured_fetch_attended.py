"""Attended, operator-approved live pilot of PR #309 against existing guarded crawl.

Run with an isolated PR venv; do NOT deploy this branch or enable the operator
flag on the running MCP service. Refuse all unknown/unapproved hosts before any
request, never change rules, and never print tokens or fetched page contents.
Three independently specified HTTPS paths exercise CSS/XPath/CSS on heading
text; this is smoke/authorization-path evidence, not full layout-quality or
negative-host SSRF/cancellation acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import urlsplit


def _require_preapproved(urls: list[str]) -> None:
    # Import only at invocation, so --help and CLI validation cannot access
    # the operator approval database.
    from domain_policy import is_domain_currently_allowed
    if len(urls) != 3 or len(set(urls)) != 3:
        raise ValueError("Supply exactly three different approved HTTPS URLs")
    hosts: set[str] = set()
    for url in urls:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or
                parsed.username or parsed.password or parsed.port is not None or
                parsed.query or parsed.fragment):
            raise ValueError("Only canonical HTTPS URLs without credentials, ports or query are allowed")
        hosts.add(parsed.hostname)
    # A single already-approved host avoids new domain approvals, DNS
    # cross-host redirects must still pass the normal bridge policy checks.
    if len(hosts) != 1:
        raise ValueError("All three URLs must be on one already-approved hostname")
    host = next(iter(hosts))
    if not is_domain_currently_allowed(host):
        raise ValueError("Host is not currently allowed; no request was made")


def _load_local_credentials() -> None:
    from bootstrap import read_config
    config = read_config()
    token = os.environ.get("CRAWL4AI_API_TOKEN") or config.get("CRAWL4AI_API_TOKEN", "")
    if not token:
        raise ValueError("Local crawler token unavailable")
    os.environ["CRAWL4AI_API_TOKEN"] = token
    # Set the feature flag only for this isolated foreground process, not
    # the deployed bridge or persistent operator configuration.
    os.environ["LOCAL_SEARCH_STRUCTURED_EXTRACT_PILOT"] = "1"
    if "ALLOWED_CRAWL_DOMAINS" in config:
        os.environ.setdefault("ALLOWED_CRAWL_DOMAINS", config["ALLOWED_CRAWL_DOMAINS"])


def _spec(strategy: str) -> dict:
    return {"strategy": strategy,
            "base_selector": "body" if strategy == "css" else "//body",
            "fields": [{"name": "heading",
                        "selector": "h1" if strategy == "css" else ".//h1",
                        "type": "text"}]}


def report(urls: list[str]) -> dict:
    _require_preapproved(urls)
    _load_local_credentials()
    # Imports after credentials are loaded so bridge.CRAWL4AI_TOKEN is correct.
    import bridge
    import structured_schema
    results = []
    for index, (url, strategy) in enumerate(zip(urls, ("css", "xpath", "css")), start=1):
        start = time.monotonic()
        try:
            # The normal bridge performs its own source/final URL checks,
            # on-demand lease, one-use/epoch checks and guard enforcement.
            response = bridge.web_fetch(url, fresh=True,
                                        structured_spec=_spec(strategy))
            elapsed = round(time.monotonic() - start, 3)
            if type(response) is not dict:
                raise ValueError("Unexpected bridge result")
            extraction = response.get("structured_extraction")
            ok = bool(
                response.get("success") is True and type(extraction) is dict
                and extraction.get("extraction_method") == "crawl4ai_json_" + strategy
                and extraction.get("authorization_verified") is False
                and isinstance(extraction.get("records"), list)
                and extraction.get("records")
                and type(extraction["records"][0]) is dict
                and isinstance(extraction["records"][0].get("heading"), str)
                and extraction["records"][0]["heading"].strip()
                and len(json.dumps(extraction, ensure_ascii=True).encode("utf-8"))
                <= structured_schema.MAX_OUTPUT_BYTES
            )
            results.append({
                "case": index, "strategy": strategy, "success": ok,
                "elapsed_seconds": elapsed,
                "status": "verified" if ok else str(response.get("status", "not_verified"))[:48],
                "records": extraction.get("rows_returned") if type(extraction) is dict else 0,
                "truncated": extraction.get("truncated") if type(extraction) is dict else None,
            })
        except Exception as exc:
            results.append({"case": index, "strategy": strategy, "success": False,
                            "status": "exception_" + type(exc).__name__[:36]})
    return {"mode": "attended_guarded_structured_smoke",
            "approval_changes": False, "persistent_settings_changes": False,
            "installed_bridge_restarted": False,
            "outbound_requests": "existing_guarded_crawl_only",
            "cases": results, "passed": len(results) == 3
            and all(x["success"] for x in results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-approved", action="store_true",
                        help="Explicitly allow one guarded crawl per supplied approved URL")
    parser.add_argument("--url", action="append", default=[], metavar="HTTPS_URL",
                        help="Exactly three distinct paths on ONE currently approved host")
    args = parser.parse_args(argv)
    if not args.live_approved:
        parser.error("Explicit --live-approved opt-in required; no crawl was run")
    try:
        outcome = report(args.url)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        # No raw exception values may contain tokens/URLs/HTML.
        outcome = {"mode": "attended_guarded_structured_smoke", "passed": False,
                   "error": type(exc).__name__}
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
