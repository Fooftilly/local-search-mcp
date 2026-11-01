"""Attended, read-only routing acceptance preflight for the local host.

Usage: ./.venv/bin/python -m scripts.engine_routing_preflight
This does NOT wake containers, send searches, reserve cooldowns, save policy,
or claim that provider quotas or real network quality have been measured.
"""
from __future__ import annotations

import json

import engine_dashboard
import engine_pacing_dashboard
import engine_routing_evidence
import engine_routing_plan
import provider_observations

MAX_CATEGORY_GROUPS = 16


def _plan(names: list[str], config: dict[str, int], history: dict | None,
          *, mode: str) -> dict:
    """Compare fixed name order with fully qualified, provisional evidence."""
    fixed = list(names)
    result = {
        "fixed": fixed, "evidence": fixed, "effective_if_enabled": fixed,
        "evidence_eligible": False, "qualifying_engines": 0,
        "eligible_engines": len(fixed),
    }
    if not fixed or history is None:
        return result
    try:
        comparison = engine_routing_evidence.compare(
            {name: config[name] for name in fixed}, history)
        result["qualifying_engines"] = comparison["comparable_count"]
        qualified = (comparison["complete_history"] is True
                     and comparison["comparable_count"] == len(fixed))
        if qualified:
            ranked = [row["name"] for row in comparison["rows"]]
            if len(ranked) == len(fixed) and set(ranked) == set(fixed):
                result["evidence"] = ranked
                result["evidence_eligible"] = True
                if mode == "evidence":
                    result["effective_if_enabled"] = ranked
    except (TypeError, ValueError, KeyError):
        pass
    return result


def report() -> dict:
    """Inspect already-running backend and private state without mutation.

    An absent, unsafe, or sleeping prerequisite returns a structured blocker
    instead of attempting to repair, wake or silently activate anything.
    """
    output = {
        "mode": "read-only", "searches_sent": 0, "services_started": 0,
        "settings_modified": False, "pacing_enabled": False,
        "routing": "fixed", "status": "unavailable",
        "plans": [], "history_complete": False,
        "next_step": "Run after an attended update while SearXNG is already running.",
    }
    try:
        config = engine_pacing_dashboard.snapshot()
        policy = config["engines"]
        routing = config["routing"]
        if (type(config["enabled"]) is not bool or not isinstance(policy, dict)
                or routing not in ("fixed", "evidence")):
            raise ValueError("Invalid pacing state")
        output["pacing_enabled"] = config["enabled"]
        output["routing"] = routing
        output["configured_engines"] = len(policy)
    except (OSError, ValueError, KeyError, TypeError):
        output["status"] = "invalid_policy"
        output["next_step"] = "Inspect private pacing policy; no changes were made."
        return output

    if not policy:
        output["status"] = "no_configured_engines"
        output["next_step"] = "Stage policy rules in the guarded settings console."
        return output
    try:
        inventory, _, _ = engine_dashboard.snapshot()
        # The existing dashboard verified the owner-controlled bind mount,
        # current container identity and SearXNG live engine inventory.
        general = engine_routing_plan.candidates(inventory, policy)
    except (ValueError, OSError, TypeError,
            engine_dashboard.engines.on_demand_docker.LifecycleError):
        output["status"] = "inventory_unavailable"
        output["next_step"] = "Start SearXNG through the attended operator setup; retry read-only preflight."
        return output

    enabled_names = {row["name"] for row in inventory if row["enabled"]}
    output["configured_but_unavailable"] = sorted(set(policy) - enabled_names,
                                                   key=str.casefold)
    output["eligible_general_engines"] = len(general)
    try:
        history = provider_observations.observations(24)
    except (OSError, ValueError):
        history = None
    output["history_complete"] = bool(
        history is not None and history.get("available") is True
        and history.get("source_snapshot_lock_acquired") is True
        and history.get("retention_or_scan_limited") is False
        and history.get("isolated_engine_groups_truncated") is False)
    output["plans"].append({
        "scope": "general",
        **_plan(general, policy, history, mode=routing),
    })
    categories = sorted({
        row["categories"][0]
        for row in inventory
        if row.get("enabled") is True
        and len(row.get("categories", ())) == 1
        and row["categories"][0] != "general"
        and row["name"] in policy
        and not row["name"].casefold().startswith("google")
    })
    if len(categories) > MAX_CATEGORY_GROUPS:
        output["status"] = "category_inventory_limit"
        output["next_step"] = "Verify the engine inventory before continuing."
        return output
    for category in categories:
        output["plans"].append({
            "scope": category,
            **_plan(engine_routing_plan.candidates(
                inventory, policy, category=category),
                policy, history, mode=routing),
        })
    output["status"] = ("ready_for_attended_comparison" if general
                        else "no_eligible_general_engine")
    output["next_step"] = (
        "Compare fixed and evidence routing using non-sensitive operator-approved queries; "
        "this preflight did not execute or enable any searches."
        if general else
        "Review enabled policy-listed general engines in the guarded console."
    )
    return output


def main() -> None:
    print(json.dumps(report(), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
