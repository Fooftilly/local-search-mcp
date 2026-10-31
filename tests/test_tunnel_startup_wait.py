"""Guard the systemd tunnel launch against bridge socket startup races.

This exercises the actual --service launcher environment without starting a
tunnel, opening sockets, touching operator credentials, or running Docker.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


class TunnelStartupWaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "tunnel-client").write_bytes(b"local test executable")
        self.profile = self.root / "local-research.yaml"
        self.profile.write_text("test profile\n")
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "bin" / "python").write_bytes(b"test python")
        self.root_patch = patch.object(bootstrap, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        profile_patch = patch.object(
            bootstrap, "profile_path", return_value=self.profile)
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        config_patch = patch.object(
            bootstrap, "read_config", return_value={
                "CONTROL_PLANE_API_KEY": "fake-credential",
                "CRAWL4AI_API_TOKEN": "fake-crawler-token",
                "ALLOWED_CRAWL_DOMAINS": "example.com",
            })
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def launch(self, component, inherited=None):
        with (patch.dict(os.environ, inherited or {}, clear=True),
              patch.object(bootstrap.os, "execve") as execve):
            bootstrap.run_unattended_service(component)
        execve.assert_called_once()
        return execve.call_args.args

    def test_tunnel_gets_bounded_startup_wait(self):
        binary, argv, env = self.launch("tunnel")
        self.assertEqual(binary, str(self.root / "tunnel-client"))
        self.assertEqual(argv[1:], ["run", "--profile", "local-research"])
        self.assertEqual(env["MCP_STARTUP_WAIT_TIMEOUT"], "60s")
        self.assertEqual(env["CONTROL_PLANE_API_KEY"], "fake-credential")

    def test_operator_override_is_not_replaced(self):
        _, _, env = self.launch("tunnel", {
            "MCP_STARTUP_WAIT_TIMEOUT": "15s",
        })
        self.assertEqual(env["MCP_STARTUP_WAIT_TIMEOUT"], "15s")
        _, _, env = self.launch("tunnel", {
            "MCP_STARTUP_WAIT_TIMEOUT": "0s",
        })
        self.assertEqual(env["MCP_STARTUP_WAIT_TIMEOUT"], "0s")

    def test_bridge_launch_never_injects_tunnel_wait(self):
        binary, argv, env = self.launch("bridge")
        self.assertEqual(binary, str(self.root / ".venv" / "bin" / "python"))
        self.assertEqual(argv[1], str(self.root / "bridge.py"))
        self.assertNotIn("MCP_STARTUP_WAIT_TIMEOUT", env)


if __name__ == "__main__":
    unittest.main()
