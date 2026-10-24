"""On-demand configuration remains private, strict and off by default."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts import on_demand_settings as settings


class OnDemandSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "private"

    def test_disabled_by_default_without_created_files(self):
        self.assertEqual(settings.read(folder=self.folder),
                         {"enabled": False, "idle_minutes": 30})
        self.assertFalse(self.folder.exists())

    def test_owner_only_atomic_round_trip_and_disable(self):
        saved = settings.save(enabled=True, idle_minutes=45, folder=self.folder)
        self.assertEqual(saved, {"enabled": True, "idle_minutes": 45})
        self.assertEqual(settings.read(folder=self.folder), saved)
        file = self.folder / settings.NAME
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertFalse(list(self.folder.glob(".on-demand-*")))
        self.assertEqual(settings.save(enabled=False, folder=self.folder),
                         {"enabled": False, "idle_minutes": 30})

    def test_invalid_configuration_rejected_without_mutation(self):
        settings.save(enabled=False, idle_minutes=30, folder=self.folder)
        original = (self.folder / settings.NAME).read_bytes()
        for seconds in (0, -1, 1441, True, None, "30", 3.5):
            with self.subTest(timeout=seconds), self.assertRaises(ValueError):
                settings.save(enabled=True, idle_minutes=seconds, folder=self.folder)
            self.assertEqual((self.folder / settings.NAME).read_bytes(), original)
        with self.assertRaises(ValueError):
            settings.save(enabled=1, folder=self.folder)

    def test_symlink_or_broad_settings_are_rejected(self):
        self.folder.mkdir(mode=0o700)
        target = self.folder.parent / "outside"
        target.write_text("DO_NOT_OVERWRITE")
        (self.folder / settings.NAME).symlink_to(target)
        with self.assertRaises(settings.SettingsError):
            settings.save(enabled=True, folder=self.folder)
        with self.assertRaises(settings.SettingsError):
            settings.read(folder=self.folder)
        self.assertEqual(target.read_text(), "DO_NOT_OVERWRITE")
        (self.folder / settings.NAME).unlink()
        self.folder.chmod(0o755)
        with self.assertRaises(settings.SettingsError):
            settings.save(enabled=True, folder=self.folder)
        with self.assertRaises(settings.SettingsError):
            settings.read(folder=self.folder)

    def test_unexpected_or_unsafe_json_fails_closed(self):
        settings.save(enabled=False, folder=self.folder)
        file = self.folder / settings.NAME
        specimens = (
            b"null", b"[]", b"bad json",
            json.dumps({"schema": settings.SCHEMA,
                        "settings": {"enabled": True, "idle_minutes": True}}).encode(),
            json.dumps({"schema": settings.SCHEMA,
                        "settings": {"enabled": True, "idle_minutes": 30,
                                     "token": "SECRET"}}).encode(),
            json.dumps({"schema": "unsupported",
                        "settings": {"enabled": True, "idle_minutes": 30}}).encode(),
            b"x" * 2049,
        )
        for raw in specimens:
            with self.subTest(raw=raw[:30]):
                file.write_bytes(raw)
                with self.assertRaises(settings.SettingsError):
                    settings.read(folder=self.folder)
        file.chmod(0o644)
        with self.assertRaises(settings.SettingsError):
            settings.read(folder=self.folder)


if __name__ == "__main__":
    unittest.main()
