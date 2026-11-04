"""Attended, bounded direct-engine sampling for routing evidence (#256).

Default mode is offline/read-only acceptance preview; no provider queries.
The SAMPLE action uses only three fixed, nonsensitive public query presets and
the existing guarded web_search tool. Search history WILL record the public
preset query, just like other web_search calls. Never pass secrets as presets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import bridge
from scripts import engine_routing_preflight

# No operator input, CLI arguments or environment can supply search text.
# Public, stable strings are intentionally saved by normal private history.
PRESETS = {
    "docs": "Python language official documentation",
    "science": "open access scientific research",
    "montenegro": "Montenegro government digital public services",
}
MAX_ENGINES = 3
MAX_RESULTS = 3
BETWEEN_CALL_SECONDS = 2.0
ENGINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}\Z", re.ASCII)


def _verified_plan() -> tuple[list[str], str, bool]:
    """Permit only the verified general candidates from the current preflight."""
    preview = engine_routing_preflight.report()
    if (not isinstance(preview, dict)
            or preview.get("status") != "ready_for_attended_comparison"
            or type(preview.get("pacing_enabled")) is not bool
            or preview.get("routing") not in ("fixed", "evidence")
            or not isinstance(preview.get("plans"), list)
            or not preview["plans"]
            or not isinstance(preview["plans"][0], dict)
            or preview["plans"][0].get("scope") != "general"):
        raise ValueError("routing_preflight_unavailable")
    names = preview["plans"][0].get("fixed")
    if (not isinstance(names, list)
            or not 1 <= len(names) <= MAX_ENGINES
            or any(not isinstance(name, str) or not ENGINE_NAME.fullmatch(name)
                   or name.casefold().startswith("google") for name in names)
            or len({name.casefold() for name in names}) != len(names)):
        raise ValueError("candidate_count_or_inventory_invalid")
    return list(names), preview["routing"], preview["pacing_enabled"]


def preview() -> dict:
    """No active provider request, pacing reservation or persistent mutation."""
    output = {
        "mode": "read-only", "status": "unavailable",
        "web_search_calls_attempted": 0, "policy_modified": False,
        "public_query_may_be_retained_in_private_history": False,
        "candidate_engines": [],
        "max_direct_engine_calls": MAX_ENGINES,
        "max_possible_upstream_search_requests": MAX_ENGINES * 3,
        "supported_profiles": list(PRESETS),
    }
    try:
        names, routing, enabled = _verified_plan()
        output.update(status="ready_for_operator_confirmation",
                      candidate_engines=names, routing=routing,
                      pacing_enabled=enabled)
    except (ValueError, OSError, TypeError, KeyError):
        output["status"] = "preflight_unavailable"
    return output


def _outcome(result: object) -> dict:
    """Redact everything except validated result count and coarse status."""
    if isinstance(result, list):
        if len(result) > MAX_RESULTS or any(not isinstance(row, dict)
                                            for row in result):
            return {"status": "invalid_response", "result_count": None}
        return {"status": "results" if result else "empty",
                "result_count": len(result)}
    if isinstance(result, dict):
        if result.get("success") is True:
            items = result.get("results")
            if (not isinstance(items, list) or len(items) > MAX_RESULTS
                    or any(not isinstance(row, dict) for row in items)):
                return {"status": "invalid_response", "result_count": None}
            declared = result.get("status")
            return {"status": "results" if items else "empty",
                    "result_count": len(items),
                    "searxng_reported_status": (
                        declared if declared in ("ok", "degraded", "unknown")
                        else "unknown")}
        if result.get("success") is False:
            value = result.get("status")
            return {"status": ("rate_limited" if value == "rate_limited"
                               else "rejected" if value in (
                                   "pacing_selection_required", "pacing_unavailable",
                                   "explicit_selection_unavailable")
                               else "upstream_failure"),
                    "result_count": None}
    return {"status": "invalid_response", "result_count": None}


def sample(profile: str, *, confirm=input) -> dict:
    """Explicit bounded operator action. No arbitrary query text can enter."""
    output = {
        "mode": "attended", "status": "refused",
        "profile": profile if profile in PRESETS else "invalid",
        "web_search_calls_attempted": 0, "policy_modified": False,
        "public_query_may_be_retained_in_private_history": False,
        "measurements": [],
        "limitations": ("These are local end-to-end tool-call measurements, "
                        "not provider response-time, uptime, quota, or evidence "
                        "of general search relevance."),
    }
    if profile not in PRESETS or os.geteuid() == 0:
        return output
    try:
        names, routing, enabled = _verified_plan()
    except (OSError, ValueError, TypeError, KeyError):
        output["status"] = "preflight_unavailable"
        return output
    print(
        "Will send one fixed PUBLIC query to each verified engine, at most "
        f"{len(names)} tool calls (up to three upstream /search attempts each). "
        "These queries and result metadata are saved in private local history. "
        "No new providers or configuration changes. Type SEARCH to proceed.",
        file=sys.stderr,
    )
    try:
        approved = confirm("Confirmation: ")
    except (EOFError, KeyboardInterrupt):
        approved = ""
    if approved != "SEARCH":
        output["status"] = "cancelled"
        return output

    output["status"] = "completed"
    for index, name in enumerate(names):
        if index:
            time.sleep(BETWEEN_CALL_SECONDS)
        # Recheck the two verified views before EACH operation. A concurrent
        # policy edit, backend sleep, or engine-category drift stops sampling.
        try:
            current, current_routing, current_enabled = _verified_plan()
        except (OSError, ValueError, TypeError, KeyError):
            output["status"] = "preflight_changed"
            break
        if (current != names or current_routing != routing
                or current_enabled != enabled):
            output["status"] = "preflight_changed"
            break
        before = time.monotonic()
        output["web_search_calls_attempted"] += 1
        output["public_query_may_be_retained_in_private_history"] = True
        try:
            result = bridge.web_search(
                PRESETS[profile], engines=name,
                max_results=MAX_RESULTS, fresh=True,
                include_search_status=True,
                record_provider_diagnostics=False,
            )
        except Exception:
            # Never expose an upstream exception (URLs, provider logs, token
            # strings); stop rather than contacting another engine afterward.
            output["measurements"].append({
                "engine": name, "status": "error", "result_count": None})
            output["status"] = "interrupted"
            break
        elapsed = max(0, round((time.monotonic() - before) * 1000))
        output["measurements"].append({"engine": name, **_outcome(result),
                                       "local_tool_call_ms": elapsed})
    if output["status"] == "completed" and len(output["measurements"]) != len(names):
        output["status"] = "interrupted"
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--sample", action="store_true",
                        help="send fixed public sample queries after terminal consent")
    parser.add_argument("--profile", choices=tuple(PRESETS), default="docs")
    args = parser.parse_args(argv)
    if not args.sample:
        result = preview()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "ready_for_operator_confirmation" else 1
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print(json.dumps({"mode": "attended", "status": "tty_required",
                          "web_search_calls_attempted": 0}))
        return 1
    result = sample(args.profile)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
