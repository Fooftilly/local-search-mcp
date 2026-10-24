"""Exercise portable transfer over the actual loopback dashboard HTTP boundary."""
from __future__ import annotations

from http.client import HTTPConnection
import json
import os
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

import admin_ui
import cache_settings


class PortableDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0), admin_ui.Handler)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        changed = patch.object(admin_ui, "PORT", self.port)
        changed.start()
        self.addCleanup(changed.stop)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, fields=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
            data = urlencode(fields) if fields is not None else None
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            return r.status, r.read().decode(), dict(r.getheaders())
        finally:
            conn.close()

    def test_export_and_confirmed_round_trip(self):
        code, html, headers = self.request("GET", "/portable-settings")
        self.assertEqual(code, 200)
        self.assertIn("Validate and preview", html)
        self.assertEqual(headers["Cache-Control"], "no-store")
        code, data, hdr = self.request("GET",
                                      "/portable-settings/export?profile=cache")
        self.assertEqual(code, 200)
        self.assertIn("nonsecret-settings-v1.json", hdr["Content-Disposition"])
        doc = json.loads(data)
        self.assertEqual(doc["profile"], "cache")
        doc["settings"]["search_seconds"] = 90
        text = json.dumps(doc)
        code, preview, _ = self.request(
            "POST", "/portable-settings/preview",
            {"csrf": admin_ui.CSRF, "settings_doc": text})
        self.assertEqual(code, 200)
        self.assertIn("Current", preview)
        self.assertEqual(cache_settings.dashboard_settings()["values"]["search_seconds"], 45)
        state = cache_settings.dashboard_settings()["state"]
        code, applied, _ = self.request(
            "POST", "/portable-settings/apply",
            {"csrf": admin_ui.CSRF, "settings_doc": text,
             "state": state, "confirm": "IMPORT"})
        self.assertEqual(code, 200, applied)
        self.assertEqual(cache_settings.dashboard_settings()["values"]["search_seconds"], 90)

    def test_post_security_boundaries_fail_without_change(self):
        document = json.loads(self.request(
            "GET", "/portable-settings/export?profile=cache")[1])
        document["settings"]["search_seconds"] = 100
        text = json.dumps(document)
        state = cache_settings.dashboard_settings()["state"]
        base = {"csrf": admin_ui.CSRF, "settings_doc": text,
                "state": state, "confirm": "IMPORT"}
        attacks = [
            ({**base, "csrf": "invalid"}, {}),
            (base, {"Origin": "http://evil.invalid"}),
            (base, {"Sec-Fetch-Site": "cross-site"}),
            (base, {"Host": "evil.invalid"}),
        ]
        for fields, headers in attacks:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/portable-settings/apply",
                                              fields, headers)[0], 403)
        self.assertEqual(cache_settings.dashboard_settings()["values"]["search_seconds"], 45)
        self.assertEqual(self.request("GET", "/portable-settings/apply")[0], 404)
        self.assertEqual(self.request("POST", "/portable-settings/apply",
                                      {**base, "confirm": ""})[0], 400)
        self.assertEqual(cache_settings.dashboard_settings()["values"]["search_seconds"], 45)

    def test_filesystem_failures_return_redacted_settings_page(self):
        with patch.object(cache_settings, "dashboard_settings",
                          side_effect=PermissionError("/private/SECRET_DIR")):
            doc = json.dumps({"schema": "local-search-mcp/portable-settings/v1",
                              "profile": "cache",
                              "settings": {"search_seconds": 45, "fetch_seconds": 120}})
            code, page, _ = self.request(
                "POST", "/portable-settings/preview",
                {"csrf": admin_ui.CSRF, "settings_doc": doc})
        self.assertEqual(code, 400)
        self.assertIn("Portable nonsecret settings", page)
        self.assertIn("Cannot safely read or save", page)
        self.assertNotIn("SECRET_DIR", page)
        with patch.object(cache_settings, "update_settings",
                          side_effect=PermissionError("/private/SECRET_FILE")):
            code, page, _ = self.request(
                "POST", "/portable-settings/apply",
                {"csrf": admin_ui.CSRF, "settings_doc": doc,
                 "state": cache_settings.dashboard_settings()["state"],
                 "confirm": "IMPORT"})
        self.assertEqual(code, 400)
        self.assertIn("Portable nonsecret settings", page)
        self.assertNotIn("SECRET_FILE", page)

    def test_rejected_document_remains_on_settings_transfer_page(self):
        code, page, _ = self.request(
            "POST", "/portable-settings/preview",
            {"csrf": admin_ui.CSRF, "settings_doc": '{"bad":true}'})
        self.assertEqual(code, 400)
        self.assertIn("Portable nonsecret settings", page)
        self.assertNotIn("Domain approvals</h1>", page)

    def test_forged_export_and_remote_origin_rejected(self):
        self.assertEqual(self.request("GET",
                         "/portable-settings/export?profile=config.json")[0], 400)
        self.assertEqual(self.request("GET",
                         "/portable-settings/export?profile=cache",
                         headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("GET",
                         "/portable-settings/export?profile=cache&profile=history")[0], 400)


if __name__ == "__main__":
    unittest.main()
