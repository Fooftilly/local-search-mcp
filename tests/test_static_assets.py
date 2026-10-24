"""Regression tests for the fixed, same-origin browser assets."""
from http.client import HTTPConnection
import threading
import unittest
from unittest.mock import patch

import admin_ui
from dashboard_live_script import LIVE_SCRIPT
from web_assets import STYLESHEET


class StaticBrowserAssetsTests(unittest.TestCase):
    def setUp(self):
        self.server = admin_ui.ThreadingHTTPServer(
            ("127.0.0.1", 0), admin_ui.Handler)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_address[1]
        override = patch.object(admin_ui, "PORT", self.port)
        override.start()
        self.addCleanup(override.stop)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)

    def request(self, path, headers=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            c.request("GET", path, headers=headers or {})
            resp = c.getresponse()
            return resp.status, resp.read(), dict(resp.getheaders())
        finally:
            c.close()

    def test_stylesheet_and_js_are_first_party_and_static(self):
        status, html, headers = self.request("/status")
        self.assertEqual(status, 200)
        self.assertIn(b'href="/dashboard.css"', html)
        self.assertNotIn(b"<style>", html)
        self.assertIn("style-src 'self'", headers["Content-Security-Policy"])
        code, css, metadata = self.request("/dashboard.css")
        self.assertEqual(code, 200)
        self.assertEqual(css, STYLESHEET.encode("utf-8"))
        self.assertEqual(metadata["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(metadata["X-Content-Type-Options"], "nosniff")
        self.assertEqual(metadata["Cache-Control"], "no-store")
        code, script, metadata = self.request("/dashboard-live.js")
        self.assertEqual(code, 200)
        self.assertEqual(script, LIVE_SCRIPT.encode("utf-8"))
        self.assertEqual(metadata["Content-Type"], "text/javascript; charset=utf-8")

    def test_assets_require_local_host_and_same_origin(self):
        for path in ("/dashboard.css", "/dashboard-live.js"):
            with self.subTest(path=path):
                for headers in (
                    {"Host": "evil.invalid"},
                    {"Origin": "http://evil.invalid"},
                    {"Sec-Fetch-Site": "cross-site"},
                    {"Referer": "http://evil.invalid/test"},
                ):
                    self.assertEqual(self.request(path, headers)[0], 403)
                self.assertEqual(self.request(path + "/private")[0], 404)

    def test_policy_and_history_html_reference_external_assets(self):
        self.assertIn('href="/dashboard.css"', admin_ui.render())
        self.assertIn('src="/dashboard-live.js"', admin_ui.render())
        page = admin_ui.history_page()
        self.assertIn('href="/dashboard.css"', page)
        self.assertIn('src="/dashboard-live.js"', page)
        self.assertNotIn("<style>", page)


if __name__ == "__main__":
    unittest.main()
