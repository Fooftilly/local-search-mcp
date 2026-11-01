"""Operator-loopback SearXNG engine selection using the existing guarded editor.

No new settings writer, hidden backend wake, or MCP mutation tool.
"""
from __future__ import annotations

import html
import secrets

from scripts import searxng_engines as engines


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
    if not 1 <= len(general) <= 32:
        raise engines.EngineSetupError("General search-engine inventory unavailable")
    return inventory, general, engines.inventory_state(inventory, source)


def apply_form(fields: dict[str, list[str]]) -> str:
    """Validate an explicit checked selection before the guarded YAML writer."""
    chosen = fields.get("engine", [])
    if (not isinstance(chosen, list) or not 1 <= len(chosen) <= 32
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
    return engines.apply(tuple(chosen), general, expected_state=state)


def page(csrf: str, *, message: str = "", error: bool = False) -> str:
    note = ""
    if message:
        note = ('<p class="alert ' + ('error' if error else 'success') +
                '" role="status">' + _escape(message) + '</p>')
    try:
        inventory, general, state = snapshot()
        rows = []
        for item in inventory:
            if item["name"] not in general:
                continue
            name = item["name"]
            is_google = name.casefold().startswith("google")
            checked = " checked" if item["enabled"] and not is_google else ""
            disabled = " disabled" if is_google else ""
            rows.append(
                '<label class="choice-option"><input type="checkbox" name="engine" '
                'value="' + _escape(name) + '"' + checked + disabled +
                '><span class="choice-text">' + _escape(name) +
                (' (not eligible: Google)' if is_google else '') +
                ' · ' + _escape(", ".join(item["categories"])) +
                '</span></label>')
        form = (
            '<form method="post" action="/engine-settings">'
            '<input type="hidden" name="csrf" value="' + _escape(csrf) + '">'
            '<input type="hidden" name="state" value="' + _escape(state) + '">'
            '<fieldset class="choice-field"><legend>Default general-search engines</legend>'
            '<div class="choice-options">' + "".join(rows) + '</div></fieldset>'
            '<p>Choose at least one non-Google engine. Flags apply across all '
            'of each engine\'s categories. Saving the selection restarts the '
            'verified running SearXNG container and may interrupt searches.</p>'
            '<label>Type APPLY <input name="confirm" autocomplete="off" '
            'required></label><button type="submit">Save engine selection</button>'
            '</form>')
    except (engines.EngineSetupError, ValueError, OSError) as exc:
        # Show only vetted, general errors: never display Docker inspection
        # responses, private YAML, paths or exception arguments.
        msg = (str(exc) if isinstance(exc, engines.EngineSetupError)
               else "Engine inventory unavailable")
        form = '<p role="status">' + _escape(msg) + '</p>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="no-referrer">'
        '<title>Local Search MCP — Engine selection</title>'
        '<link rel="stylesheet" href="/dashboard.css"></head><body><main>'
        '<h1>Search engine selection</h1>'
        '<p>This local page reads the running SearXNG engine inventory without '
        'waking sleeping services. Updates reuse the protected operator setup '
        'editor, Docker ownership checks, checkpoint and rollback. Explicitly '
        'selected engines in MCP calls are not changed here.</p>'
        + note + form +
        '<p><a href="/engine-settings">Refresh inventory</a> · '
        '<a href="/status">Service settings</a> · '
        '<a href="/">Operator workspace</a></p>'
        '</main></body></html>')
