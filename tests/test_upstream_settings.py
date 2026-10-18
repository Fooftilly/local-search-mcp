"""Secure persistent upstream timeout settings and loopback HTTP regressions."""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import admin_ui
import upstream_settings


class UpstreamTimeoutSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def fields(self, search="9", crawl="100"):
        return {"searxng_seconds": search, "crawl_seconds": crawl}

    def config_path(self):
        return self.root / "local-search-mcp" / upstream_settings.FILENAME

    def test_defaults_and_next_operation_without_restart(self):
        before = upstream_settings.dashboard_settings()
        self.assertEqual(before["values"], {"searxng_seconds": 25, "crawl_seconds": 75})
        self.assertTrue(before["editable"])
        upstream_settings.update_settings(self.fields(), before["state"])
        saved = self.config_path()
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(saved.read_text())["schema"], upstream_settings.SCHEMA)
        self.assertEqual(json.loads(saved.read_text())["settings"],
                         {"searxng_seconds": 9, "crawl_seconds": 100})
        self.assertEqual(upstream_settings.effective_timeout("searxng_seconds"), 9)
        self.assertEqual(upstream_settings.effective_timeout("crawl_seconds"), 100)
        self.assertFalse((saved.parent / "history-settings.json").exists())
        self.assertFalse((saved.parent / "config.json").exists())
        self.assertNotIn("TOKEN", saved.read_text())

    def test_rejects_invalid_fields_stale_writes_and_env_override(self):
        state = upstream_settings.dashboard_settings()["state"]
        for values in (self.fields("4"), self.fields("121"),
                       self.fields(crawl="14"), self.fields(crawl="301"),
                       self.fields("-1"), self.fields("10.5"),
                       self.fields(crawl="nan"), self.fields("true"),
                       {**self.fields(), "token": "private"},
                       {"searxng_seconds": "9"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                upstream_settings.update_settings(values, state)
        self.assertEqual(upstream_settings.dashboard_settings()["state"], state)
        upstream_settings.update_settings(self.fields(), state)
        with self.assertRaisesRegex(ValueError, "Settings changed"):
            upstream_settings.update_settings(self.fields("10"), state)
        self.assertEqual(upstream_settings.effective_timeout("searxng_seconds"), 9)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_SEARXNG_TIMEOUT": "31"}):
            self.assertFalse(upstream_settings.dashboard_settings()["editable"])
            self.assertEqual(upstream_settings.effective_timeout("searxng_seconds"), 31)
            with self.assertRaisesRegex(ValueError, "Environment overrides"):
                upstream_settings.update_settings(
                    self.fields(), upstream_settings.dashboard_settings()["state"])
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_CRAWL_TIMEOUT": "1000"}):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(upstream_settings.effective_timeout("crawl_seconds"), 75)

    def test_corruption_permissions_symlink_cannot_replace_or_disable_requests(self):
        path = self.config_path()
        path.parent.mkdir(mode=0o700)
        path.write_text("not-json")
        path.chmod(0o600)
        with self.assertRaises(ValueError):
            upstream_settings.dashboard_settings()
        with self.assertRaises(ValueError):
            upstream_settings.update_settings(self.fields(), "0" * 64)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(upstream_settings.effective_timeout("searxng_seconds"), 25)
        self.assertEqual(path.read_text(), "not-json")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            upstream_settings.dashboard_settings()
        with self.assertLogs(level="WARNING"):
            self.assertEqual(upstream_settings.effective_timeout("crawl_seconds"), 75)
        path.unlink()
        target = self.root / "outside"
        target.write_text("do not modify")
        path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            upstream_settings.dashboard_settings()
        with self.assertRaises((ValueError, OSError)):
            upstream_settings.update_settings(self.fields(), "0" * 64)
        self.assertEqual(target.read_text(), "do not modify")
        path.unlink()
        path.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            upstream_settings.dashboard_settings()

    def test_concurrent_cas_allows_exactly_one_update(self):
        start = upstream_settings.dashboard_settings()["state"]
        results = []
        barrier = threading.Barrier(3)

        def save(value):
            barrier.wait()
            try:
                upstream_settings.update_settings(self.fields(value), start)
                results.append("saved")
            except ValueError:
                results.append("stale")
        jobs = [threading.Thread(target=save, args=(value,)) for value in ("12", "13")]
        for job in jobs:
            job.start()
        barrier.wait()
        for job in jobs:
            job.join(timeout=5)
            self.assertFalse(job.is_alive())
        self.assertCountEqual(results, ["saved", "stale"])
        self.assertIn(upstream_settings.effective_timeout("searxng_seconds"), (12, 13))


class UpstreamTimeoutHTTPTests(UpstreamTimeoutSettingsTests):
    def setUp(self):
        super().setUp()
        server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0), admin_ui.Handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.port = server.server_address[1]
        patch_port = patch.object(admin_ui, "PORT", self.port)
        patch_port.start()
        self.addCleanup(patch_port.stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

    def request(self, method, path, data=None, headers=None):
        client = HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            body = urlencode(data) if data is not None else None
            client.request(method, path, body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
            response = client.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            client.close()

    def test_get_post_stale_and_rejected_form_stay_on_timeout_page(self):
        status, page = self.request("GET", "/upstream-settings")
        self.assertEqual(status, 200)
        self.assertIn("Upstream request timeouts", page)
        match = re.search(r'name="state" value="([a-f0-9]{64})"', page)
        self.assertIsNotNone(match)
        form = {"csrf": admin_ui.CSRF, "state": match.group(1),
                **self.fields(), "confirm": "APPLY"}
        status, page = self.request("POST", "/upstream-settings", data=form)
        self.assertEqual(status, 200)
        self.assertIn("timeouts saved", page)
        self.assertEqual(upstream_settings.effective_timeout("searxng_seconds"), 9)
        status, page = self.request("POST", "/upstream-settings", data=form)
        self.assertEqual(status, 400)
        self.assertIn("Settings changed", page)
        self.assertIn(f'name="state" value="{form["state"]}"', page)
        status, page = self.request("POST", "/upstream-settings", data={
            **form, "confirm": "apply", "searxng_seconds": "11"})
        self.assertEqual(status, 400)
        self.assertIn("Type APPLY", page)
        self.assertIn('value="11"', page)
        status, page = self.request("POST", "/upstream-settings", data={
            **form, "state": upstream_settings.dashboard_settings()["state"],
            "crawl_seconds": "301"})
        self.assertEqual(status, 400)
        self.assertIn("crawl_seconds must be between", page)
        self.assertIn('value="301"', page)
        self.assertEqual(upstream_settings.effective_timeout("crawl_seconds"), 100)
        self.assertEqual(self.request("POST", "/upstream-settings", data={
            **form, "csrf": "invalid"})[0], 403)

    def test_cross_origin_requests_and_invalid_file_fail_closed(self):
        for headers in ({"Host": "evil.invalid"},
                        {"Origin": "http://evil.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(
                    "GET", "/upstream-settings", headers=headers)[0], 403)
                self.assertEqual(self.request(
                    "POST", "/upstream-settings",
                    data={"csrf": admin_ui.CSRF}, headers=headers)[0], 403)
        settings = self.config_path()
        settings.parent.mkdir(mode=0o700)
        settings.write_text("not-json")
        settings.chmod(0o600)
        status, page = self.request("POST", "/upstream-settings", data={
            "csrf": admin_ui.CSRF, "state": "0" * 64,
            **self.fields(), "confirm": "APPLY"})
        self.assertEqual(status, 400)
        self.assertIn("Upstream settings unavailable", page)
        self.assertEqual(settings.read_text(), "not-json")

    def test_environment_override_disables_editing(self):
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_CRAWL_TIMEOUT": "99"}):
            status, page = self.request("GET", "/upstream-settings")
            self.assertEqual(status, 200)
            self.assertIn("disabled", page)
            self.assertIn("environment override", page)
            status, page = self.request("POST", "/upstream-settings", data={
                "csrf": admin_ui.CSRF,
                "state": upstream_settings.dashboard_settings()["state"],
                **self.fields(), "confirm": "APPLY"})
            self.assertEqual(status, 400)
            self.assertIn("Environment overrides are active", page)
        self.assertEqual(upstream_settings.dashboard_settings()["values"],
                         upstream_settings.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
