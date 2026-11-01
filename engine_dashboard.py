"""Operator-loopback SearXNG engine selection using the existing guarded editor.

No new settings writer, hidden backend wake, or MCP mutation tool.
"""
from __future__ import annotations

import html
import secrets

from scripts import searxng_engines as engines
import request_cache
from web_templates import render_template


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def snapshot() -> tuple[list[dict], tuple[str, ...], str]:
    """Verified inventory and the current guarded config's opaque revision."""
    target = engines.on_demand_docker._safe_target("searxng")
    if not target.running:
        raise engines.EngineSetupError(
            "SearXNG is sleeping. Use ./setup.sh --search-engines for attended WAKE.")
    path = engines.settings_path()
    engines._check_bind_mount(target.ident, path)
    source = engines._read_settings(path)
    inventory = engines.live_engines()
    general = tuple(row["name"] for row in inventory
                    if "general" in row["categories"])
    if not 1 <= len(general) <= 512:
        raise engines.EngineSetupError("General search-engine inventory unavailable")
    return inventory, general, engines.inventory_state(inventory, source)


def apply_form(fields: dict[str, list[str]]) -> str:
    """Validate an explicit checked selection before the guarded YAML writer."""
    chosen = fields.get("engine", [])
    if (not isinstance(chosen, list) or not 1 <= len(chosen) <= 512
            or any(not isinstance(x, str) for x in chosen)
            or len(set(chosen)) != len(chosen)):
        raise ValueError("Select one or more unique general search engines")
    if fields.get("confirm") != ["APPLY"]:
        raise ValueError("Type APPLY to confirm the engine selection")
    if (not isinstance(fields.get("state"), list)
            or len(fields["state"]) != 1):
        raise ValueError("Engine inventory revision is missing")
    inventory, general, state = snapshot()
    if not secrets.compare_digest(fields["state"][0], state):
        raise ValueError("Engine inventory changed; refresh before applying")
    if (not set(chosen).issubset(set(general))
            or any(name.casefold().startswith("google") for name in chosen)):
        raise ValueError("Engine selection is unavailable or prohibited")
    # apply() revalidates the inventory + original YAML *inside* its cross-
    # process lock, before writing/restarting the owner-verified container.
    result = engines.apply(tuple(chosen), general, expected_state=state)
    # The dashboard and MCP bridge share one process. Invalidate only after
    # verified completion, detaching pre-change in-flight search loaders.
    request_cache.invalidate_search()
    return result


def page(csrf: str, *, message: str = "", error: bool = False) -> str:
    """Render fixed web templates; data/validation remain separate from HTML."""
    alert = (render_template("engine-alert",
                             kind="error" if error else "success",
                             message=_escape(message)) if message else "")
    try:
        inventory, general, state = snapshot()
        options = []
        available = set(general)
        for item in inventory:
            if item["name"] not in available:
                continue
            name = item["name"]
            google = name.casefold().startswith("google")
            options.append(render_template(
                "engine-option", engine_name=_escape(name),
                checked="checked" if item["enabled"] and not google else "",
                disabled="disabled" if google else "",
                restriction=" (not eligible: Google)" if google else "",
                categories=_escape(", ".join(item["categories"]))))
        form = render_template(
            "engine-form", csrf=_escape(csrf), state=_escape(state),
            rows="".join(options))
    except (engines.EngineSetupError, engines.on_demand_docker.LifecycleError,
            ValueError, OSError) as exc:
        detail = (str(exc) if isinstance(exc, engines.EngineSetupError)
                  else "Engine inventory unavailable")
        form = render_template("engine-unavailable", message=_escape(detail))
    return render_template("engine-settings", alert=alert, form=form)
