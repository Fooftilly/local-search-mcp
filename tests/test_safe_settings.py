"""Validated nonsecret settings and authenticated loopback HTTP regressions."""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
import re

import admin_ui
import history_index
import history_log
import safe_settings


class HistorySettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.tmp / "state" / "history.jsonl"),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def fields(self, retention="14", size="128000"):
        return {"retention_days": retention, "max_bytes": size}

    def test_defaults_secure_atomic_save_and_no_restart_for_effective_values(self):
        report = safe_settings.dashboard_settings()
        self.assertEqual(report["values"], safe_settings.DEFAULTS)
        self.assertTrue(report["editable"])
        self.assertEqual(history_index._retention_days(), 30)
        safe_settings.update_settings(self.fields(), report["state"])
        path = self.tmp / "config" / "local-search-mcp" / "history-settings.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        raw = path.read_text()
        self.assertEqual(set(json.loads(raw)["settings"]), set(safe_settings.DEFAULTS))
        self.assertNotIn("CRAWL4AI_API_TOKEN", raw)
        self.assertEqual(history_index._retention_days(), 14)
        self.assertEqual(safe_settings.effective_value("max_bytes"), 128000)
        self.assertEqual(safe_settings.dashboard_settings()["values"],
                         {"retention_days": 14, "max_bytes": 128000})
        self.assertFalse((path.parent / "config.json").exists())

    def test_invalid_and_stale_writes_never_change_current_settings(self):
        first = safe_settings.dashboard_settings()["state"]
        cases = [self.fields("0"), self.fields("366"), self.fields("4.0"),
                 self.fields("-1"), self.fields("a"), self.fields(size="63999"),
                 self.fields(size="100000001"), self.fields(size="42e3"),
                 {**self.fields(), "secret": "value"},
                 {"retention_days": "8"}]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                safe_settings.update_settings(fields, first)
        self.assertEqual(safe_settings.dashboard_settings()["state"], first)
        safe_settings.update_settings(self.fields(), first)
        with self.assertRaisesRegex(ValueError, "changed"):
            safe_settings.update_settings(self.fields("30"), first)
        self.assertEqual(safe_settings.effective_value("retention_days"), 14)

    def test_env_precedence_requires_unset_and_restart_before_editing(self):
        state = safe_settings.dashboard_settings()["state"]
        with patch.dict(os.environ, {
            "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "7",
        }):
            report = safe_settings.dashboard_settings()
            self.assertFalse(report["editable"])
            self.assertTrue(report["environment_override"]["retention_days"])
            self.assertEqual(history_index._retention_days(), 7)
            with self.assertRaisesRegex(ValueError, "Environment overrides"):
                safe_settings.update_settings(self.fields(), state)
        self.assertEqual(history_index._retention_days(), 30)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES": "123"}):
            self.assertEqual(safe_settings.effective_value("max_bytes"), 64000)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "0"}):
            with self.assertRaises(ValueError):
                history_index._retention_days()

    def test_unsafe_storage_and_malformed_data_are_never_overwritten(self):
        settings_dir = self.tmp / "config" / "local-search-mcp"
        settings_dir.mkdir(parents=True, mode=0o700)
        file = settings_dir / safe_settings.FILENAME
        file.write_text("not-json")
        file.chmod(0o600)
        with self.assertRaises(ValueError):
            safe_settings.dashboard_settings()
        with self.assertRaises(ValueError):
            safe_settings.update_settings(self.fields(), "0" * 64)
        self.assertEqual(file.read_text(), "not-json")
        file.unlink()
        outside = self.tmp / "outside"
        outside.write_text("untouched")
        file.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            safe_settings.dashboard_settings()
        self.assertEqual(outside.read_text(), "untouched")
        file.unlink()
        settings_dir.chmod(0o755)
        with self.assertRaises(ValueError):
            safe_settings.dashboard_settings()

    def test_old_jsonl_writer_picks_up_rotation_size_setting(self):
        target = self.tmp / "state" / "history.jsonl"
        rotated = target.with_name(target.name + ".1")
        first = {"kind": "search", "time_utc": "2026-10-18T01:00:00Z",
                 "query": "a" * 40_000}
        second = {**first, "query": "b" * 40_000}
        history_log.append_history(first)
        self.assertFalse(rotated.exists())
        state = safe_settings.dashboard_settings()["state"]
        safe_settings.update_settings(self.fields(size="64000"), state)
        self.assertEqual(safe_settings.effective_value("max_bytes"), 64000)
        # Two 40 KB events are below the old 8 MB default but exceed 64 KB.
        history_log.append_history(second)
        self.assertTrue(rotated.exists())
        self.assertEqual(json.loads(rotated.read_text())["query"], first["query"])
        self.assertEqual(json.loads(target.read_text())["query"], second["query"])

    def test_audit_writes_survive_invalid_nonsecret_settings(self):
        directory = self.tmp / "config" / "local-search-mcp"
        directory.mkdir(parents=True, mode=0o700)
        settings = directory / safe_settings.FILENAME
        settings.write_text("invalid-json")
        settings.chmod(0o600)
        for index, permission in enumerate((0o600, 0o644)):
            settings.chmod(permission)
            with self.subTest(permission=permission), self.assertLogs(level="WARNING") as logs:
                history_log.append_history({
                    "kind": "search", "time_utc": "2026-10-18T01:00:00Z",
                    "query": f"fallback-{index}",
                })
            self.assertIn("using default", " ".join(logs.output))
        target = self.tmp / "state" / "history.jsonl"
        self.assertEqual([json.loads(line)["query"] for line in target.read_text().splitlines()],
                         ["fallback-0", "fallback-1"])
        self.assertEqual(settings.read_text(), "invalid-json")


class HistorySettingsHTTPTests(HistorySettingsTests):
    def setUp(self):
        super().setUp()
        server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0),
                                               admin_ui.Handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.port = server.server_address[1]
        port = patch.object(admin_ui, "PORT", self.port)
        port.start()
        self.addCleanup(port.stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

    def request(self, method, path, *, data=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            body = urlencode(data) if data is not None else None
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  **(headers or {})})
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def test_settings_form_live_apply_and_secure_mutations(self):
        status, page = self.request("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn("Nonsecret history settings", page)
        self.assertIn("Apply history settings", page)
        state = re.search(r'name="state" value="([0-9a-f]{64})"', page)
        self.assertIsNotNone(state)
        form = {"csrf": admin_ui.CSRF, "state": state.group(1),
                "retention_days": "10", "max_bytes": "64000",
                "confirm": "APPLY"}
        self.assertEqual(self.request("POST", "/settings", data=form)[0], 200)
        self.assertEqual(history_index._retention_days(), 10)
        self.assertEqual(safe_settings.effective_value("max_bytes"), 64000)
        status, page = self.request("POST", "/settings", data=form)
        self.assertEqual(status, 400)
        self.assertIn("History settings", page)
        self.assertIn("Settings changed", page)
        # Preserve the stale CAS token so another click cannot silently
        # overwrite the intervening update; a refresh is required.
        self.assertIn(f'name="state" value="{form["state"]}"', page)
        self.assertNotIn("Domain approvals", page)
        status, page = self.request("POST", "/settings", data={
            **form, "confirm": "apply", "max_bytes": "65000"})
        self.assertEqual(status, 400)
        self.assertIn("Type APPLY", page)
        self.assertIn('value="65000"', page)
        self.assertEqual(self.request("POST", "/settings", data={
            **form, "csrf": "wrong"})[0], 403)
        status, page = self.request("POST", "/settings", data={
            **form, "state": safe_settings.dashboard_settings()["state"],
            "retention_days": "366"})
        self.assertEqual(status, 400)
        self.assertIn("History settings", page)
        self.assertIn("retention_days must be between", page)
        self.assertIn('value="366"', page)
        self.assertEqual(history_index._retention_days(), 10)

    def test_corrupt_settings_document_post_returns_safe_error(self):
        directory = self.tmp / "config" / "local-search-mcp"
        directory.mkdir(parents=True, mode=0o700)
        settings = directory / safe_settings.FILENAME
        settings.write_text("not-json")
        settings.chmod(0o600)
        status, page = self.request("POST", "/settings", data={
            "csrf": admin_ui.CSRF, "state": "0" * 64,
            "retention_days": "10", "max_bytes": "64000",
            "confirm": "APPLY"})
        self.assertEqual(status, 400)
        self.assertIn("Nonsecret settings unavailable", page)
        self.assertEqual(settings.read_text(), "not-json")

    def test_cross_origin_get_and_post_blocked(self):
        for headers in ({"Host": "evil.example"},
                        {"Origin": "http://evil.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/settings",
                                 headers=headers)[0], 403)
                self.assertEqual(self.request("POST", "/settings", data={
                    "csrf": admin_ui.CSRF}, headers=headers)[0], 403)
        self.assertEqual(safe_settings.dashboard_settings()["values"],
                         safe_settings.DEFAULTS)
        self.assertEqual(self.request("GET", "/settings/clear")[0], 404)

    def test_environment_override_disables_form(self):
        with patch.dict(os.environ, {
            "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS": "8",
        }):
            status, page = self.request("GET", "/settings")
            self.assertEqual(status, 200)
            self.assertIn("disabled", page)
            self.assertIn("environment override", page)
            state = safe_settings.dashboard_settings()["state"]
            status, page = self.request("POST", "/settings", data={
                "csrf": admin_ui.CSRF, "state": state,
                "retention_days": "10", "max_bytes": "64000",
                "confirm": "APPLY"})
            self.assertEqual(status, 400)
            self.assertIn("History settings", page)
            self.assertIn("Environment overrides are active", page)
        self.assertEqual(safe_settings.dashboard_settings()["values"],
                         safe_settings.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
