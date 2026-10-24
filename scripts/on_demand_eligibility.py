"""Inert on-demand eligibility checker. Never starts, stops, or configures Docker."""
from __future__ import annotations

import argparse
import json
from scripts import backend_inventory

DEFAULT_IDLE_MINUTES = 30
MAX_IDLE_MINUTES = 1440


def validate_options(*, enabled: bool = False, idle_minutes: int = DEFAULT_IDLE_MINUTES) -> dict:
    """Validate future opt-in settings without persisting them."""
    if type(enabled) is not bool:
        raise ValueError("Enabled must be boolean")
    if type(idle_minutes) is not int or not 1 <= idle_minutes <= MAX_IDLE_MINUTES:
        raise ValueError("Idle timeout must be 1-1440 whole minutes")
    return {"enabled": enabled, "idle_minutes": idle_minutes}


def eligibility(*, inventory: dict | None = None) -> dict:
    """Read-only candidate check, NOT an authorization to enable sleep/wake."""
    if inventory is None:
        inventory = backend_inventory.report()
    result = {
        "mode": "read-only", "feature": "on-demand-backends",
        "enabled": False, "can_activate": False,
        "default_idle_minutes": DEFAULT_IDLE_MINUTES,
        "restart_policy_review": "unknown",
        "state": "blocked",
        "reason": "Cannot verify dedicated backend ownership",
        "next_gates": [
            "Prove exactly one dedicated SearXNG and one Crawl4AI container",
            "Live firewall/egress validation after every crawler wake",
            "Race-safe startup, full-stop suppression and restart-policy recovery",
            "Preserve bridge and ChatGPT tunnel through idle sleep",
        ],
    }
    if not isinstance(inventory, dict):
        return result
    external = inventory.get("other_published_crawlers")
    unknown = inventory.get("unverified_project_containers")
    running = inventory.get("managed_running")
    stopped = inventory.get("managed_stopped")
    if any(type(v) is not int or v < 0 for v in
           (external, unknown, running, stopped)):
        return result
    # Refuse older count-only reports and any ambiguous service selection.
    # Counts alone could represent two crawler containers, no SearXNG.
    services = inventory.get("verified_services")
    services_ok = (type(services) is dict
                   and set(services) == {"crawl4ai", "searxng"}
                   and all(type(services[key]) is int and services[key] == 1
                           for key in ("crawl4ai", "searxng")))
    # Inspect actual Docker restart policies before claiming a candidate.
    # 'unless-stopped' is the normal Compose default and requires planned
    # coordination before any future automatic idle-stop operation.
    modes = inventory.get("verified_restart_policies")
    modes_ok = (type(modes) is dict
                and set(modes) == {"crawl4ai", "searxng"}
                and all(type(modes[s]) is str
                        and modes[s] in ("no", "always", "unless-stopped", "on-failure")
                        for s in ("crawl4ai", "searxng")))
    state = inventory.get("state")
    if external:
        result["reason"] = "Another crawler publishes this port; never take over a shared stack"
    elif unknown or state in ("unknown", "unverified"):
        result["reason"] = "Docker ownership or project state is unverified"
    elif state in ("not_configured", "not_started"):
        result["reason"] = "No verified private backend deployment is active"
    elif (state in ("running", "partially_running", "stopped")
          and running + stopped == 2 and services_ok and modes_ok):
        result["state"] = "manual_verification_required"
        result["restart_policy_review"] = (
            "requires_coordinated_change"
            if any(modes[s] != "no" for s in ("crawl4ai", "searxng"))
            else "no_change_observed")
        result["reason"] = (
            "Potential dedicated deployment; restart-policy coordination, "
            "least-privilege controls and live firewall gate still require review")
    else:
        result["reason"] = "Expected exactly one verified crawler and one search backend"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle-minutes", type=int, default=DEFAULT_IDLE_MINUTES,
                        help="preview a future 1-1440 minute timeout (default 30)")
    args = parser.parse_args(argv)
    try:
        options = validate_options(idle_minutes=args.idle_minutes)
    except ValueError as exc:
        parser.error(str(exc))
    result = eligibility()
    result["preview"] = options
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
