"""Mocked tunnel binary/version inventory; never executes or downloads it."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_tunnel_version as tunnel


class FakeResponse:
    status = 200

    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._data


class TunnelVersionAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_binary_never_downloads_or_executes(self):
        with (patch.object(tunnel, "public_latest",
                           side_effect=AssertionError("No network")),
              patch.object(tunnel.urllib.request, "build_opener",
                           side_effect=AssertionError("No public lookup"))):
            result = tunnel.version_report(root=self.root)
        self.assertEqual(result["local_binary"], {"state": "not_installed"})
        self.assertFalse(result["remote_requested"])
        self.assertNotIn("public_release", result)
        self.assertFalse(result["downloads_performed"])
        self.assertFalse(result["updates_applied"])

    def test_local_binary_hash_and_no_execution(self):
        binary = self.root / "tunnel-client"
        binary.write_bytes(b"FIXED_TEST_BINARY")
        binary.chmod(0o700)
        with patch.object(tunnel.os, "system",
                          side_effect=AssertionError("never execute")):
            result = tunnel.version_report(root=self.root)
        expected = hashlib.sha256(b"FIXED_TEST_BINARY").hexdigest()
        self.assertEqual(result["local_binary"]["sha256"], expected)
        self.assertEqual(result["local_binary"]["version"], "unverified")
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_symlinks_and_world_writable_binaries_fail_closed(self):
        external = self.root / "outside"
        external.write_bytes(b"EXTERNAL")
        (self.root / "tunnel-client").symlink_to(external)
        self.assertEqual(tunnel.version_report(root=self.root)["local_binary"]["state"],
                         "unavailable")
        (self.root / "tunnel-client").unlink()
        (self.root / "tunnel-client").write_bytes(b"test")
        (self.root / "tunnel-client").chmod(0o777)
        self.assertEqual(tunnel.version_report(root=self.root)["local_binary"]["state"],
                         "unavailable")

    def test_opt_in_public_tag_only_never_claims_newer_version(self):
        tag = "v1.2.3"
        document = {"tag_name": tag, "assets": [
            {"name": name, "browser_download_url":
             f"https://github.com/openai/tunnel-client/releases/download/{tag}/{name}"}
            for name in ("tunnel-client-v1.2.3-linux-amd64.zip", "SHA256SUMS.txt")]}
        raw = json.dumps(document).encode()
        class Opener:
            def open(self, request, timeout):
                self.assertEqual(timeout, 7)
                self.assertEqual(request.full_url, tunnel.LATEST)
                return FakeResponse(raw)
        opener = Opener()
        opener.assertEqual = self.assertEqual
        with patch.object(tunnel.urllib.request, "build_opener",
                          return_value=opener) as build:
            result = tunnel.version_report(root=self.root, check_remote=True)
        self.assertEqual(result["public_release"]["tag"], tag)
        self.assertTrue(result["public_release"]["complete_linux_amd64_release"])
        self.assertEqual(result["local_binary"]["state"], "not_installed")
        self.assertNotIn("update_available", result)
        self.assertTrue(any(isinstance(handler, tunnel.urllib.request.ProxyHandler)
                            for handler in build.call_args.args))

    def test_redirected_or_untrusted_public_asset_fails_closed(self):
        document = {"tag_name": "v1", "assets": [
            {"name": "SHA256SUMS.txt", "browser_download_url":
             "https://evil.invalid/credential"}]}
        class Opener:
            def open(self, *_args, **_kwargs):
                return FakeResponse(json.dumps(document).encode())
        with patch.object(tunnel.urllib.request, "build_opener",
                          return_value=Opener()):
            result = tunnel.version_report(root=self.root, check_remote=True)
        self.assertEqual(result["public_release"], {"state": "unavailable"})
        self.assertNotIn("evil.invalid", json.dumps(result))

    def test_truncated_http_response_does_not_crash_maintenance_report(self):
        class Opener:
            def open(self, *_args, **_kwargs):
                raise http.client.IncompleteRead(b"truncated", 100)
        with patch.object(tunnel.urllib.request, "build_opener",
                          return_value=Opener()):
            report = tunnel.version_report(root=self.root, check_remote=True)
        self.assertEqual(report["public_release"], {"state": "unavailable"})

    def test_remote_invalid_type_or_oversized_response_is_redacted(self):
        for response in (b"null", b"[]", b"x" * (tunnel.MAX_RESPONSE + 1)):
            class Opener:
                def open(self, *_args, **_kwargs):
                    return FakeResponse(response)
            with self.subTest(size=len(response)), patch.object(
                    tunnel.urllib.request, "build_opener", return_value=Opener()):
                result = tunnel.version_report(root=self.root, check_remote=True)
                self.assertEqual(result["public_release"]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
