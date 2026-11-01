"""Read-only evidence comparison of directly selected local search operations.

Not an upstream uptime estimate, provider quota, routing policy, or automatic
selection algorithm. Only configured engine names can enter this preview.
"""
from __future__ import annotations

import math

MIN_DIRECT_OPERATIONS = 10
WINDOW_HOURS = 24


def compare(policy_engines: dict[str, int], report: dict) -> dict:
    """Return bounded, nonsecret, stable-order comparison data.

    Reporting truncation, unlocked history, missing observations or insufficient
    direct samples makes *all* comparative claims provisional. Observed result
    fraction uses only single-engine operations actually recorded in history,
    never fanout hits or recovery attempts.
    """
    if not isinstance(policy_engines, dict) or len(policy_engines) > 64:
        raise ValueError("Invalid configured engine list")
    if not isinstance(report, dict) or report.get("hours") != WINDOW_HOURS:
        raise ValueError("Invalid history window")
    summaries = report.get("isolated_engine_operations")
    if not isinstance(summaries, list) or len(summaries) > 24:
        raise ValueError("Invalid observation summaries")
    lookup = {}
    for entry in summaries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("Invalid observation entry")
        name = entry["name"]
        if name in lookup:
            raise ValueError("Duplicate observation entry")
        lookup[name] = entry

    reliable = (report.get("available") is True
                and report.get("source_snapshot_lock_acquired") is True
                and report.get("retention_or_scan_limited") is False
                and report.get("isolated_engine_groups_truncated") is False)
    rows = []
    for name in sorted(policy_engines, key=str.casefold):
        row = lookup.get(name, {})
        attempts = row.get("direct_operations", 0)
        successes = row.get("direct_verified_results", 0)
        latency = row.get("direct_verified_result_mean_ms")
        if (type(attempts) is not int or type(successes) is not int
                or not 0 <= successes <= attempts <= 20_000
                or (latency is not None and
                    (type(latency) not in (int, float)
                     or not math.isfinite(latency)
                     or not 0 <= latency <= 3_600_000))):
            raise ValueError("Invalid isolated search measurements")
        comparable = reliable and attempts >= MIN_DIRECT_OPERATIONS and successes > 0 and latency is not None
        rows.append({
            "name": name,
            "attempts": attempts,
            "results": successes,
            "fraction": round(100 * successes / attempts, 1) if attempts else None,
            "mean_success_ms": latency,
            "comparable": comparable,
        })
    # Unqualified engines stay in predictable name order. Eligible rows rank
    # by observed direct verified result fraction, then successful local
    # round-trip latency (not upstream provider latency).
    rows.sort(key=lambda row: (
        not row["comparable"],
        -(row["results"] / row["attempts"]) if row["comparable"] else 0,
        row["mean_success_ms"] if row["comparable"] else 0,
        row["name"].casefold()))
    return {"rows": rows, "complete_history": reliable,
            "comparable_count": sum(row["comparable"] for row in rows),
            "configured_count": len(rows)}
