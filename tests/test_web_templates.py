"""The HTML templates are fixed assets, never request-controlled paths."""
from pathlib import Path
import inspect
import unittest
from unittest.mock import patch

import admin_ui
from web_templates import render_template


class DashboardTemplateTests(unittest.TestCase):
    def test_fixed_templates_exist_without_inline_css_or_javascript(self):
        folder = Path(__file__).resolve().parents[1] / "web" / "templates"
        for name in ("policy", "history", "status", "cache-settings"):
            with self.subTest(name=name):
                raw = (folder / (name + ".html")).read_text()
                self.assertIn('href="/dashboard.css"', raw)
                self.assertNotIn("<style>", raw)
                self.assertNotIn("<script>", raw)
                if name in ("policy", "history"):
                    self.assertIn('src="/dashboard-live.js"', raw)
                else:
                    self.assertNotIn("<script", raw)

    def test_common_ui_helpers_use_fixed_templates_not_inline_html(self):
        for fn in (admin_ui.choice_buttons,
                   admin_ui.pending_pagination_html,
                   admin_ui.operations_overview_html,
                   admin_ui.overview_summary_html,
                   admin_ui.service_overview_rows,
                   admin_ui.history_settings_page,
                   admin_ui.upstream_settings_page,
                   admin_ui.cache_settings_page,
                   admin_ui.portable_settings_page,
                   admin_ui.decision_audit_rows,
                   admin_ui.decision_audit_html,
                   admin_ui.once_grants_html,
                   admin_ui.once_audit_html,
                   admin_ui.history_pagination_html,
                   admin_ui.history_items_html,
                   admin_ui.recent_rows,
                   admin_ui.history_summary_html,
                   admin_ui.status_page,
                   admin_ui.history_page,
                   admin_ui.provider_observations_page):
            with self.subTest(fn=fn.__name__):
                self.assertNotRegex(inspect.getsource(fn), r"""['"]<[a-zA-Z]""")
        group = admin_ui.choice_buttons(
            "mode", "Choose <mode>", (("a", "A&B"), ("b", "B")),
            selected="a")
        self.assertIn("&lt;mode&gt;", group)
        self.assertIn('name="mode" value="a" checked required', group)
        self.assertIn("A&amp;B", group)
        self.assertNotIn("<mode>", group)
        next_page = admin_ui.pending_pagination_html(
            "<script>", 1, admin_ui.PENDING_PAGE_SIZE + 1)
        self.assertIn("Next 200", next_page)
        self.assertNotIn("<script>", next_page)
        self.assertIn("%3Cscript%3E", next_page)

    def test_migrated_settings_pages_are_external_templates(self):
        with (patch.object(admin_ui.safe_settings, "dashboard_settings",
                           return_value={"values": {"retention_days": 30, "max_bytes": 8388608},
                                         "state": "revision", "editable": True,
                                         "environment_override": {
                                             "retention_days": False, "max_bytes": False}}),
              patch.object(admin_ui.upstream_settings, "dashboard_settings",
                           return_value={"values": {"searxng_seconds": 25,
                                                      "crawl_seconds": 75},
                                         "state": "revision", "editable": False,
                                         "environment_override": {
                                             "searxng_seconds": True,
                                             "crawl_seconds": False}})):
            history = admin_ui.history_settings_page(message='<test>')
            upstream = admin_ui.upstream_settings_page()
        self.assertIn('name="retention_days"', history)
        self.assertIn('value="30"', history)
        self.assertIn('&lt;test&gt;', history)
        self.assertNotIn('<test>', history)
        self.assertIn('name="searxng_seconds"', upstream)
        self.assertIn('value="25"', upstream)
        self.assertIn('Apply upstream timeouts', upstream)
        self.assertIn(' disabled>Apply upstream timeouts', upstream)
        self.assertIn('environment override', upstream)

    def test_history_and_provider_components_are_template_backed(self):
        navigation = admin_ui.history_pagination_html(
            {"kind": "search", "status": "", "domain": "<unsafe>",
             "hours": 24, "show_queries": True}, 1, 26)
        self.assertIn("Next 25", navigation)
        self.assertIn("%3Cunsafe%3E", navigation)
        self.assertNotIn("<unsafe>", navigation)
        rows = admin_ui.history_summary_html([
            {"kind": "<script>", "status": "success", "count": 3,
             "avg_duration_ms": 1.5}])
        self.assertIn("&lt;script&gt;", rows)
        self.assertNotIn("<script>", rows)
        self.assertIn('value="3" max="3"', rows)
        self.assertIn("No matching events.", admin_ui.history_summary_html([]))
        self.assertIn("No matching MCP history.",
                      admin_ui.history_items_html(items=[]))

    def test_unknown_template_is_rejected(self):
        for name in ("../secrets", "/etc/passwd", "x", "policy.html"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                render_template(name)

    def test_operator_fields_remain_escaped_in_policy_html(self):
        with (patch.object(admin_ui.policy, "counts",
                           return_value={"pending": 0, "allowed": 0, "denied": 0}),
              patch.object(admin_ui.policy, "entries", return_value=[])):
            page = admin_ui.render(query='<script>alert(1)</script>',
                                   message='<img src=x onerror=alert(1)>')
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        self.assertNotIn('<img src=x onerror=alert(1)>', page)
        self.assertIn('value="&lt;script&gt;alert(1)&lt;/script&gt;"', page)
        self.assertIn('name="csrf"', page)

    def test_24h_operational_cards_use_only_aggregates(self):
        metrics = [
            {"kind": "search", "status": "success", "count": 7,
             "avg_duration_ms": 350},
            {"kind": "fetch", "status": "success", "count": 4,
             "avg_duration_ms": 1200},
            {"kind": "fetch", "status": "http_error", "count": 2,
             "avg_duration_ms": 800},
            {"kind": "page", "status": "expired_or_unknown", "count": 1,
             "avg_duration_ms": 0},
            {"kind": "<secret-host>", "status": "Bearer SECRET",
             "count": 1, "avg_duration_ms": 0},
        ]
        rendered = admin_ui.operations_overview_html(metrics)
        for part in ("Operations</span><b>15</b>",
                     "Searches</span><b>7</b>",
                     "Fetches</span><b>6</b>",
                     "Non-success outcomes</span><b>4</b>"):
            self.assertIn(part, rendered)
        self.assertNotIn("secret-host", rendered)
        self.assertNotIn("Bearer", rendered)
        unavailable = admin_ui.operations_overview_html(None)
        self.assertIn("history is unavailable", unavailable)
        self.assertIn("Operational history unavailable",
                      admin_ui.overview_summary_html(None))
        self.assertNotIn("<b>0</b>", unavailable)
        self.assertIn("Operations</span><b>0</b>",
                      admin_ui.operations_overview_html([]))

    def test_overview_metric_refresh_is_explicit_and_leaves_default_poll_cheap(self):
        metrics = [{"kind": "search", "status": "success", "count": 3,
                    "avg_duration_ms": 100}]
        with (patch.object(admin_ui.policy, "entries", return_value=[]),
              patch.object(admin_ui.policy, "counts",
                           return_value={"pending": 0, "allowed": 0, "denied": 0}),
              patch.object(admin_ui, "history_items_html",
                           return_value="<tr><td>safe</td></tr>"),
              patch.object(admin_ui, "service_overview_rows",
                           return_value="<tr><td>ok</td></tr>"),
              patch.object(admin_ui, "_overview_history_metrics",
                           return_value=metrics) as overview):
            ordinary = admin_ui.live_snapshot()
            overview.assert_not_called()
            self.assertNotIn("operations_html", ordinary)
            self.assertNotIn("activity_summary_html", ordinary)
            detailed = admin_ui.live_snapshot(include_overview=True)
            overview.assert_called_once_with()
        self.assertIn("Searches</span><b>3</b>", detailed["operations_html"])
        self.assertIn("search", detailed["activity_summary_html"])
        self.assertNotIn("domain", detailed["operations_html"])

    def test_new_domain_rule_requires_explicit_duration_selection(self):
        with (patch.object(admin_ui.policy, "counts",
                           return_value={"pending": 0, "allowed": 0, "denied": 0}),
              patch.object(admin_ui.policy, "entries", return_value=[])):
            page = admin_ui.render()
        marker = '<h2 style="margin-top:0">Add domain</h2>'
        new_domain = page.split(marker, 1)[1].split('</form>', 1)[0]
        self.assertIn('name="expires_minutes" value="60" required', new_domain)
        self.assertNotIn('name="expires_minutes" value="60" checked', new_domain)

    def test_bulk_shortcuts_are_opt_in_and_leave_the_bulk_post_explicit(self):
        with (patch.object(admin_ui.policy, "counts",
                           return_value={"pending": 0, "allowed": 0, "denied": 0}),
              patch.object(admin_ui.policy, "entries", return_value=[])):
            page = admin_ui.render()
        self.assertIn('id="bulk-selection-tools"', page)
        self.assertIn('data-max="25" hidden', page)
        self.assertIn('id="bulk-select-visible" type="button"', page)
        self.assertIn('id="bulk-clear-selection" type="button"', page)
        self.assertIn('id="bulk-selection-count" role="status" aria-live="polite"', page)
        self.assertIn('id="bulk-form" method="post" action="/bulk"', page)
        self.assertIn('name="csrf"', page)
        self.assertIn('name="action" value="allow"', page)
        self.assertIn('name="action" value="deny"', page)
        self.assertIn('name="scope" value="exact" checked required', page)
        self.assertIn('name="expires_minutes" value="60" checked required', page)
        self.assertNotIn('<script>', page)

    def test_pending_row_has_bounded_one_click_decisions_and_advanced_options(self):
        row = {
            "domain": "example.org", "status": "pending", "request_count": 3,
            "request_id": "a" * 24, "first_requested_at": "2026-10-30T08:00:00Z",
            "last_requested_at": "2026-10-30T08:01:00Z",
            "last_requested_url": "https://example.org/private?do-not-show=1",
        }
        markup = admin_ui.row_html(row)
        self.assertEqual(markup.count('class="quick-decision"'), 1)
        decision = markup.split('class="quick-decision"', 1)[1].split('</form>', 1)[0]
        self.assertIn('name="csrf"', decision)
        self.assertIn('name="domain" value="example.org"', decision)
        self.assertIn('name="scope" value="exact" checked required', decision)
        self.assertIn('name="expires_minutes" value="60" checked required', decision)
        self.assertIn('name="expires_minutes" value="0" required', decision)
        self.assertIn('name="action" value="allow"', decision)
        self.assertIn('name="action" value="deny"', decision)
        self.assertLess(decision.index('value="60" checked required'),
                        decision.index('value="0" required'))
        self.assertIn('name="request_id" value="' + "a" * 24, markup)
        self.assertIn('form="bulk-form"', markup)
        self.assertIn('summary>More actions</summary>', markup)
        self.assertNotIn('do-not-show', markup)

    def test_decided_row_keeps_rule_edit_controls_but_not_quick_grants(self):
        row = {"domain": "example.org", "status": "allowed",
               "request_count": 0, "explicit_rule": True,
               "match_scope": "exact", "expires_at": ""}
        markup = admin_ui.row_html(row)
        self.assertNotIn('class="quick-decision"', markup)
        self.assertNotIn('name="action" value="stage"', markup)
        self.assertIn('name="action" value="rename"', markup)
        self.assertIn('name="action" value="delete"', markup)
        self.assertIn('summary>More actions</summary>', markup)

    def test_workspace_embeds_privacy_safe_history_and_read_only_services(self):
        history = [{"at_ms": 1_700_000_000_000, "duration_ms": 15,
                    "kind": "search", "status": "success",
                    "domain": "example.org", "query": "PRIVATE SEARCH"}]
        with (patch.object(admin_ui.policy, "counts",
                           return_value={"pending": 0, "allowed": 0, "denied": 0}),
              patch.object(admin_ui.policy, "entries", return_value=[]),
              patch.object(admin_ui.history_index, "search_history",
                           return_value=history) as history_read,
              patch.object(admin_ui.history_index, "summarize_history",
                           return_value=[]) as summary,
              patch.object(admin_ui.service_status, "probe_services",
                           return_value=[{"service": "SearXNG",
                                          "status": "TCP unavailable <secret>",
                                          "source": "default"}]) as probes):
            page = admin_ui.render()
        self.assertIn('id="live-activity"', page)
        self.assertIn('id="live-activity-summary"', page)
        self.assertIn("refreshes about once a minute", page)
        self.assertNotIn("page-load snapshot", page)
        self.assertIn('id="live-count-pending"', page)
        self.assertIn('id="bulk-form"', page)
        self.assertLess(page.index('id="bulk-form"'),
                        page.index('id="live-pending"'))
        self.assertIn('id="configuration"', page)
        self.assertIn('href="/history"', page)
        self.assertIn('href="/status"', page)
        self.assertNotIn("PRIVATE SEARCH", page)
        self.assertIn('TCP unavailable &lt;secret&gt;', page)
        self.assertNotIn('TCP unavailable <secret>', page)
        self.assertEqual(history_read.call_args.kwargs["reveal_queries"], False)
        self.assertEqual(summary.call_args.kwargs["hours"], 24)
        probes.assert_called_once()

    def test_live_workspace_activity_cannot_reveal_stored_query(self):
        history = [{"at_ms": 1_700_000_000_000, "duration_ms": 15,
                    "kind": "search", "status": "success",
                    "domain": "example.org", "query": "PRIVATE SEARCH"}]
        with (patch.object(admin_ui.policy, "entries", return_value=[]),
              patch.object(admin_ui.policy, "counts",
                           return_value={"pending": 0, "allowed": 0, "denied": 0}),
              patch.object(admin_ui.history_index, "search_history",
                           return_value=history) as history_read,
              patch.object(admin_ui.history_index, "summarize_history",
                           return_value=[]),
              patch.object(admin_ui.policy, "recent_decisions",
                           return_value=[])):
            snapshot = admin_ui.live_snapshot(view="policy")
        self.assertIn("example.org", snapshot["activity_html"])
        self.assertNotIn("PRIVATE SEARCH", snapshot["activity_html"])
        self.assertNotIn("activity_summary_html", snapshot)
        self.assertIn("service_rows_html", snapshot)
        self.assertEqual(history_read.call_args.kwargs["reveal_queries"], False)

    def test_history_filters_remain_html_escaped(self):
        with (patch.object(admin_ui.history_index, "dashboard_history_snapshot",
                           return_value={"metrics": [], "rows": [],
                                         "total": 0, "page": 1}),
              patch.object(admin_ui.history_index, "_retention_days",
                           return_value=30),
              patch.object(admin_ui, "recent_rows", return_value="safe rows")):
            page = admin_ui.history_page(
                kind="search", status='<img src=x onerror=alert(1)>',
                domain='<script>alert(1)</script>')
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        self.assertNotIn('<img src=x onerror=alert(1)>', page)
        self.assertIn("safe rows", page)
        self.assertIn('name="kind" value="search" checked required', page)
        self.assertIn('src="/dashboard-live.js"', page)


    def test_cache_settings_template_preserves_escaping_and_locked_form(self):
        payload = "<img src=x onerror=alert(1)>"
        values = {"search_seconds": 45, "fetch_seconds": 120}
        data = {"values": values, "state": "a" * 64, "editable": True,
                "environment_override": {"search_seconds": False,
                                         "fetch_seconds": False}}
        with patch.object(admin_ui.cache_settings, "dashboard_settings",
                          return_value=data):
            page = admin_ui.cache_settings_page(
                message=payload, kind="error",
                submitted={"state": payload, "search_seconds": payload,
                           "fetch_seconds": payload})
        self.assertNotIn(payload, page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertIn('name="csrf"', page)
        self.assertIn('action="/cache-settings"', page)
        self.assertIn('pattern="APPLY"', page)
        self.assertIn('name="state" value="&lt;img', page)
        self.assertNotIn('<script', page)
        self.assertNotIn('type="submit" disabled', page)

        with patch.object(admin_ui.cache_settings, "dashboard_settings",
                          return_value={**data, "editable": False,
                                        "environment_override": {
                                            "search_seconds": True,
                                            "fetch_seconds": False}}):
            page = admin_ui.cache_settings_page()
        self.assertIn('type="submit" disabled', page)
        self.assertIn("An environment override is present", page)
        self.assertIn('value="45"', page)
        self.assertIn('value="120"', page)
        with self.assertRaises(KeyError):
            render_template("cache-settings", alert="missing context")


if __name__ == "__main__":
    unittest.main()
