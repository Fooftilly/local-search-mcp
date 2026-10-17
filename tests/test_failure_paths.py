"""Startup and upstream-response failure regression tests without network access."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import bootstrap
import bridge


class ServiceStartupFailures(unittest.TestCase):
    def test_unprepared_bridge_cannot_exec(self):
        """Missing private config and absent executable each fail before execve."""
        with tempfile.TemporaryDirectory() as root:
            with patch.object(bootstrap, "ROOT", Path(root)), \
                 patch.object(bootstrap.os, "execve") as execve:
                with self.subTest(missing="config"), \
                     patch.object(bootstrap, "read_config", return_value={}):
                    with self.assertRaisesRegex(bootstrap.SetupError, "Missing CRAWL4AI_API_TOKEN"):
                        bootstrap.run_unattended_service("bridge")
                    execve.assert_not_called()
                with self.subTest(missing="executable"), \
                     patch.object(bootstrap, "read_config", return_value={
                         "CRAWL4AI_API_TOKEN": "test-token",
                         "ALLOWED_CRAWL_DOMAINS": "example.com",
                     }):
                    with self.assertRaisesRegex(bootstrap.SetupError, "Missing service executable"):
                        bootstrap.run_unattended_service("bridge")
                    execve.assert_not_called()

    def test_tunnel_requires_prepared_profile_and_refuses_symlink(self):
        """A missing profile or linked tunnel executable never gets run."""
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            target = base / "untrusted-tunnel"
            target.write_bytes(b"not an executable")
            (base / "tunnel-client").symlink_to(target)
            profile = base / "profile.json"
            with patch.object(bootstrap, "ROOT", base), \
                 patch.object(bootstrap, "read_config", return_value={
                     "CONTROL_PLANE_API_KEY": "test-secret",
                 }), \
                 patch.object(bootstrap.os, "execve") as execve, \
                 patch.object(bootstrap, "profile_path", return_value=profile):
                with self.assertRaises(bootstrap.SetupError):
                    bootstrap.run_unattended_service("tunnel")
                profile.write_text("{}")
                with self.assertRaisesRegex(bootstrap.SetupError, "symlink"):
                    bootstrap.run_unattended_service("tunnel")
                execve.assert_not_called()

    def test_invalid_service_component_does_not_exec(self):
        with patch.object(bootstrap, "read_config", return_value={}), \
             patch.object(bootstrap.os, "execve") as execve:
            with self.assertRaises(bootstrap.SetupError):
                bootstrap.run_unattended_service("invalid")
            execve.assert_not_called()


class BridgeBackendFailures(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(temp.name) / "history.jsonl"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_invalid_or_unhealthy_crawler_health_blocks_post(self):
        """Broken HTTP, malformed JSON and obsolete versions never reach /crawl."""
        upstream_health = (
            httpx.Response(200, text="not json"),
            httpx.Response(502, json={"detail": "backend down"}),
            httpx.Response(200, json={"status": "ok", "version": "0.9.4"}),
        )
        original_client = httpx.Client
        for health_reply in upstream_health:
            requests = []

            def handler(request):
                requests.append((request.method, request.url.path))
                self.assertEqual(request.url.path, "/health")
                return health_reply

            def client_factory(**kwargs):
                return original_client(transport=httpx.MockTransport(handler), trust_env=False)

            with self.subTest(reply=health_reply.status_code, body=health_reply.text), \
                 patch.object(bridge, "_check_public_target", return_value=None), \
                 patch.object(bridge, "CRAWLER_SECURITY_WARNING", None), \
                 patch.object(bridge.httpx, "Client", side_effect=client_factory):
                result = bridge.web_fetch("https://example.com/test")
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["success"])
            self.assertEqual(requests, [("GET", "/health")])


if __name__ == "__main__":
    unittest.main()
