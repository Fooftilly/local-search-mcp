"""Service status HTML is a fixed, inert template with sanitized values."""
from pathlib import Path
import unittest
from unittest.mock import patch

import admin_ui
import service_status
from web_templates import render_template


class StatusTemplateTests(unittest.TestCase):
    def test_status_template_contains_no_executable_markup_or_handlers(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "web" / "templates" / "status.html").read_text()
        self.assertIn("${rows}", text)
        self.assertIn("${ports}", text)
        self.assertIn("${limits}", text)
        self.assertIn("${mcp_port}", text)
        self.assertIn('href="/dashboard.css"', text)
        self.assertNotIn("<style", text)
        self.assertNotIn("<script", text)
        self.assertNotIn('onclick=', text)

    def test_status_page_escapes_all_dynamic_fields_and_probes_once(self):
        raw = "<img src=x onerror=alert(1)>"
        services = [{"service": raw, "status": raw, "source": raw}]
        ports = [{"service": raw, "port": 8888, "valid": True,
                  "source": raw, "apply": raw},
                 {"service": "Crawl4AI", "port": None, "valid": False,
                  "source": "environment", "apply": "restart required"}]
        limits = [{"name": raw, "value": raw, "apply": raw}]
        with (patch.object(service_status, "probe_services",
                           return_value=services) as probe,
              patch.object(service_status, "configured_ports",
                           return_value=ports) as configured,
              patch.object(service_status, "fixed_safety_limits",
                           return_value=limits) as fixed):
            page = admin_ui.status_page()
        probe.assert_called_once_with()
        configured.assert_called_once_with()
        fixed.assert_called_once_with()
        self.assertNotIn(raw, page)
        self.assertEqual(page.count("&lt;img src=x onerror=alert(1)&gt;"), 9)
        self.assertIn("Invalid (check local configuration)", page)
        self.assertIn('<td>8888</td>', page)
        self.assertIn('href="/dashboard.css"', page)
        self.assertIn("Custom backend or dashboard ports", page)
        self.assertIn("Fixed safety limits (read-only)", page)
        self.assertIn("History retention and rotation size", page)
        self.assertIn("Port changes must instead be coordinated", page)

    def test_mcp_port_in_rows_and_explanation_comes_from_same_constant(self):
        with (patch.object(admin_ui, "MCP_PORT", 9876),
              patch.object(service_status, "probe_services", return_value=[])):
            page = admin_ui.status_page()
        self.assertIn("<td>MCP</td><td>9876</td>", page)
        self.assertIn("fixed MCP listener port (9876)", page)
        self.assertNotIn("<td>MCP</td><td>8790</td>", page)

    def test_unknown_template_and_unresolved_placeholder_fail_closed(self):
        with self.assertRaises(ValueError):
            render_template("../status")
        with self.assertRaises(KeyError):
            render_template("status", rows="present")


if __name__ == "__main__":
    unittest.main()
