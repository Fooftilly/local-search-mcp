"""No secret access; transfer only the three fixed nonsecret profiles."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import cache_settings
import portable_settings as transfer
import safe_settings
import upstream_settings


class PortableSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.temp.name,
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_defaults_round_trip_for_all_profiles(self):
        for name in transfer.PROFILES:
            with self.subTest(name=name):
                exported = transfer.export_profile(name)
                self.assertEqual(set(exported), {"schema", "profile", "settings"})
                self.assertEqual(exported["schema"], transfer.SCHEMA)
                plan = transfer.preview(json.dumps(exported))
                self.assertEqual(plan["changes"], {})
                self.assertEqual(transfer.apply(json.dumps(exported), plan["state"]), name)
                self.assertEqual(transfer.export_profile(name), exported)

    def test_stale_preview_cannot_overwrite_changed_profile(self):
        payload = transfer.export_profile("cache")
        payload["settings"]["search_seconds"] = 120
        text = json.dumps(payload)
        state = transfer.preview(text)["state"]
        cache_settings.update_settings(
            {"search_seconds": "85", "fetch_seconds": "120"}, state)
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            transfer.apply(text, state)
        self.assertEqual(cache_settings.dashboard_settings()["values"]["search_seconds"], 85)

    def test_bounded_whole_values_and_unknown_sections_rejected(self):
        sample = transfer.export_profile("history")
        for bad in (
            {**sample, "secret": "SHOULD_NEVER_EXPORT"},
            {**sample, "settings": {**sample["settings"], "token": "secret"}},
            {**sample, "settings": {**sample["settings"], "retention_days": True}},
            {**sample, "settings": {**sample["settings"], "retention_days": 0}},
            {**sample, "profile": "../config.json"},
            {**sample, "schema": "unsupported"},
        ):
            with self.subTest(bad=str(bad)[:90]), self.assertRaises(ValueError):
                transfer.preview(json.dumps(bad))
        for raw in ("null", "[]", '"hello"', "1",
                    '{"schema":"local-search-mcp/portable-settings/v1",'
                    '"profile":"cache","settings":{},"settings":{}}',
                    "x" * 4097):
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                transfer.preview(raw)

    def test_environment_override_blocks_import_without_writes(self):
        document = json.dumps(transfer.export_profile("upstream"))
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_SEARXNG_TIMEOUT": "35"}):
            with self.assertRaisesRegex(ValueError, "overrides active"):
                transfer.preview(document)

    def test_never_exposes_launcher_secrets_paths_or_history(self):
        with patch.dict(os.environ, {
            "CRAWL4AI_API_TOKEN": "PRIVATE_TOKEN_SENTINEL",
            "OPENAI_MCP_TUNNEL_TOKEN": "OTHER_SENTINEL",
        }):
            for name in transfer.PROFILES:
                exported = json.dumps(transfer.export_profile(name))
                self.assertNotIn("SENTINEL", exported)
                self.assertNotIn(self.temp.name, exported)
                self.assertNotIn("config.json", exported)

    def test_each_profile_is_isolated(self):
        target = transfer.export_profile("history")
        target["settings"]["retention_days"] = 60
        text = json.dumps(target)
        state = transfer.preview(text)["state"]
        self.assertEqual(transfer.apply(text, state), "history")
        self.assertEqual(safe_settings.dashboard_settings()["values"]["retention_days"], 60)
        self.assertEqual(upstream_settings.dashboard_settings()["values"],
                         upstream_settings.DEFAULTS)
        self.assertEqual(cache_settings.dashboard_settings()["values"],
                         cache_settings.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
