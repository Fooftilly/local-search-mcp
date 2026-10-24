"""Portable, allowlisted nonsecret settings transfer for the loopback owner UI.

Each imported profile is one atomic compare-and-swap write through its
existing validated settings reader. No credentials, URL, ports, history,
domain decisions or other files are ever loaded or exported.
"""
from __future__ import annotations

import json

import cache_settings
import safe_settings
import upstream_settings

SCHEMA = "local-search-mcp/portable-settings/v1"
MAX_JSON_BYTES = 4096
PROFILES = {
    "history": (safe_settings, safe_settings.DEFAULTS, safe_settings.BOUNDS),
    "upstream": (upstream_settings, upstream_settings.DEFAULTS,
                 upstream_settings.BOUNDS),
    "cache": (cache_settings, cache_settings.DEFAULTS, cache_settings.BOUNDS),
}


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate settings document field")
        result[key] = value
    return result


def _profile(name: str) -> tuple:
    if type(name) is not str or name not in PROFILES:
        raise ValueError("Unknown portable settings profile")
    return PROFILES[name]


def export_profile(name: str) -> dict:
    module, defaults, _bounds = _profile(name)
    values = module.dashboard_settings()["values"]
    if type(values) is not dict or set(values) != set(defaults):
        raise ValueError("Settings unavailable for export")
    for key, value in values.items():
        if type(value) is not int:
            raise ValueError("Invalid settings value")
    return {"schema": SCHEMA, "profile": name, "settings": values.copy()}


def preview(text: str) -> dict:
    if (type(text) is not str or len(text.encode("utf-8")) > MAX_JSON_BYTES):
        raise ValueError("Portable settings document must be at most 4 KB")
    try:
        doc = json.loads(text, object_pairs_hook=_unique)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid portable settings JSON") from exc
    if type(doc) is not dict or set(doc) != {"schema", "profile", "settings"}:
        raise ValueError("Unknown portable settings document fields")
    if doc["schema"] != SCHEMA:
        raise ValueError("Unsupported portable settings schema")
    module, defaults, bounds = _profile(doc["profile"])
    values = doc["settings"]
    if type(values) is not dict or set(values) != set(defaults):
        raise ValueError("Unexpected settings keys")
    for key, value in values.items():
        lower, upper = bounds[key]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("Settings values must be bounded whole numbers")
    snapshot = module.dashboard_settings()
    if not snapshot["editable"]:
        raise ValueError("Environment overrides active; import is disabled")
    old = snapshot["values"]
    return {
        "profile": doc["profile"],
        "settings": values.copy(),
        "changes": {key: {"old": old[key], "new": value}
                    for key, value in values.items() if old[key] != value},
        "state": snapshot["state"],
    }


def apply(text: str, expected_state: str) -> str:
    """Reparse and recheck when applying; CAS refuses any stale preview."""
    plan = preview(text)
    if plan["state"] != expected_state:
        raise ValueError("Settings changed since preview; review the import again")
    module = PROFILES[plan["profile"]][0]
    module.update_settings(
        {key: str(value) for key, value in plan["settings"].items()},
        expected_state)
    return plan["profile"]
