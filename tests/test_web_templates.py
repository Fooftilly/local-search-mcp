"""The HTML templates are fixed assets, never request-controlled paths."""
from pathlib import Path
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

    def test_new_domain_rule_requires_explicit_duration_selection(self):
        with (patch.object(admin_ui.policy, "counts",
                           return_value={"pending": 0, "allowed": 0, "denied": 0}),
              patch.object(admin_ui.policy, "entries", return_value=[])):
            page = admin_ui.render()
        marker = '<h2 style="margin-top:0">Add domain</h2>'
        new_domain = page.split(marker, 1)[1].split('</form>', 1)[0]
        self.assertIn('<select name="expires_minutes" required>', new_domain)
        self.assertIn('<option value="" selected disabled>Choose duration</option>', new_domain)

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
        self.assertIn('<option value="exact" selected>', decision)
        self.assertIn('<option value="60" selected>', decision)
        self.assertIn('<option value="0">Permanent', decision)
        self.assertIn('name="action" value="allow"', decision)
        self.assertIn('name="action" value="deny"', decision)
        self.assertLess(decision.index('value="60" selected'),
                        decision.index('value="0">Permanent'))
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
        self.assertIn("activity_summary_html", snapshot)
        self.assertEqual(history_read.call_args.kwargs["reveal_queries"], False)

    def test_history_filters_remain_html_escaped(self):
        with (patch.object(admin_ui.history_index, "summarize_history",
                           return_value=[]),
              patch.object(admin_ui.history_index, "_retention_days",
                           return_value=30),
              patch.object(admin_ui, "recent_rows", return_value="safe rows")):
            page = admin_ui.history_page(
                kind="search", status='<img src=x onerror=alert(1)>',
                domain='<script>alert(1)</script>')
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        self.assertNotIn('<img src=x onerror=alert(1)>', page)
        self.assertIn("safe rows", page)
        self.assertIn('value="search" selected', page)
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
