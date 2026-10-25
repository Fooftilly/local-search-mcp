"""Offline backup, staged restore and corruption rejection regressions."""
from __future__ import annotations

from contextlib import closing, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
import sqlite3
import stat
import tempfile
import unittest
import warnings
import zipfile
import zlib

from scripts import backup_state as backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.archive = self.base / "state-backup.zip"
        self.db = self.state / "domains.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE domains (domain TEXT, status TEXT)")
            conn.execute("INSERT INTO domains VALUES ('example.org', 'allowed')")
            conn.commit()
        self.db.chmod(0o600)
        self.history = self.state / "history.jsonl"
        self.history.write_text('{"kind":"search","query":"private"}\n')
        self.history.chmod(0o600)

    def test_backup_verify_restore_roundtrip_and_private_permissions(self):
        backup.backup(self.archive, state_dir=self.state)
        self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        content = backup.verified_payload(self.archive)
        self.assertEqual(set(content), {"domains.sqlite3", "history.jsonl"})
        self.assertIn(b"private", content["history.jsonl"])
        staged = self.base / "restored"
        backup.restore(self.archive, staged)
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((staged / "domains.sqlite3").stat().st_mode), 0o600)
        self.assertEqual((staged / "history.jsonl").read_bytes(), self.history.read_bytes())
        with closing(sqlite3.connect(staged / "domains.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM domains").fetchone()[0],
                             "allowed")
        self.assertTrue(self.db.exists())

    def test_secret_files_are_excluded_and_no_clobber_is_allowed(self):
        secret = self.state / "config.json"
        secret.write_text('{"api_token":"NEVER-EXPORT-ME"}')
        secret.chmod(0o600)
        backup.backup(self.archive, state_dir=self.state)
        self.assertNotIn(b"NEVER-EXPORT-ME", self.archive.read_bytes())
        with zipfile.ZipFile(self.archive) as z:
            self.assertNotIn("config.json", z.namelist())
        original = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            backup.backup(self.archive, state_dir=self.state)
        self.assertEqual(original, hashlib.sha256(self.archive.read_bytes()).hexdigest())
        restored = self.base / "destination"
        restored.mkdir()
        with self.assertRaises(ValueError):
            backup.restore(self.archive, restored)
        self.assertEqual(list(restored.iterdir()), [])

    def test_corrupt_hash_cannot_create_restoration(self):
        backup.backup(self.archive, state_dir=self.state)
        forged = self.base / "forged.zip"
        with zipfile.ZipFile(self.archive) as original, zipfile.ZipFile(forged, "w") as dst:
            for name in original.namelist():
                data = original.read(name)
                if name == "history.jsonl":
                    data = b"altered\n"
                dst.writestr(name, data)
        forged.chmod(0o600)
        target = self.base / "should-not-exist"
        with self.assertRaisesRegex(ValueError, "Integrity mismatch"):
            backup.restore(forged, target)
        self.assertFalse(target.exists())

    def test_duplicate_and_traversal_members_fail(self):
        for entries in [
            [("manifest.json", b"{}"), ("../escape", b"malice")],
            [("manifest.json", b"{}"), ("manifest.json", b"{}")],
        ]:
            with self.subTest(entries=entries):
                forged = self.base / "bad.zip"
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Duplicate name:.*",
                                            category=UserWarning)
                    with zipfile.ZipFile(forged, "w") as dst:
                        for name, data in entries:
                            dst.writestr(name, data)
                forged.chmod(0o600)
                with self.assertRaises(ValueError):
                    backup.verified_payload(forged)
                forged.unlink()

    def test_unsupported_archive_compression_is_rejected_before_extraction(self):
        backup.backup(self.archive, state_dir=self.state)
        forged = self.base / "unsupported-compression.zip"
        with zipfile.ZipFile(self.archive) as original:
            with zipfile.ZipFile(forged, "w", compression=zipfile.ZIP_BZIP2) as zipped:
                for name in original.namelist():
                    zipped.writestr(name, original.read(name))
        forged.chmod(0o600)
        target = self.base / "must-not-restore-unsupported"
        with self.assertRaisesRegex(ValueError, "Unsafe backup entry"):
            backup.verified_payload(forged)
        with self.assertRaises(ValueError):
            backup.restore(forged, target)
        self.assertFalse(target.exists())
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(backup.main(["verify", str(forged)]), 1)
        self.assertNotIn("Traceback", err.getvalue())

    def test_decompression_errors_are_normalized_for_direct_cli_and_restore(self):
        backup.backup(self.archive, state_dir=self.state)
        destination = self.base / "must-not-restore-corrupt"
        for error in (
            zlib.error("PRIVATE history decompression data"),
            NotImplementedError("PRIVATE unsupported ZIP decoder"),
            RuntimeError("PRIVATE unexpected archive stream"),
        ):
            with self.subTest(kind=type(error).__name__):
                with patch.object(zipfile.ZipFile, "read", side_effect=error):
                    with self.assertRaisesRegex(ValueError, "Corrupt backup archive"):
                        backup.verified_payload(self.archive)
                    with redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(backup.main(["verify", str(self.archive)]), 1)
                        self.assertEqual(backup.main([
                            "restore", str(self.archive),
                            "--target-dir", str(destination)]), 1)
                self.assertFalse(destination.exists())
                self.assertNotIn("PRIVATE", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())
        # Valid archives remain accepted after ZIP exception normalization.
        self.assertEqual(set(backup.verified_payload(self.archive)),
                         {"domains.sqlite3", "history.jsonl"})

    def test_symlink_sources_and_unsafe_permissions_fail(self):
        external = self.base / "private.txt"
        external.write_text("secret")
        self.history.unlink()
        self.history.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())
        self.history.unlink()
        self.history.write_text("{}\n")
        self.history.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private owned"):
            backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())

    def test_wal_database_snapshot_includes_committed_changes(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO domains VALUES ('another.example', 'denied')")
            conn.commit()
            backup.backup(self.archive, state_dir=self.state)
            staged = self.base / "wal-restored"
            backup.restore(self.archive, staged)
        with closing(sqlite3.connect(staged / "domains.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM domains").fetchone()[0], 2)

    def test_custom_history_path_uses_override_not_default(self):
        elsewhere = self.base / "private-history"
        elsewhere.mkdir(mode=0o700)
        custom = elsewhere / "events.private.jsonl"
        custom.write_bytes(b'{"query":"actual-custom-history"}\n')
        custom.chmod(0o600)
        rotated = elsewhere / "events.private.jsonl.1"
        rotated.write_bytes(b'{"query":"older-custom-history"}\n')
        rotated.chmod(0o600)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": str(custom)}):
            backup.backup(self.archive, state_dir=self.state)
        stored = backup.verified_payload(self.archive)
        self.assertIn(b"actual-custom-history", stored["history.jsonl"])
        self.assertIn(b"older-custom-history", stored["history.jsonl.1"])
        self.assertNotIn(b"private", stored["history.jsonl"])
        with zipfile.ZipFile(self.archive) as container:
            self.assertFalse(any("events.private.jsonl" in name for name in
                                 container.namelist()))
        staged = self.base / "custom-staged"
        backup.restore(self.archive, staged)
        self.assertEqual((staged / "history.jsonl").read_bytes(),
                         custom.read_bytes())

    def test_missing_configured_history_fails_instead_of_silent_db_only_backup(self):
        outside = self.base / "private-history"
        outside.mkdir(mode=0o700)
        missing = outside / "not-created.jsonl"
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": str(missing)}):
            with self.assertRaisesRegex(ValueError, "Configured history file is missing"):
                backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())

    def test_configured_history_must_be_private_and_absolute(self):
        outside = self.base / "history-readable"
        outside.mkdir(mode=0o755)
        current = outside / "events.jsonl"
        current.write_text("secret history")
        current.chmod(0o600)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": str(current)}):
            with self.assertRaisesRegex(ValueError, "privately owned"):
                backup.backup(self.archive, state_dir=self.state)
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": "relative/events.jsonl"}):
            with self.assertRaisesRegex(ValueError, "must be absolute"):
                backup.backup(self.archive, state_dir=self.state)
        self.assertFalse(self.archive.exists())

    def test_rotated_history_over_db_limit_is_valid_for_backup_and_verify(self):
        # Reduced test constants exercise multi-generation writer-sized data
        # without consuming hundreds of megabytes in the CI matrix.
        db_cap = 16 * 1024
        history_cap = 6000
        for name in backup.HISTORY_NAMES:
            source = self.state / name
            source.write_bytes(os.urandom(5200))
            source.chmod(0o600)
        with (patch.object(backup, "MAX_DB_FILE", db_cap),
              patch.object(backup, "MAX_HISTORY_FILE", history_cap),
              patch.object(backup, "MAX_TOTAL", db_cap + 4 * history_cap),
              patch.object(backup, "MAX_ARCHIVE", db_cap + 4 * history_cap + 4096)):
            backup.backup(self.archive, state_dir=self.state)
            self.assertGreater(self.archive.stat().st_size, db_cap)
            verified = backup.verified_payload(self.archive)
            self.assertEqual(len(verified), 5)
            self.assertTrue(all(len(verified[name]) == 5200
                                for name in backup.HISTORY_NAMES))
            restored = self.base / "large-staged"
            backup.restore(self.archive, restored)
            self.assertEqual((restored / "history.jsonl.3").stat().st_size, 5200)
        self.assertGreaterEqual(backup.MAX_HISTORY_FILE, 100_000_000)
        self.assertGreaterEqual(backup.MAX_TOTAL,
                                backup.MAX_DB_FILE + 4 * 100_000_000)


    def _write_nonsecret_settings(self):
        private = self.base / "private-settings"
        private.mkdir(mode=0o700, exist_ok=True)
        history = private / "history-settings.json"
        history.write_text(json.dumps({
            "schema": "local-search-mcp/nonsecret-history/v1",
            "settings": {"retention_days": 45, "max_bytes": 65536},
        }))
        history.chmod(0o600)
        upstream = private / "upstream-timeouts.json"
        upstream.write_text(json.dumps({
            "schema": "local-search-mcp/upstream-timeouts/v1",
            "settings": {"searxng_seconds": 10, "crawl_seconds": 90},
        }))
        upstream.chmod(0o600)
        secret = private / "config.json"
        secret.write_text('{"api_key":"MUST-NOT-BACK-UP"}')
        secret.chmod(0o600)
        return private

    def test_on_demand_settings_roundtrip_is_private_and_staged_only(self):
        config = self.base / "on-demand-config"
        config.mkdir(mode=0o700)
        settings = {"enabled": True, "idle_minutes": 45}
        optional = config / "on-demand.json"
        raw = json.dumps({"schema": "local-search-mcp/on-demand/v1",
                          "settings": settings}).encode()
        optional.write_bytes(raw)
        optional.chmod(0o600)
        (config / "config.json").write_text('{"token": "DO_NOT_COPY_TOKEN"}')
        (config / "config.json").chmod(0o600)
        backup.backup(self.archive, state_dir=self.state, config_dir=config)
        stored = backup.verified_payload(self.archive)
        self.assertEqual(stored["on-demand.json"], raw)
        self.assertNotIn("config.json", stored)
        with zipfile.ZipFile(self.archive) as archive:
            self.assertNotIn("config.json", archive.namelist())
        target = self.base / "on-demand-staged-only"
        backup.restore(self.archive, target)
        self.assertEqual((target / "on-demand.json").read_bytes(), raw)
        self.assertEqual(stat.S_IMODE((target / "on-demand.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertFalse((config / "config.json").is_symlink())
        self.assertEqual(optional.read_bytes(), raw)  # No live config replacement.

    def test_on_demand_minimum_and_maximum_idle_values_are_allowed(self):
        config = self.base / "on-demand-boundaries"
        config.mkdir(mode=0o700)
        settings = config / "on-demand.json"
        for idle in (1, 1440):
            with self.subTest(idle_minutes=idle):
                raw = json.dumps({
                    "schema": "local-search-mcp/on-demand/v1",
                    "settings": {"enabled": True, "idle_minutes": idle},
                }).encode()
                settings.write_bytes(raw)
                settings.chmod(0o600)
                backup.backup(self.archive, state_dir=self.state, config_dir=config)
                self.assertEqual(backup.verified_payload(self.archive)["on-demand.json"],
                                 raw)
                self.archive.unlink()

    def test_on_demand_settings_reject_unsafe_types_duplicate_and_secret_fields(self):
        config = self.base / "on-demand-input"
        config.mkdir(mode=0o700)
        option = config / "on-demand.json"
        good = {"schema": "local-search-mcp/on-demand/v1",
                "settings": {"enabled": False, "idle_minutes": 30}}
        invalid = (
            {**good, "settings": {"enabled": 1, "idle_minutes": 30}},
            {**good, "settings": {"enabled": "true", "idle_minutes": 30}},
            {**good, "settings": {"enabled": False, "idle_minutes": True}},
            {**good, "settings": {"enabled": False, "idle_minutes": 0}},
            {**good, "settings": {"enabled": False, "idle_minutes": 1441}},
            {**good, "settings": {"enabled": False, "idle_minutes": 30,
                                  "credential": "DO_NOT_ARCHIVE"}},
            {**good, "schema": "local-search-mcp/on-demand/v2"},
        )
        raw_values = [json.dumps(value).encode() for value in invalid]
        raw_values.extend([
            b'{"schema":"local-search-mcp/on-demand/v1","settings":{"enabled":false,"enabled":true,"idle_minutes":30}}',
            b'{"schema":"local-search-mcp/on-demand/v1","schema":"local-search-mcp/on-demand/v1","settings":{"enabled":false,"idle_minutes":30}}',
        ])
        for raw in raw_values:
            with self.subTest(raw=raw[:90]):
                option.write_bytes(raw)
                option.chmod(0o600)
                with self.assertRaises(ValueError):
                    backup.backup(self.archive, state_dir=self.state, config_dir=config)
                self.assertFalse(self.archive.exists())
                with self.assertRaises(ValueError):
                    backup._verify_nonsecret_setting("on-demand.json", raw)
        # A valid on-demand.json is archived again after the rejected inputs.
        option.write_text(json.dumps(good))
        option.chmod(0o600)
        backup.backup(self.archive, state_dir=self.state, config_dir=config)
        self.assertIn("on-demand.json", backup.verified_payload(self.archive))

    def test_optional_settings_roundtrip_never_archives_credentials(self):
        conf = self._write_nonsecret_settings()
        backup.backup(self.archive, state_dir=self.state, config_dir=conf)
        payload = backup.verified_payload(self.archive)
        self.assertEqual(set(payload), {
            "domains.sqlite3", "history.jsonl",
            "history-settings.json", "upstream-timeouts.json",
        })
        self.assertNotIn(b"MUST-NOT-BACK-UP", self.archive.read_bytes())
        with zipfile.ZipFile(self.archive) as z:
            self.assertNotIn("config.json", z.namelist())
        staged = self.base / "staged-settings"
        backup.restore(self.archive, staged)
        for name in ("history-settings.json", "upstream-timeouts.json"):
            self.assertEqual((staged / name).read_bytes(), (conf / name).read_bytes())
            self.assertEqual(stat.S_IMODE((staged / name).stat().st_mode), 0o600)
        # Stage only: source remains unchanged and no live config is rewritten.
        self.assertTrue((conf / "config.json").is_file())

    def test_schema_is_validated_before_backup_and_again_during_verify(self):
        conf = self._write_nonsecret_settings()
        upstream = conf / "upstream-timeouts.json"
        original = upstream.read_bytes()
        invalid = (
            b"not-json",
            json.dumps({"schema": "unexpected", "settings": {
                "searxng_seconds": 10, "crawl_seconds": 90}}).encode(),
            json.dumps({"schema": "local-search-mcp/upstream-timeouts/v1",
                        "settings": {"searxng_seconds": 10, "crawl_seconds": 90,
                                     "api_token": "CREDENTIALS"}}).encode(),
            json.dumps({"schema": "local-search-mcp/upstream-timeouts/v1",
                        "settings": {"searxng_seconds": True, "crawl_seconds": 90}}).encode(),
            json.dumps({"schema": "local-search-mcp/upstream-timeouts/v1",
                        "settings": {"searxng_seconds": 10, "crawl_seconds": 999}}).encode(),
        )
        for payload in invalid:
            with self.subTest(payload=payload[:60]):
                upstream.write_bytes(payload)
                with self.assertRaises(ValueError):
                    backup.backup(self.archive, state_dir=self.state, config_dir=conf)
                self.assertFalse(self.archive.exists())
        upstream.write_bytes(original)
        backup.backup(self.archive, state_dir=self.state, config_dir=conf)
        # Forge a *consistent* manifest: hashes alone must not be sufficient
        # to import unsafe settings or smuggled credentials.
        with zipfile.ZipFile(self.archive) as z:
            manifest = json.loads(z.read("manifest.json"))
            files = {name: z.read(name) for name in z.namelist()}
        files["upstream-timeouts.json"] = invalid[2]
        manifest["files"]["upstream-timeouts.json"] = {
            "size": len(invalid[2]), "sha256": hashlib.sha256(invalid[2]).hexdigest(),
        }
        files["manifest.json"] = json.dumps(manifest).encode()
        forged = self.base / "forged-schema.zip"
        with zipfile.ZipFile(forged, "w") as z:
            for name, contents in files.items():
                z.writestr(name, contents)
        forged.chmod(0o600)
        with self.assertRaises(ValueError):
            backup.verified_payload(forged)
        staged = self.base / "bad-restore"
        with self.assertRaises(ValueError):
            backup.restore(forged, staged)
        self.assertFalse(staged.exists())

    def test_unsafe_config_sources_fail_closed_and_explicit_isolation(self):
        conf = self._write_nonsecret_settings()
        history = conf / "history-settings.json"
        target = self.base / "outside"
        target.write_text("NOT-A-SETTING")
        history.unlink()
        history.symlink_to(target)
        with self.assertRaises(ValueError):
            backup.backup(self.archive, state_dir=self.state, config_dir=conf)
        self.assertEqual(target.read_text(), "NOT-A-SETTING")
        history.unlink()
        history.write_text(json.dumps({
            "schema": "local-search-mcp/nonsecret-history/v1",
            "settings": {"retention_days": 10, "max_bytes": 64000},
        }))
        history.chmod(0o644)
        with self.assertRaises(ValueError):
            backup.backup(self.archive, state_dir=self.state, config_dir=conf)
        history.chmod(0o600)
        conf.chmod(0o755)
        with self.assertRaises(ValueError):
            backup.backup(self.archive, state_dir=self.state, config_dir=conf)
        conf.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "Explicit settings directory is missing"):
            backup.backup(self.archive, state_dir=self.state,
                          config_dir=self.base / "missing-config")
        self.assertFalse(self.archive.exists())
        # Populate the *default* config location so a regression that reads
        # it despite an explicit --state-dir would copy these real settings.
        default_config = self.base / "local-search-mcp"
        default_config.mkdir(mode=0o700)
        for name in ("history-settings.json", "upstream-timeouts.json"):
            source = conf / name
            target = default_config / name
            target.write_bytes(source.read_bytes())
            target.chmod(0o600)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base)}):
            backup.backup(self.archive, state_dir=self.state)
        archived = backup.verified_payload(self.archive)
        self.assertEqual(set(archived), {"domains.sqlite3", "history.jsonl"})
        self.assertTrue(set(backup.SETTINGS_NAMES).isdisjoint(archived))

    def test_default_location_archives_only_validated_nonsecret_settings(self):
        conf = self._write_nonsecret_settings()
        with (patch.object(backup, "state_directory", return_value=self.state),
              patch.object(backup, "settings_directory", return_value=conf)):
            backup.backup(self.archive)
        self.assertIn("history-settings.json", backup.verified_payload(self.archive))
        self.assertIn("upstream-timeouts.json", backup.verified_payload(self.archive))

    def test_cli_backup_verify_restore_and_refusal_of_existing_target(self):
        self.assertEqual(backup.main(["backup", str(self.archive),
                                      "--state-dir", str(self.state)]), 0)
        self.assertEqual(backup.main(["verify", str(self.archive)]), 0)
        self.assertEqual(backup.main(["restore", str(self.archive), "--target-dir",
                                      str(self.base / "staged")]), 0)
        self.assertEqual(backup.main(["restore", str(self.archive), "--target-dir",
                                      str(self.base / "staged")]), 1)


if __name__ == "__main__":
    unittest.main()
