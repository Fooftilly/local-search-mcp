"""Validated live cache lifetimes stored through the existing private settings layer."""
from __future__ import annotations

from upstream_settings import SettingsProfile
import upstream_settings as _store

SCHEMA = "local-search-mcp/cache-ttl/v1"
FILENAME = "cache-ttl.json"
DEFAULTS = {"search_seconds": 45, "fetch_seconds": 120}
BOUNDS = {"search_seconds": (5, 900), "fetch_seconds": (5, 1800)}
OVERRIDES = {
    "search_seconds": "LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL",
    "fetch_seconds": "LOCAL_SEARCH_MCP_CACHE_FETCH_TTL",
}
_PROFILE = SettingsProfile(
    SCHEMA, FILENAME, DEFAULTS, BOUNDS, OVERRIDES, "cache TTL settings",
    ".cache-ttl.lock", ".cache-ttl-", 4,
)


def dashboard_settings() -> dict:
    return _store.dashboard_settings(_PROFILE)


def effective_ttl(key: str) -> int:
    return _store.effective_timeout(key, profile=_PROFILE)


def update_settings(fields: dict[str, str], expected_state: str) -> None:
    _store.update_settings(fields, expected_state, profile=_PROFILE)
