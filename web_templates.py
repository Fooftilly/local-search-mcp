"""Fixed, bundled dashboard page templates; never derive paths from requests.

Template substitution is intentionally *not* HTML auto-escaping. All dynamic
values are escaped or rendered from audited fixed HTML helpers in admin_ui.
Missing placeholders are errors rather than silently omitted.
"""
from pathlib import Path
from string import Template

_DIR = Path(__file__).resolve().parent / "web" / "templates"
_TEMPLATES = {
    name: Template((_DIR / (name + ".html")).read_text(encoding="utf-8"))
    for name in ("policy", "history", "provider-observations", "status", "cache-settings", "portable-settings",
                 "choice-group", "choice-radio", "pending-nav", "pending-prev", "pending-next",
                 "operations-stat", "operations-unavailable", "service-row", "overview-unavailable",
                 "settings-source", "settings-alert", "settings-locked", "history-settings", "upstream-settings",
                 "decision-row", "decision-table", "decision-empty", "once-grant-row", "once-grant-table",
                 "once-grant-empty", "once-audit-row", "once-audit-table", "once-audit-empty")
}


def render_template(name: str, **escaped_context: object) -> str:
    if name not in _TEMPLATES:
        raise ValueError("Unknown dashboard template")
    return _TEMPLATES[name].substitute(escaped_context)
