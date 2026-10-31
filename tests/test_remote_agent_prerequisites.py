"""Non-destructive tests for confidential verifier reload and root guard gate."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from remote_agent_gateway import LoopbackMCPGateway
from remote_agent_prerequisites import (
    CredentialStoreError, ROOT_GUARD_COMMAND, SCHEMA, MAX_STORE_BYTES,
    fixed_root_tunnel_gate, load_client_grants,
)


class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "grants.json"
        self.token = "a" * 48
        self.record = {
            "client_id": "grok-api",
            "token_sha256": hashlib.sha256(self.token.encode()).hexdigest(),
            "expires_at": 2000000000,
        }
        self.write({"schema": SCHEMA, "grants": [self.record]})

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.path.chmod(0o600)

    def assert_invalid(self):
        with self.assertRaisesRegex(CredentialStoreError, "^Invalid credential store$"):
            load_client_grants(self.path)

    def test_loads_digest_not_raw_token_and_supports_revocation(self):
        grants = load_client_grants(self.path)
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0].client_id, "grok-api")
        self.assertEqual(grants[0].token_sha256,
                         hashlib.sha256(self.token.encode()).digest())
        self.assertFalse(hasattr(grants[0], "token"))
        self.write({"schema": SCHEMA, "grants": []})
        self.assertEqual(load_client_grants(self.path), ())

    def test_replaced_store_is_reloaded_without_caching(self):
        new = self.path.with_name("replacement.json")
        new.write_text(json.dumps({
            "schema": SCHEMA,
            "grants": [{**self.record, "client_id": "next",
                        "expires_at": 2000000010}],
        }), encoding="utf-8")
        new.chmod(0o600)
        new.replace(self.path)
        self.assertEqual(load_client_grants(self.path)[0].client_id, "next")

    def test_denies_absent_file_and_symlink(self):
        self.path.unlink()
        self.assert_invalid()
        target = Path(self.temp.name) / "actual.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o600)
        self.path.symlink_to(target)
        self.assert_invalid()

    def test_denies_unsafe_file_and_parent_permissions(self):
        for mode in (0o644, 0o660, 0o400, 0o666):
            self.path.chmod(mode)
            with self.subTest(mode=oct(mode)):
                self.assert_invalid()
        self.path.chmod(0o600)
        Path(self.temp.name).chmod(0o755)
        self.assert_invalid()
        Path(self.temp.name).chmod(0o700)

    def test_denies_non_regular_file_and_hardlink(self):
        mirror = self.path.with_name("hardlink")
        os.link(self.path, mirror)
        self.assert_invalid()
        mirror.unlink()
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        self.assert_invalid()

    def test_denies_large_file_and_unsafe_json(self):
        with self.path.open("wb") as handle:
            handle.write(b" " * (MAX_STORE_BYTES + 1))
        self.path.chmod(0o600)
        self.assert_invalid()
        for raw in (
            '{"schema":"'+SCHEMA+'","schema":"'+SCHEMA+'","grants":[]}',
            '{"schema":"'+SCHEMA+'","grants":[],"token":"raw"}',
            '{"schema":"'+SCHEMA+'","grants":[{"client_id":"x"}]}',
            '{"schema":"'+SCHEMA+'","grants":NaN}',
            "not json", "{}",
        ):
            self.path.write_text(raw)
            self.path.chmod(0o600)
            with self.subTest(raw=raw[:70]):
                self.assert_invalid()

    def test_denies_duplicate_id_digest_bad_values_and_bad_schema(self):
        for record in (
            [{**self.record}, {**self.record}],
            [{**self.record}, {**self.record, "client_id": "different"}],
            [{**self.record, "token_sha256": "invalid"}],
            [{**self.record, "client_id": "bad client id"}],
            [{**self.record, "expires_at": True}],
            [{**self.record, "expires_at": float("inf")}],
            [{**self.record, "expires_at": -1}],
            [{**self.record, "token": self.token}],
            [{**self.record}] * 9,
        ):
            self.write({"schema": SCHEMA, "grants": record})
            with self.subTest(record=repr(record)[:60]):
                self.assert_invalid()
        self.write({"schema": "other", "grants": [self.record]})
        self.assert_invalid()

    def test_requires_absolute_path_and_no_dotdot(self):
        with self.assertRaises(CredentialStoreError):
            load_client_grants("grants.json")
        with self.assertRaises(CredentialStoreError):
            load_client_grants(self.path.parent / ".." / "grants.json")

    def test_no_symlink_parent(self):
        linked = Path(self.temp.name) / "linked"
        linked.symlink_to(self.temp.name, target_is_directory=True)
        with self.assertRaises(CredentialStoreError):
            load_client_grants(linked / "grants.json")


class RootGuardAdapterTests(unittest.TestCase):
    def test_success_uses_fixed_root_owned_helper_without_shell(self):
        ok = subprocess.CompletedProcess(
            ROOT_GUARD_COMMAND, 0, "PASS: guarded tunnel startup\n", "")
        with patch("remote_agent_prerequisites.subprocess.run", return_value=ok) as run:
            self.assertTrue(fixed_root_tunnel_gate())
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args, (ROOT_GUARD_COMMAND,))
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["cwd"], "/")
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        self.assertEqual(kwargs["timeout"], 30)

    def test_missing_privilege_failed_probe_or_spoofed_output_denied(self):
        for completed in (
            subprocess.CompletedProcess(ROOT_GUARD_COMMAND, 1, "PASS: fake", ""),
            subprocess.CompletedProcess(ROOT_GUARD_COMMAND, 0, "", ""),
            subprocess.CompletedProcess(ROOT_GUARD_COMMAND, 0, "FAIL: guard", ""),
        ):
            with self.subTest(status=completed.returncode, out=completed.stdout):
                with patch("remote_agent_prerequisites.subprocess.run",
                           return_value=completed):
                    self.assertFalse(fixed_root_tunnel_gate())
        for failure in (OSError("no sudo"), subprocess.TimeoutExpired(
                ROOT_GUARD_COMMAND, 30)):
            with patch("remote_agent_prerequisites.subprocess.run",
                       side_effect=failure):
                self.assertFalse(fixed_root_tunnel_gate())


class ActualGatewayBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_file_and_guard_adapters_fail_before_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_path = Path(directory) / "grants.json"
            token = "x" * 48
            record = {
                "client_id": "client-one",
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": 2000000000,
            }
            credential_path.write_text(json.dumps({
                "schema": SCHEMA, "grants": [record]}))
            credential_path.chmod(0o600)
            observed = []
            def handler(request):
                observed.append(str(request.url))
                return httpx.Response(200, json={"ok": True})
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as upstream:
                guard = [False]
                gateway = LoopbackMCPGateway(
                    upstream=upstream,
                    grants=lambda: load_client_grants(credential_path),
                    guard_ready=lambda: guard[0],
                )
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=gateway),
                    base_url="http://127.0.0.1:18790") as client:
                    headers = {"Authorization": "Bearer " + token}
                    request = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
                    self.assertEqual((await client.post(
                        "/mcp", headers=headers, json=request)).status_code, 503)
                    self.assertFalse(observed)
                    guard[0] = True
                    self.assertEqual((await client.post(
                        "/mcp", headers=headers, json=request)).status_code, 200)
                    self.assertEqual(len(observed), 1)
                    credential_path.write_text(json.dumps({
                        "schema": SCHEMA, "grants": []}))
                    credential_path.chmod(0o600)
                    self.assertEqual((await client.post(
                        "/mcp", headers=headers, json=request)).status_code, 401)
                    self.assertEqual(len(observed), 1)
                    credential_path.chmod(0o644)
                    self.assertEqual((await client.post(
                        "/mcp", headers=headers, json=request)).status_code, 503)
                    self.assertEqual(len(observed), 1)


if __name__ == "__main__":
    unittest.main()
