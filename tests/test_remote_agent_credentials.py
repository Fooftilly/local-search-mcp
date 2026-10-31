"""Offline token lifecycle tests; no actual operator credentials or services."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from remote_agent_credentials import (
    ProvisioningError, PublicationUncertainError,
    apply_change, main, plan_change,
    _trusted_tty_confirmation,
)
from remote_agent_prerequisites import SCHEMA, load_client_grants


class OfflineCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "grants.json"
        self.tokens = {}

    def confirm(self, client_id, token):
        self.tokens[client_id] = token
        return True

    def test_preview_is_read_only_and_generates_no_token(self):
        with patch("remote_agent_credentials.secrets.token_urlsafe",
                   side_effect=AssertionError("preview minted token")):
            result = plan_change(self.path, "create", "grok")
        self.assertIn("No changes made", result)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.path.parent / "grants.json.lock").exists())

    def test_create_rotate_revoke_keep_verifier_only(self):
        response = apply_change(self.path, "create", "grok",
                                now=lambda: 1000, reveal_and_confirm=self.confirm)
        self.assertEqual(response, "Create completed for grok.")
        original = self.tokens["grok"]
        self.assertNotIn(original, response)
        self.assertNotIn(original, self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(load_client_grants(self.path)[0].token_sha256,
                         hashlib.sha256(original.encode()).digest())
        self.assertEqual(load_client_grants(self.path)[0].expires_at, 87400)
        with self.assertRaisesRegex(ProvisioningError, "already exists"):
            plan_change(self.path, "create", "grok")
        with self.assertRaisesRegex(ProvisioningError, "already exists"):
            apply_change(self.path, "create", "grok", reveal_and_confirm=self.confirm)
        apply_change(self.path, "rotate", "grok", ttl_hours=48,
                     now=lambda: 1000, reveal_and_confirm=self.confirm)
        self.assertNotEqual(original, self.tokens["grok"])
        self.assertNotIn(original, self.path.read_text())
        self.assertEqual(load_client_grants(self.path)[0].expires_at, 173800)
        apply_change(self.path, "revoke", "grok")
        self.assertEqual(load_client_grants(self.path), ())
        self.assertNotIn(original, self.path.read_text())

    def test_confirmation_rejection_does_not_change_store(self):
        with self.assertRaisesRegex(ProvisioningError, "not confirmed"):
            apply_change(self.path, "create", "grok",
                         reveal_and_confirm=lambda *_: False)
        self.assertFalse(self.path.exists())
        apply_change(self.path, "create", "grok", reveal_and_confirm=self.confirm)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ProvisioningError, "not confirmed"):
            apply_change(self.path, "rotate", "grok",
                         reveal_and_confirm=lambda *_: False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_names_ttl_and_unknown_client(self):
        for name in ("", "../escape", "bad client", "bad\nname"):
            with self.subTest(name=name), self.assertRaises(ProvisioningError):
                plan_change(self.path, "create", name)
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "create", "grok", ttl_hours=0,
                         reveal_and_confirm=self.confirm)
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "create", "grok", ttl_hours=721,
                         reveal_and_confirm=self.confirm)
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "rotate", "missing",
                         reveal_and_confirm=self.confirm)
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "revoke", "missing")
        self.assertFalse(self.path.exists())

    def test_fails_closed_for_unsafe_file_and_symlink(self):
        apply_change(self.path, "create", "grok", reveal_and_confirm=self.confirm)
        self.path.chmod(0o644)
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "rotate", "grok",
                         reveal_and_confirm=self.confirm)
        self.path.chmod(0o600)
        mirror = self.path.with_name("mirror")
        mirror.hardlink_to(self.path)
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "rotate", "grok",
                         reveal_and_confirm=self.confirm)
        mirror.unlink()
        self.path.unlink()
        self.path.symlink_to("/dev/null")
        with self.assertRaises(ProvisioningError):
            plan_change(self.path, "create", "grok")
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "create", "grok",
                         reveal_and_confirm=self.confirm)

    def test_refuses_unsafe_lock_path_and_lock_contention(self):
        target_lock = self.path.with_name("grants.json.lock")
        target_lock.symlink_to("/dev/null")
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "create", "grok",
                         reveal_and_confirm=self.confirm)
        self.assertFalse(self.path.exists())
        target_lock.unlink()
        apply_change(self.path, "create", "grok", reveal_and_confirm=self.confirm)
        self.assertEqual(stat.S_IMODE(target_lock.stat().st_mode), 0o600)
        target_lock.chmod(0o644)
        with self.assertRaises(ProvisioningError):
            apply_change(self.path, "revoke", "grok")
        target_lock.chmod(0o600)
        with patch("remote_agent_credentials.fcntl.flock",
                   side_effect=OSError("busy")):
            with self.assertRaises(ProvisioningError):
                apply_change(self.path, "revoke", "grok")
        self.assertEqual(len(load_client_grants(self.path)), 1)

    def test_atomic_replace_failure_preserves_existing_grants(self):
        apply_change(self.path, "create", "grok", reveal_and_confirm=self.confirm)
        before = self.path.read_bytes()
        with patch("remote_agent_credentials.os.replace",
                   side_effect=OSError("disk failure")):
            with self.assertRaises(ProvisioningError):
                apply_change(self.path, "rotate", "grok",
                             reveal_and_confirm=self.confirm)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".grant-update-*")), [])

    def test_directory_sync_failure_reports_published_indeterminate_state(self):
        apply_change(self.path, "create", "grok", reveal_and_confirm=self.confirm)
        previous_digest = load_client_grants(self.path)[0].token_sha256
        # First fsync protects the replacement file; the second protects
        # its directory entry *after* os.replace succeeds.
        with patch("remote_agent_credentials.os.fsync",
                   side_effect=[None, OSError("directory fsync failed")]):
            with self.assertRaisesRegex(
                    PublicationUncertainError, "may already be ACTIVE"):
                apply_change(self.path, "rotate", "grok",
                             reveal_and_confirm=self.confirm)
        current = load_client_grants(self.path)
        self.assertEqual(len(current), 1)
        self.assertNotEqual(current[0].token_sha256, previous_digest)
        self.assertEqual(current[0].token_sha256,
                         hashlib.sha256(self.tokens["grok"].encode()).digest())
        self.assertEqual(list(self.path.parent.glob(".grant-update-*")), [])

    def test_directory_sync_failure_on_revoke_does_not_claim_rollback(self):
        apply_change(self.path, "create", "grok", reveal_and_confirm=self.confirm)
        with patch("remote_agent_credentials.os.fsync",
                   side_effect=[None, OSError("directory fsync failed")]):
            with self.assertRaises(PublicationUncertainError):
                apply_change(self.path, "revoke", "grok")
        self.assertEqual(load_client_grants(self.path), ())

    def test_multiple_clients_and_store_limit(self):
        for i in range(8):
            apply_change(self.path, "create", f"agent-{i}",
                         reveal_and_confirm=self.confirm)
        self.assertEqual(len(load_client_grants(self.path)), 8)
        with self.assertRaisesRegex(ProvisioningError, "Maximum"):
            apply_change(self.path, "create", "agent-8",
                         reveal_and_confirm=self.confirm)
        self.assertEqual(len(load_client_grants(self.path)), 8)

    def test_cli_dry_run_list_and_apply_gate(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main(["--store", str(self.path), "create", "grok"]), 0)
        self.assertIn("No changes made", output.getvalue())
        self.assertFalse(self.path.exists())
        with patch("remote_agent_credentials._trusted_tty_confirmation",
                   self.confirm), patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["--store", str(self.path), "--apply",
                                   "create", "grok"]), 0)
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main(["--store", str(self.path), "list"]), 0)
        self.assertIn("grok: expires at", output.getvalue())
        self.assertNotIn(self.tokens["grok"], output.getvalue())
        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["--store", str(self.path), "--apply",
                                   "revoke", "grok"]), 0)
        self.assertEqual(load_client_grants(self.path), ())

    def test_non_tty_refusal_and_root_protection(self):
        with patch("builtins.open", side_effect=OSError("no controlling tty")):
            with self.assertRaises(ProvisioningError):
                _trusted_tty_confirmation("grok", "s" * 48)
        output = io.StringIO()
        with patch("remote_agent_credentials.os.geteuid", return_value=0), \
                patch("sys.stderr", output):
            self.assertEqual(main(["--store", str(self.path), "list"]), 1)
        self.assertIn("unprivileged", output.getvalue())


if __name__ == "__main__":
    unittest.main()
