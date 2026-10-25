"""Pre-cutover recovery checkpoint tests; never stop or switch installed services."""
from __future__ import annotations

from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import backup_state
from scripts import rollback_recovery_checkpoint as checkpoint


class RecoveryCheckpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.candidate = self.home / "candidate"
        self.candidate.mkdir(mode=0o700)
        self.current = "a" * 40
        self.old = "b" * 40
        self.identity = {
            "schema": "local-search-mcp/rollback-candidate/v2",
            "source_commit": self.current,
            "target_commit": self.old,
            "activation_performed": False, "ready_for_live_use": False,
        }
        manifest = self.candidate / "candidate.json"
        manifest.write_text(json.dumps(self.identity))
        manifest.chmod(0o600)
        self.root = self.home / "checkout"
        self.root.mkdir(mode=0o700)
        self.state = self.home / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.home / "config"
        self.config.mkdir(mode=0o700)
        db = self.state / "domains.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE domains (domain TEXT, status TEXT)")
            conn.execute("INSERT INTO domains VALUES ('example.org', 'allowed')")
            conn.commit()
        db.chmod(0o600)
        (self.state / "history.jsonl").write_text(
            '{"time_utc":"2026-01-01T12:00:00+00:00","kind":"search","status":"success"}\n')
        (self.state / "history.jsonl").chmod(0o600)
        token = self.config / "config.json"
        token.write_text('{"token":"TOP_SECRET"}')
        token.chmod(0o600)
        self.dest = self.home / "checkpoint"
        self.gates = patch.multiple(
            checkpoint,
            **{"_maintenance_ready": lambda _: None,
               "_checkout_unchanged": lambda *_: None})
        self.gates.start()
        self.addCleanup(self.gates.stop)
        self.candidate_verified = patch.object(checkpoint.rollback_candidate, "inspect",
                                               return_value={"integrity_verified": True})
        self.candidate_verified.start()
        self.addCleanup(self.candidate_verified.stop)
        self.smoke = patch.object(checkpoint.restore_smoke, "smoke", return_value=(1, 1))
        self.smoke_mock = self.smoke.start()
        self.addCleanup(self.smoke.stop)
        self.uid = patch.object(checkpoint.os, "geteuid", return_value=1000)
        self.uid.start()
        self.addCleanup(self.uid.stop)

    def create(self):
        return checkpoint.create(self.candidate, self.dest, root=self.root,
                                 state_dir=self.state, config_dir=self.config)

    def test_positive_private_sealed_checkpoint_and_exact_candidate_binding(self):
        result = self.create()
        self.assertTrue(result["checkpoint_verified"])
        self.assertTrue(result["archive_integrity_verified"])
        self.assertTrue(result["candidate_binding_verified"])
        self.assertFalse(result["activation_performed"])
        self.assertFalse(result["ready_for_live_use"])
        self.assertFalse(result["services_changed"])
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o700)
        for name in (checkpoint.MANIFEST, checkpoint.ARCHIVE):
            self.assertEqual(stat.S_IMODE((self.dest / name).stat().st_mode), 0o600)
        payload = backup_state.verified_payload(self.dest / checkpoint.ARCHIVE)
        self.assertIn("domains.sqlite3", payload)
        self.assertIn("history.jsonl", payload)
        self.assertNotIn("config.json", payload)
        self.assertNotIn(b"TOP_SECRET", (self.dest / checkpoint.MANIFEST).read_bytes())
        self.assertNotIn(b"TOP_SECRET", (self.dest / checkpoint.ARCHIVE).read_bytes())
        self.assertEqual(self.smoke_mock.call_count, 1)
        self.assertTrue(checkpoint.inspect(self.dest)["checkpoint_verified"])
        with self.assertRaises(ValueError):
            self.create()  # never overwrite an existing checkpoint

    def test_integrity_digest_and_incomplete_write_fail_closed(self):
        self.create()
        archive = self.dest / checkpoint.ARCHIVE
        with archive.open("ab") as output:
            output.write(b"modified")
        with self.assertRaises(ValueError):
            checkpoint.inspect(self.dest)
        archive.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            checkpoint.inspect(self.dest)

    def test_missing_manifest_and_extra_entries_rejected(self):
        self.create()
        (self.dest / checkpoint.MANIFEST).unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            checkpoint.inspect(self.dest)
        (self.dest / checkpoint.MANIFEST).write_text("unsealed")
        with self.assertRaises(ValueError):
            checkpoint.inspect(self.dest)
        (self.dest / "foreign.txt").write_text("not allowed")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            checkpoint.inspect(self.dest)

    def test_candidate_change_requires_new_checkpoint(self):
        self.create()
        self.identity["target_commit"] = "c" * 40
        (self.candidate / "candidate.json").write_text(json.dumps(self.identity))
        with self.assertRaisesRegex(ValueError, "another candidate"):
            checkpoint.inspect(self.dest, candidate=self.candidate)

    def test_maintenance_gate_failure_leaves_no_checkpoint_or_backup(self):
        with patch.object(checkpoint, "_maintenance_ready",
                          side_effect=ValueError("active update watcher")):
            with self.assertRaisesRegex(ValueError, "active"):
                self.create()
        self.assertFalse(self.dest.exists())

    def test_second_quiescence_check_blocks_manifest_publication(self):
        calls = 0
        def state(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("service reactivated")
        with patch.object(checkpoint, "_maintenance_ready", side_effect=state):
            with self.assertRaisesRegex(ValueError, "reactivated"):
                self.create()
        self.assertEqual(calls, 2)
        self.assertTrue((self.dest / checkpoint.ARCHIVE).exists())
        self.assertFalse((self.dest / checkpoint.MANIFEST).exists())
        with self.assertRaisesRegex(ValueError, "incomplete"):
            checkpoint.inspect(self.dest)

    def test_changed_checkout_after_backup_blocks_publication(self):
        calls = 0
        def check(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("checkout changed")
        with patch.object(checkpoint, "_checkout_unchanged", side_effect=check):
            with self.assertRaises(ValueError):
                self.create()
        self.assertTrue((self.dest / checkpoint.ARCHIVE).exists())
        self.assertFalse((self.dest / checkpoint.MANIFEST).exists())

    def test_readiness_is_required_but_does_not_stop_services(self):
        with patch.object(checkpoint, "_maintenance_ready", wraps=lambda _: None) as ready:
            self.create()
        self.assertEqual(ready.call_count, 2)

    def test_unsafe_parent_and_existing_destination_are_rejected(self):
        insecure = self.home / "public"
        insecure.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            checkpoint.create(self.candidate, insecure / "out", root=self.root,
                              state_dir=self.state, config_dir=self.config)
        existing = self.home / "existing"
        existing.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            checkpoint.create(self.candidate, existing, root=self.root,
                              state_dir=self.state, config_dir=self.config)

    def test_absent_optional_config_directory_does_not_block_checkpoint(self):
        missing_config = self.home / "config-not-created"
        self.assertFalse(missing_config.exists())
        with patch.object(checkpoint.backup_state, "settings_directory",
                          return_value=missing_config):
            result = checkpoint.create(
                self.candidate, self.dest, root=self.root, state_dir=self.state)
        self.assertTrue(result["checkpoint_verified"])
        self.assertFalse(missing_config.exists())

    def test_checkpoint_cannot_live_under_current_state(self):
        # The parent is private, but a nested checkpoint would itself
        # appear in state being saved or alter a tracked checkout.
        nested = self.state / "snapshot"
        with self.assertRaisesRegex(ValueError, "outside live"):
            checkpoint.create(self.candidate, nested, root=self.root,
                              state_dir=self.state, config_dir=self.config)
        self.assertFalse(nested.exists())

    def test_bad_archive_symlink_and_permissive_files_are_rejected(self):
        self.create()
        archive = self.dest / checkpoint.ARCHIVE
        copy = self.home / "external.zip"
        copy.write_bytes(archive.read_bytes())
        archive.unlink()
        archive.symlink_to(copy)
        with self.assertRaises((OSError, ValueError)):
            checkpoint.inspect(self.dest)
        archive.unlink()
        archive.write_bytes(copy.read_bytes())
        archive.chmod(0o644)
        with self.assertRaises(ValueError):
            checkpoint.inspect(self.dest)

    def test_corrupt_candidate_fails_before_creating_checkpoint(self):
        with patch.object(checkpoint.rollback_candidate, "inspect",
                          side_effect=ValueError("staged source changed")):
            with self.assertRaises(ValueError):
                self.create()
        self.assertFalse(self.dest.exists())

    def test_git_subprocess_failure_is_redacted_by_cli(self):
        with (patch.object(checkpoint, "_checkout_unchanged",
                           side_effect=subprocess.CalledProcessError(
                               1, ["git", "status"], stderr=b"TOP_SECRET")),
              redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(checkpoint.main([
                "--candidate", str(self.candidate),
                "--destination", str(self.dest)]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("TOP_SECRET", errors.getvalue())
        self.assertNotIn(str(self.home), errors.getvalue())
        self.assertFalse(self.dest.exists())

    def test_cli_errors_redact_private_data_and_paths(self):
        with (patch.object(checkpoint, "create",
                           side_effect=ValueError("TOP_SECRET /home/operator/private")),
              redirect_stdout(io.StringIO()) as output,
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(checkpoint.main([
                "--candidate", str(self.candidate),
                "--destination", str(self.dest)]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("TOP_SECRET", errors.getvalue())
        self.assertNotIn(str(self.home), errors.getvalue())


if __name__ == "__main__":
    unittest.main()
