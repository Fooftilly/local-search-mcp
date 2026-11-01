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
    for name in (
        "policy",
        "history",
        "provider-observations",
        "status",
        "cache-settings",
        "portable-settings",
        "engine-settings",
        "engine-form",
        "engine-option",
        "engine-alert",
        "engine-unavailable",
        "choice-group",
        "choice-radio",
        "pending-nav",
        "pending-prev",
        "pending-next",
        "operations-stat",
        "operations-unavailable",
        "service-row",
        "overview-unavailable",
        "settings-source",
        "settings-alert",
        "settings-locked",
        "history-settings",
        "upstream-settings",
        "decision-row",
        "decision-table",
        "decision-empty",
        "once-grant-row",
        "once-grant-table",
        "once-grant-empty",
        "once-audit-row",
        "once-audit-table",
        "once-audit-empty",
        "history-nav",
        "history-prev",
        "history-next",
        "history-row",
        "history-engine-cell",
        "history-empty",
        "history-unavailable",
        "history-table",
        "history-summary-row",
        "history-summary-empty",
        "history-summary-unavailable",
        "history-pages-unavailable",
        "history-filter-notice",
        "history-filter-warning",
        "history-filter-correction",
        "status-port-row",
        "status-dashboard-port",
        "status-mcp-port",
        "status-limit-row",
        "provider-time-row",
        "provider-time-empty",
        "provider-failure-row",
        "provider-failure-empty",
        "provider-coverage-row",
        "provider-source-unavailable",
        "provider-unavailable-two",
        "provider-unavailable-four",
        "provider-no-files",
        "provider-partial",
        "provider-bounded",
        "import-rule-row",
        "import-rule-conflict",
        "import-rule-safe",
        "import-rule-form",
        "import-rule-no-change",
        "import-rule-page",
        "portable-preview-row",
        "portable-preview-empty",
        "portable-preview-page",
        "domain-review-context",
        "domain-limits",
        "domain-request-meta",
        "domain-reason",
        "domain-action-form",
        "domain-quick-form",
        "domain-once-form",
        "domain-edit-form",
        "domain-bulk-select",
        "domain-maintenance",
        "domain-table-row",
        "domain-empty-pending",
        "domain-empty-decided",
        "domain-table-head",
        "domain-alert",
        "provider-isolated-row",
        "provider-isolated-empty",
        "provider-isolated-unavailable"
    )
}


def render_template(name: str, **escaped_context: object) -> str:
    if name not in _TEMPLATES:
        raise ValueError("Unknown dashboard template")
    return _TEMPLATES[name].substitute(escaped_context)
