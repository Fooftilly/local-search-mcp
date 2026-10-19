"""Backup/restore privacy and schema validation of optional cache TTL settings."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
import zipfile

from scripts import backup_state as backup


class OptionalCacheSettingsBackupTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.root / "private-config"
        self.config.mkdir(mode=0o700)
        self.archive = self.root / "archive.zip"
        database = self.state / "domains.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE domains (domain TEXT, status TEXT)")
            db.execute("INSERT INTO domains VALUES ('example.org', 'allowed')")
            db.commit()
        database.chmod(0o600)
        self.cache = self.config / "cache-ttl.json"
        self.cache.write_text(json.dumps({
            "schema": "local-search-mcp/cache-ttl/v1",
            "settings": {"search_seconds": 45, "fetch_seconds": 120},
        }))
        self.cache.chmod(0o600)
        secret = self.config / "config.json"
        secret.write_text('{"api_key":"NEVER-BACK-UP"}')
        secret.chmod(0o600)

    def test_optional_settings_backup_verify_and_private_staged_restore(self):
        backup.backup(self.archive, state_dir=self.state, config_dir=self.config)
        payload = backup.verified_payload(self.archive)
        self.assertEqual(set(payload), {"domains.sqlite3", "cache-ttl.json"})
        self.assertEqual(payload["cache-ttl.json"], self.cache.read_bytes())
        # Inspect DECOMPRESSED members, not the compressed archive bytes.
        with zipfile.ZipFile(self.archive) as z:
            self.assertNotIn("config.json", z.namelist())
            for name in z.namelist():
                self.assertNotIn(b"NEVER-BACK-UP", z.read(name))

        staged = self.root / "staged"
        backup.restore(self.archive, staged)
        self.assertEqual((staged / "cache-ttl.json").read_bytes(), self.cache.read_bytes())
        self.assertEqual(stat.S_IMODE((staged / "cache-ttl.json").stat().st_mode), 0o600)
        self.assertEqual((staged / "cache-ttl.json").stat().st_size,
                         self.cache.stat().st_size)
        self.assertFalse((staged / "config.json").exists())

    def test_bad_settings_never_archive_anything(self):
        initial = self.cache.read_bytes()
        bad = [
            b"not-json",
            json.dumps({"schema":"unsupported","settings":{
                "search_seconds":45,"fetch_seconds":120}}).encode(),
            json.dumps({"schema":"local-search-mcp/cache-ttl/v1","settings":{
                "search_seconds":45,"fetch_seconds":120,"api_key":"secret"}}).encode(),
            json.dumps({"schema":"local-search-mcp/cache-ttl/v1","settings":{
                "search_seconds":True,"fetch_seconds":120}}).encode(),
            json.dumps({"schema":"local-search-mcp/cache-ttl/v1","settings":{
                "search_seconds":45,"fetch_seconds":1801}}).encode(),
            # Duplicate top-level settings allow a secret-bearing first object
            # to be hidden from a last-wins parser while retaining raw bytes.
            (b'{"schema":"local-search-mcp/cache-ttl/v1",'
             b'"settings":{"api_token":"SECRET-LEAK"},'
             b'"settings":{"search_seconds":45,"fetch_seconds":120}}'),
            # Duplicate nested field could mask an earlier sensitive value.
            (b'{"schema":"local-search-mcp/cache-ttl/v1",'
             b'"settings":{"search_seconds":"PRIVATE-QUERY",'
             b'"search_seconds":45,"fetch_seconds":120}}'),
            # Duplicates must also reject escaped spelling of the same key.
            (b'{"schema":"local-search-mcp/cache-ttl/v1",'
             b'"settings":{"search_seconds":45,"fetch_seconds":120,'
             b'"fetch_\\u0073econds":120}}'),
        ]
        for document in bad:
            with self.subTest(document=document[:50]):
                self.cache.write_bytes(document)
                with self.assertRaises(ValueError):
                    backup.backup(self.archive, state_dir=self.state,
                                  config_dir=self.config)
                self.assertFalse(self.archive.exists())
        self.cache.write_bytes(initial)
        other = self.root / "outside"
        other.write_text("safe")
        self.cache.unlink()
        self.cache.symlink_to(other)
        with self.assertRaises(ValueError):
            backup.backup(self.archive, state_dir=self.state,
                          config_dir=self.config)
        self.assertEqual(other.read_text(), "safe")

    def test_forged_hash_cannot_hide_secret_in_duplicate_settings(self):
        backup.backup(self.archive, state_dir=self.state, config_dir=self.config)
        with zipfile.ZipFile(self.archive) as z:
            members = {name: z.read(name) for name in z.namelist()}
        raw = (b'{"schema":"local-search-mcp/cache-ttl/v1",'
               b'"settings":{"private_url":"https://hidden.invalid/?token=secret"},'
               b'"settings":{"search_seconds":45,"fetch_seconds":120}}')
        manifest = json.loads(members["manifest.json"])
        manifest["files"]["cache-ttl.json"] = {
            "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        members["cache-ttl.json"] = raw
        members["manifest.json"] = json.dumps(manifest).encode()
        forged = self.root / "duplicate-member.zip"
        with zipfile.ZipFile(forged, "w") as z:
            for name, content in members.items():
                z.writestr(name, content)
        forged.chmod(0o600)
        with self.assertRaises(ValueError):
            backup.verified_payload(forged)
        destination = self.root / "invalid-staging"
        with self.assertRaises(ValueError):
            backup.restore(forged, destination)
        self.assertFalse(destination.exists())

    def test_tampered_archive_with_matching_hash_still_rejected(self):
        backup.backup(self.archive, state_dir=self.state, config_dir=self.config)
        with zipfile.ZipFile(self.archive) as z:
            payload = {name: z.read(name) for name in z.namelist()}
        bad = json.dumps({"schema": "local-search-mcp/cache-ttl/v1",
                          "settings": {"search_seconds": 45, "fetch_seconds": 120,
                                       "token": "not-allowed"}}).encode()
        manifest = json.loads(payload["manifest.json"])
        manifest["files"]["cache-ttl.json"] = {
            "size": len(bad), "sha256": hashlib.sha256(bad).hexdigest()}
        payload["cache-ttl.json"] = bad
        payload["manifest.json"] = json.dumps(manifest).encode()
        forged = self.root / "forged.zip"
        with zipfile.ZipFile(forged, "w") as z:
            for name, content in payload.items():
                z.writestr(name, content)
        forged.chmod(0o600)
        with self.assertRaises(ValueError):
            backup.verified_payload(forged)
        target = self.root / "unsafe-restore"
        with self.assertRaises(ValueError):
            backup.restore(forged, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
