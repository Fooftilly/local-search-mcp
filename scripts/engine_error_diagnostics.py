"""Read-only SearXNG per-engine error diagnostics on the owner-verified host.

    ./.venv/bin/python -m scripts.engine_error_diagnostics
    ./.venv/bin/python -m scripts.engine_error_diagnostics --engine brave

Reads only local SearXNG /config (via the guarded owner inventory) and
/stats/errors. No provider search, backend wake, rate-limit reservation,
settings change, or raw error/log text is performed or disclosed.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys

import httpx

import engine_dashboard
import engine_pacing_dashboard

_STATS_URL = "http://127.0.0.1:8888/stats/errors"
_MAX_BYTES = 96 * 1024
_MAX_ENGINES = 8
_MAX_ERROR_ROWS = 24
_MAX_STAT_ENGINES = 512
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}\Z", re.ASCII)
_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,79}\Z", re.ASCII)


class DiagnosticError(ValueError):
    """Reject unexpected or unsafe upstream statistics without exposing them."""


def _names(requested: list[str] | None) -> list[str]:
    if requested is None:
        try:
            policy = engine_pacing_dashboard.snapshot()["engines"]
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise DiagnosticError("policy_unavailable") from exc
        if not isinstance(policy, dict):
            raise DiagnosticError("policy_unavailable")
        names = list(policy)
    else:
        names = requested
    if (not isinstance(names, list) or len(names) > _MAX_ENGINES
            or any(not isinstance(name, str) or not _NAME.fullmatch(name)
                   or name != name.strip()
                   or name.casefold().startswith("google") for name in names)
            or len({name.casefold() for name in names}) != len(names)):
        raise DiagnosticError("invalid_selection")
    return sorted(names, key=str.casefold)


def _error_category(value: object) -> str:
    """Use broad known classes, never raw upstream strings or log details."""
    if not isinstance(value, str) or not _CLASS.fullmatch(value):
        return "unclassified"
    name = value.rsplit(".", 1)[-1].casefold()
    if "toomanyrequests" in name:
        return "rate_limit"
    if "captcha" in name:
        return "captcha"
    if "accessdenied" in name:
        return "access_denied"
    if "timeout" in name or name == "timeouterror":
        return "timeout"
    if name in ("keyerror", "indexerror", "typeerror", "valueerror",
                "searxenginexpathexception", "searxengineparsingexception"):
        return "parser_or_response"
    if any(term in name for term in ("connect", "network", "proxy", "sslerror")):
        return "network"
    return "other"


def _parse(raw: bytes, selected: list[str]) -> list[dict]:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise DiagnosticError("invalid_stats") from exc
    if not isinstance(data, dict) or len(data) > _MAX_STAT_ENGINES:
        raise DiagnosticError("invalid_stats")
    result = []
    for engine in selected:
        entries = data.get(engine, [])
        if not isinstance(entries, list) or len(entries) > _MAX_ERROR_ROWS:
            raise DiagnosticError("invalid_stats")
        problems = []
        for row in entries:
            if not isinstance(row, dict):
                raise DiagnosticError("invalid_stats")
            value = row.get("percentage")
            if (value is not None and
                    (type(value) not in (int, float) or not math.isfinite(value)
                     or not 0 <= value <= 100)):
                raise DiagnosticError("invalid_stats")
            secondary = row.get("secondary", False)
            if type(secondary) is not bool:
                raise DiagnosticError("invalid_stats")
            problems.append({
                "category": _error_category(row.get("exception_classname")),
                "reported_percentage": round(value, 1) if value is not None else None,
                "secondary": secondary,
            })
        result.append({
            "engine": engine,
            "status": "error_records_present" if problems else "no_error_records",
            "error_entries": len(problems),
            "errors": problems,
        })
    return result


def _get_stats(names: list[str]) -> list[dict]:
    # Use a fixed loopback origin, not bridge.SEARXNG_URL or an environment
    # override. Never follow redirects, send cookies, or read large responses.
    with httpx.Client(timeout=4.0, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", _STATS_URL) as response:
            if response.status_code != 200:
                raise DiagnosticError("stats_unavailable")
            if response.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise DiagnosticError("stats_unavailable")
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > _MAX_BYTES:
                    raise DiagnosticError("invalid_stats")
                raw.extend(chunk)
    return _parse(bytes(raw), names)


def report(requested: list[str] | None = None) -> dict:
    """Inspect only selected, owner-verified, currently enabled engines."""
    output = {
        "mode": "read-only",
        "source": "local_searxng_process_metrics",
        "status": "unavailable",
        "provider_searches_sent": 0,
        "services_started": 0,
        "settings_modified": False,
        "engine_errors": [],
        "limitations": ("SearXNG in-memory error statistics are not a 24-hour "
                        "provider uptime or quota measurement; missing errors "
                        "do not prove an engine works."),
    }
    try:
        names = _names(requested)
    except DiagnosticError as exc:
        output["status"] = str(exc)
        return output
    if not names:
        output["status"] = "no_configured_engines"
        return output
    try:
        inventory, _ = engine_dashboard.verified_inventory()
        enabled = {row["name"] for row in inventory
                   if row.get("enabled") is True
                   and not row["name"].casefold().startswith("google")}
    except (ValueError, OSError, TypeError, RuntimeError, KeyError):
        output["status"] = "inventory_unavailable"
        return output
    missing = sorted(set(names) - enabled, key=str.casefold)
    if missing:
        output["status"] = "engine_unavailable"
        output["unavailable_engines"] = missing
        return output
    try:
        output["engine_errors"] = _get_stats(names)
    except (DiagnosticError, httpx.RequestError, OSError):
        output["status"] = "stats_unavailable"
        return output
    output["status"] = "ok"
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read existing owner-verified SearXNG /stats/errors; no searches.")
    parser.add_argument("--engine", action="append", default=None,
                        help="Exact enabled engine name (up to 8); default: staged policy engines")
    args = parser.parse_args(argv)
    result = report(args.engine)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
