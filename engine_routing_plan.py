"""Pure subset selection shared by the bridge and attended routing preflight.

Callers must supply verified, bounded SearXNG engine inventory. No provider
request, Docker command, policy write or history read occurs in this module.
"""
from __future__ import annotations


def candidates(rows: list[dict], policy: dict[str, int],
               *, category: str | None = None) -> list[str]:
    """Return deterministic eligible names without widening category scope.

    General selection uses membership of the general category, matching the
    existing bridge behavior. Category selection requires the engine's full
    category list to contain ONLY the exact requested category because
    SearXNG unions filters. Disabled and Google engines are never eligible.
    """
    if (not isinstance(rows, list) or len(rows) > 512
            or not isinstance(policy, dict) or len(policy) > 64
            or (category is not None and (not isinstance(category, str)
                                          or not 1 <= len(category) <= 80))):
        raise ValueError("Invalid verified engine inventory or policy")
    selected = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid verified engine row")
        name, categories = row.get("name"), row.get("categories")
        enabled = row.get("enabled", True)
        if (not isinstance(name, str) or not 1 <= len(name) <= 80
                or not isinstance(categories, (list, tuple))
                or not 1 <= len(categories) <= 16
                or any(not isinstance(cat, str) or not 1 <= len(cat) <= 80
                       for cat in categories)
                or type(enabled) is not bool or name.casefold() in seen):
            raise ValueError("Invalid verified engine entry")
        seen.add(name.casefold())
        if (not enabled or name not in policy
                or name.casefold().startswith("google")):
            continue
        if ((category is None and "general" in categories)
                or (category is not None and tuple(categories) == (category,))):
            selected.append(name)
    return sorted(selected, key=str.casefold)
