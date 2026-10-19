"""Offline regressions for explicit private Docker Compose scaffolding."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
from scripts import crawler_compose as compose


class CrawlerComposeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.private = self.home / "private"
        self.private.mkdir(mode=0o700)

    def test_creates_owner_only_stack_with_isolated_network_and_no_docker_calls(self):
        with patch.object(compose.subprocess, "run", side_effect=AssertionError("Docker")):
            root = compose.generate(self.private, image="unclecode/crawl4ai:0.9.5",
                                    token="literal$VAR#value")
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for name in ("compose.yaml", ".env"):
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)
        config = (root / "compose.yaml").read_text()
        env = (root / ".env").read_text()
        self.assertEqual((root / "compose.yaml").read_bytes(),
                         compose.COMPOSE_SOURCE.read_bytes())
        self.assertIn("127.0.0.1:11235:11235", config)
        self.assertIn("dns:\n      - 1.1.1.1\n      - 9.9.9.9", config)
        self.assertIn("enable_ipv6: false", config)
        self.assertIn("com.docker.network.bridge.name: lsm-crawler0", config)
        self.assertIn("container_name: crawl4ai", config)
        self.assertIn("networks:\n      - crawler-egress", config)
        self.assertNotIn("hermes-web_default", config)
        self.assertNotIn("literal$VAR#value", config)
        self.assertIn("CRAWL4AI_API_TOKEN='literal$VAR#value'", env)
        self.assertNotIn("CONTROL_PLANE_API_KEY", env + config)

    def test_missing_saved_token_prompts_instead_of_inspecting_docker_env(self):
        with patch.object(bootstrap, "PRIVATE_DIR", self.private), \
             patch.object(bootstrap, "PRIVATE_CONFIG", self.private / "config.json"), \
             patch.object(bootstrap, "read_config", return_value={}), \
             patch.dict(os.environ, {"CRAWL4AI_API_TOKEN": ""}), \
             patch.object(bootstrap, "docker_token",
                          side_effect=AssertionError("container environment accessed")), \
             patch.object(bootstrap, "prompt_secret", return_value="prompted-token") as prompt, \
             patch("sys.argv", ["bootstrap.py", "--setup-only",
                                "--generate-crawler-compose", "--crawler-image",
                                "unclecode/crawl4ai:0.9.5"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.main(), 0)
        prompt.assert_called_once()
        self.assertIn("CRAWL4AI_API_TOKEN='prompted-token'",
                      (self.private / "crawler-compose" / ".env").read_text())

    def test_never_replaces_existing_compose_or_external_deployment(self):
        external = self.home / "hermes-web" / "docker-compose.yml"
        external.parent.mkdir()
        external.write_text("original hermes settings")
        first = compose.generate(self.private, image="myrepo/crawler@sha256:abcdef",
                                 token="abc")
        with self.assertRaisesRegex(compose.ComposeSetupError, "already exists"):
            compose.generate(self.private, image="myrepo/crawler:another", token="new")
        self.assertEqual((first / ".env").read_text().splitlines()[-1],
                         "CRAWL4AI_API_TOKEN='abc'")
        self.assertEqual(external.read_text(), "original hermes settings")

    def test_unsafe_existing_paths_and_invalid_values_fail_closed(self):
        target = self.home / "other"
        target.mkdir()
        alias = self.home / "private-alias"
        alias.symlink_to(target)
        with self.assertRaisesRegex(compose.ComposeSetupError, "Private settings"):
            compose.generate(alias, image="unclecode/crawl4ai:0.9.5", token="secret")
        for image in ("", "bad image", "\nmalicious", "-arg", "repo/img;id",
                      "foo/../../other", "foo:tag${CMD}"):
            with self.subTest(image=image), self.assertRaises(compose.ComposeSetupError):
                compose.generate(self.private, image=image, token="secret")
        for token in ("", "line\nbreak", "return\r", "null\x00", "back\\slash", "it's-ambiguous"):
            with self.subTest(token=token), self.assertRaises(compose.ComposeSetupError):
                compose.generate(self.private, image="repo/crawler:0.9.5", token=token)
        self.assertFalse((self.private / "crawler-compose").exists())

    def test_inspection_only_reads_current_image_metadata(self):
        response = subprocess.CompletedProcess(args=[], returncode=0,
                                               stdout="unclecode/crawl4ai:0.9.5\n")
        with patch.object(compose.subprocess, "run", return_value=response) as runner:
            self.assertEqual(compose.inspect_existing_image(), "unclecode/crawl4ai:0.9.5")
        args = runner.call_args.args[0]
        self.assertEqual(args[:3], ["docker", "inspect", "--type=container"])
        self.assertEqual(args[-1], "crawl4ai")
        self.assertNotIn("run", args)
        self.assertNotIn("Env", " ".join(args))
        with patch.object(compose.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, stdout="")):
            with self.assertRaisesRegex(compose.ComposeSetupError, "--crawler-image"):
                compose.inspect_existing_image()

    def test_explicit_setup_flag_does_not_start_or_reconfigure_containers(self):
        with patch.object(bootstrap, "PRIVATE_DIR", self.private), \
             patch.object(bootstrap, "PRIVATE_CONFIG", self.private / "config.json"), \
             patch.object(bootstrap, "read_config", return_value={
                 "CRAWL4AI_API_TOKEN": "private-token"}), \
             patch.object(bootstrap, "check_services", side_effect=AssertionError("services")), \
             patch.object(bootstrap, "docker_token", side_effect=AssertionError("Docker")), \
             patch.object(bootstrap, "make_venv", side_effect=AssertionError("venv")), \
             patch.object(bootstrap, "tunnel_client", side_effect=AssertionError("tunnel")), \
             patch.object(bootstrap, "ensure_profile", side_effect=AssertionError("profile")), \
             patch("sys.argv", ["bootstrap.py", "--setup-only",
                                "--generate-crawler-compose", "--crawler-image",
                                "unclecode/crawl4ai:0.9.5"]):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(bootstrap.main(), 0)
        self.assertNotIn("private-token", stdout.getvalue())
        self.assertIn("not started", stdout.getvalue())
        self.assertTrue((self.private / "crawler-compose" / "compose.yaml").is_file())

    def test_generation_rejects_implicit_or_service_execution(self):
        with patch("sys.argv", ["bootstrap.py", "--generate-crawler-compose",
                                "--crawler-image", "repo/crawler:0.9.5"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bootstrap.main(), 1)
        with patch("sys.argv", ["bootstrap.py", "--setup-only", "--crawler-image",
                                "repo/crawler:0.9.5"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bootstrap.main(), 1)


if __name__ == "__main__":
    unittest.main()
