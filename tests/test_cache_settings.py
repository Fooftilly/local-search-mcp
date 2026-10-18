"""Private cache lifetime settings: validation, stale state and browser boundary."""
import json
from http.client import HTTPConnection
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import admin_ui
import bridge
import cache_settings


class CacheSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def fields(self, search="30", fetch="180"):
        return {"search_seconds": search, "fetch_seconds": fetch}

    def test_atomic_save_live_reads_and_bridge_integration(self):
        before = cache_settings.dashboard_settings()
        self.assertEqual(before["values"], {"search_seconds": 45, "fetch_seconds": 120})
        cache_settings.update_settings(self.fields(), before["state"])
        file = self.root / "local-search-mcp" / "cache-ttl.json"
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(file.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(file.read_text())["settings"], {
            "search_seconds": 30, "fetch_seconds": 180})
        self.assertEqual(cache_settings.effective_ttl("search_seconds"), 30)
        self.assertEqual(cache_settings.effective_ttl("fetch_seconds"), 180)
        self.assertFalse((file.parent / "config.json").exists())
        with patch.object(bridge.request_cache, "run", return_value=[]) as cached:
            bridge.web_search("cache policy test")
            self.assertEqual(cached.call_args.kwargs["ttl"], 30)

    def test_rejects_unsafe_invalid_and_stale_values(self):
        state = cache_settings.dashboard_settings()["state"]
        cases = [self.fields("0"), self.fields("901"), self.fields(fetch="4"),
                 self.fields(fetch="1801"), self.fields("-1"), self.fields("1.1"),
                 {**self.fields(), "secret": "x"}, {"search_seconds": "30"}]
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                cache_settings.update_settings(candidate, state)
        cache_settings.update_settings(self.fields(), state)
        with self.assertRaisesRegex(ValueError, "Settings changed"):
            cache_settings.update_settings(self.fields("40"), state)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL": "12"}):
            self.assertFalse(cache_settings.dashboard_settings()["editable"])
            self.assertEqual(cache_settings.effective_ttl("search_seconds"), 12)
            with self.assertRaises(ValueError):
                cache_settings.update_settings(
                    self.fields(), cache_settings.dashboard_settings()["state"])
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_CACHE_FETCH_TTL": "10000"}):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(cache_settings.effective_ttl("fetch_seconds"), 120)

    def test_corruption_and_symlinks_are_not_overwritten(self):
        folder = self.root / "local-search-mcp"
        folder.mkdir(mode=0o700)
        file = folder / "cache-ttl.json"
        file.write_text("not-json")
        file.chmod(0o600)
        with self.assertRaises(ValueError):
            cache_settings.dashboard_settings()
        with self.assertRaises(ValueError):
            cache_settings.update_settings(self.fields(), "0" * 64)
        self.assertEqual(file.read_text(), "not-json")
        file.unlink()
        external = self.root / "untouched"
        external.write_text("untouched")
        file.symlink_to(external)
        with self.assertRaises((OSError, ValueError)):
            cache_settings.dashboard_settings()
        self.assertEqual(external.read_text(), "untouched")


class CacheSettingsHTTPTests(CacheSettingsTests):
    def setUp(self):
        super().setUp()
        server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0), admin_ui.Handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.port = server.server_address[1]
        mock_port = patch.object(admin_ui, "PORT", self.port)
        mock_port.start()
        self.addCleanup(mock_port.stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

    def request(self, method, path, data=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            body = urlencode(data) if data is not None else None
            connection.request(method, path, body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_browser_security_and_stale_form(self):
        status, page = self.request("GET", "/cache-settings")
        self.assertEqual(status, 200)
        state = re.search(r'name="state" value="([0-9a-f]{64})"', page).group(1)
        form = {**self.fields(), "csrf": admin_ui.CSRF, "state": state, "confirm": "APPLY"}
        self.assertEqual(self.request("POST", "/cache-settings", form)[0], 200)
        self.assertEqual(self.request("POST", "/cache-settings", form)[0], 400)
        self.assertEqual(self.request("POST", "/cache-settings",
                         {**form, "csrf": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/cache-settings",
                         {**form, "confirm": "apply"})[0], 400)
        for headers in ({"Host": "bad.invalid"}, {"Origin": "http://bad.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("GET", "/cache-settings", headers=headers)[0], 403)
            self.assertEqual(self.request("POST", "/cache-settings", form,
                             headers=headers)[0], 403)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL": "12"}):
            self.assertIn("disabled", self.request("GET", "/cache-settings")[1])
            form["state"] = cache_settings.dashboard_settings()["state"]
            self.assertEqual(self.request("POST", "/cache-settings", form)[0], 400)


if __name__ == "__main__":
    unittest.main()
