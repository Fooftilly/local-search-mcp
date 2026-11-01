"""Guarded nonsecret engine pacing editor, ownership, CSRF, and live policy."""
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
import engine_pacing
import engine_pacing_dashboard as editor


INVENTORY = [
    {"name": "duckduckgo", "enabled": True, "categories": ["general"]},
    {"name": "brave", "enabled": True, "categories": ["general"]},
    {"name": "qwant", "enabled": False, "categories": ["general"]},
    {"name": "google", "enabled": False, "categories": ["general"]},
]


class PacingEditorSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    @property
    def path(self):
        return self.root / "local-search-mcp" / engine_pacing.FILENAME

    def form(self, enabled=False, rules="duckduckgo=15", confirm=None, state=None):
        return {"csrf": [admin_ui.CSRF],
                "state": [state or editor.snapshot()["state"]],
                "rules": [rules],
                "confirm": [confirm or ("ENABLE" if enabled else "APPLY")],
                **({"enabled": ["on"]} if enabled else {})}

    def apply(self, **opts):
        with patch.object(editor.engine_dashboard, "snapshot",
                          return_value=(INVENTORY, ("duckduckgo", "brave"), "state")):
            return editor.apply_form(self.form(**opts))

    def test_missing_policy_disabled_read_only_and_private_atomic_writes(self):
        self.assertEqual(editor.snapshot()["engines"], {})
        self.assertFalse(self.path.parent.exists())
        self.assertIn("saved", self.apply(rules="duckduckgo=15\nbrave=60"))
        self.assertIsNone(engine_pacing.policy())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(editor.snapshot()["engines"], {"brave": 60, "duckduckgo": 15})
        self.assertEqual(list(self.path.parent.glob(".engine-pacing-policy-*")), [])

    def test_enable_stale_cas_disable_and_sleeping_backend(self):
        self.apply()
        stale = self.form(enabled=True)
        with patch.object(editor.engine_dashboard, "snapshot",
                          return_value=(INVENTORY, ("duckduckgo",), "state")):
            self.assertIn("saved", editor.apply_form(stale))
        self.assertEqual(engine_pacing.policy(), {"duckduckgo": 15})
        with self.assertRaisesRegex(ValueError, "changed"):
            editor.apply_form(stale)
        disabled = self.form(confirm="DISABLE")
        with patch.object(editor.engine_dashboard, "snapshot",
                          side_effect=ValueError("sleeping backend")) as inventory:
            editor.apply_form(disabled)
            inventory.assert_not_called()
        self.assertIsNone(engine_pacing.policy())

    def test_enabling_requires_current_enabled_inventory(self):
        for engines in ("qwant=60", "unknown=5", "Google=30",
                        "DuckDuckGo=15"):
            with self.subTest(engines=engines), patch.object(
                    editor.engine_dashboard, "snapshot",
                    return_value=(INVENTORY, ("duckduckgo",), "state")):
                with self.assertRaises(ValueError):
                    editor.apply_form(self.form(enabled=True, rules=engines))
                self.assertFalse(self.path.exists())
        with patch.object(editor.engine_dashboard, "snapshot",
                          side_effect=ValueError("backend asleep")):
            with self.assertRaisesRegex(ValueError, "inventory"):
                editor.apply_form(self.form(enabled=True))
        self.assertFalse(self.path.exists())

    def test_validation_rejects_untrusted_and_duplicate_rules(self):
        for rules in ("duckduckgo=3601", "duckduckgo=-1",
                      "duckduckgo=3\nDuckDuckGo=5",
                      "duckduckgo=4\nbad<script>=10", "duckduckgo=3.5",
                      "google=0", "duckduckgo =4", "duckduckgo=1" * 1000):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                self.apply(rules=rules)
        self.assertFalse(self.path.exists())

    def test_invalid_file_and_symlink_refuse_replacement(self):
        self.apply()
        self.path.write_text("{bad")
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.path.read_text(), "{bad")
        self.path.unlink()
        target = self.root / "outside"
        target.write_text("untouched")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            editor.snapshot()
        self.assertEqual(target.read_text(), "untouched")

    def test_stale_concurrent_editor_only_one_can_commit(self):
        form = self.form()
        results = []
        def writer():
            try:
                editor.apply_form(form)
                results.append("saved")
            except ValueError:
                results.append("rejected")
        threads = [threading.Thread(target=writer) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=4)
            self.assertFalse(item.is_alive())
        self.assertCountEqual(results, ["saved", "rejected"])


class PacingEditorHTTPTests(PacingEditorSettingsTests):
    def setUp(self):
        super().setUp()
        server = admin_ui.ThreadingHTTPServer(("127.0.0.1", 0), admin_ui.Handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        self.port = server.server_address[1]
        port = patch.object(admin_ui, "PORT", self.port)
        port.start()
        self.addCleanup(port.stop)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

    def request(self, method, data=None, headers=None):
        client = HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            body = urlencode(data) if data is not None else None
            client.request(method, "/engine-pacing-settings", body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    **(headers or {})})
            response = client.getresponse()
            return response.status, response.read().decode()
        finally:
            client.close()

    def test_page_and_post_confirmation_and_escape(self):
        code, page = self.request("GET")
        self.assertEqual(code, 200)
        self.assertIn('action="/engine-pacing-settings"', page)
        self.assertIn("Disabled by default", page)
        self.assertEqual(self.request("POST", data={
            "csrf": admin_ui.CSRF, "state": editor.snapshot()["state"],
            "rules": "duckduckgo=15", "confirm": "APPLY"})[0], 200)
        code, page = self.request("GET")
        self.assertEqual(code, 200)
        self.assertIn("duckduckgo=15", page)
        state = editor.snapshot()["state"]
        with patch.object(editor.engine_dashboard, "snapshot",
                          return_value=(INVENTORY, ("duckduckgo",), "state")):
            data = {"csrf": admin_ui.CSRF, "state": state, "rules": "duckduckgo=15",
                    "enabled": "on", "confirm": "ENABLE"}
            self.assertEqual(self.request("POST", data=data)[0], 200)
            self.assertEqual(self.request("POST", data=data)[0], 400)
        self.assertEqual(engine_pacing.policy(), {"duckduckgo": 15})

    def test_cross_origin_and_csrf_rejected_without_policy_write(self):
        for headers in ({"Host": "evil.invalid"},
                        {"Origin": "http://evil.invalid"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", headers=headers)[0], 403)
                self.assertEqual(self.request("POST", data={
                    "csrf": admin_ui.CSRF}, headers=headers)[0], 403)
        self.assertEqual(self.request("POST", data={
            "csrf": "invalid", "state": editor.snapshot()["state"],
            "rules": "duckduckgo=15", "confirm": "APPLY"})[0], 403)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
