"""The HTML templates are fixed assets, never request-controlled paths."""
from pathlib import Path
import unittest
from unittest.mock import patch

import admin_ui
from web_templates import render_template


class DashboardTemplateTests(unittest.TestCase):
    def test_fixed_templates_exist_without_inline_css_or_javascript(self):
        folder = Path(__file__).resolve().parents[1] / "web" / "templates"
        for name in ("policy", "history", "status"):
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


if __name__ == "__main__":
    unittest.main()
