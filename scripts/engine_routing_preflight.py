"""Attended, read-only routing acceptance preflight for the local host.

Usage: ./.venv/bin/python -m scripts.engine_routing_preflight
This does NOT wake containers, send searches, reserve cooldowns, save policy,
or claim that provider quotas or real network quality have been measured.
"""
from __future__ import annotations

import json

import httpx
import bridge
import engine_dashboard
import engine_pacing_dashboard
import engine_routing_evidence
import engine_routing_plan
import provider_observations
import upstream_settings

MAX_CATEGORY_GROUPS = 16


def _verified_raw_active_inventory(verified_inventory: list[dict]) -> list[dict]:
    """Recheck the bridge's *unmodified* bounded /config category semantics.

    The operator inventory reader may normalize repeated category names.
    Compare exact categories and enabled names before publishing an
    acceptance plan; on drift/ambiguity, fail closed without a /search.
    """
    with httpx.Client(
            timeout=upstream_settings.effective_timeout("searxng_seconds"),
            trust_env=False) as client:
        active = bridge._enabled_search_inventory(client, require_categories=True)
    if isinstance(active, dict):
        raise ValueError("Raw inventory unavailable")
    owner_active = {
        row["name"]: tuple(row["categories"])
        for row in verified_inventory if row["enabled"] is True
    }
    actual_active = {row["name"]: row["categories"] for row in active}
    if owner_active != actual_active:
        raise ValueError("Operator and bridge inventory differ")
    return active



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
        inventory, _ = engine_dashboard.verified_inventory()
        # Verify the container/bind-mount through the operator read path,
        # then validate *raw* /config category rows through the exact bridge
        # parser. Any normalization or concurrent drift fails closed.
        active = _verified_raw_active_inventory(inventory)
        general = engine_routing_plan.candidates(active, policy)
    except (ValueError, OSError, TypeError,
            engine_dashboard.engines.on_demand_docker.LifecycleError):
        output["status"] = "inventory_unavailable"
        output["next_step"] = "Start SearXNG through the attended operator setup; retry read-only preflight."
        return output

    enabled_names = {row["name"] for row in active}
    output["configured_but_unavailable"] = sorted(set(policy) - enabled_names,
                                                   key=str.casefold)
    output["eligible_general_engines"] = len(general)
    output["general_route_available"] = bool(general)
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
        for row in active
        if len(row.get("categories", ())) == 1
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
                active, policy, category=category),
                policy, history, mode=routing),
        })
    any_eligible = any(plan["eligible_engines"] for plan in output["plans"])
    output["status"] = ("ready_for_attended_comparison" if any_eligible
                        else "no_eligible_engines")
    output["next_step"] = (
        "Compare fixed and evidence routing using non-sensitive operator-approved queries; "
        "this preflight did not execute or enable any searches."
        if any_eligible else
        "Review enabled policy-listed engines in the guarded console."
    )
    return output


def main() -> None:
    print(json.dumps(report(), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
