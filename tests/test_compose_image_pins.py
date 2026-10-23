"""No Docker mutations or secret echo from image digest audits."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_compose_image_pins as pins


class ImagePinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        (self.path / "compose.yaml").write_text("image: anything")
        (self.path / ".env").write_text("CRAWL4AI_IMAGE=example:latest\n")

    def test_digest_detection_and_no_side_effect(self):
        dig = "docker.io/searxng/searxng@sha256:" + "a" * 64
        (self.path / "searxng.compose.yaml").write_text("services: {}")
        refs = "unclecode/crawl4ai:latest\n" + dig + "\n"
        with patch.object(pins.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, refs, "")) as runner:
            status = pins.report(self.path)
        self.assertEqual(status["image_count"], 2)
        self.assertFalse(status["all_digest_pinned"])
        self.assertEqual([r["digest_pinned"] for r in status["images"]], [False, True])
        args = runner.call_args.args[0]
        self.assertEqual(args[-2:], ["config", "--images"])
        self.assertEqual(args[2:4], ["--project-directory", str(self.path)])
        self.assertEqual(args[4:6], ["--env-file", str(self.path / ".env")])
        self.assertNotIn("TOKEN=private", str(status))
        self.assertNotIn("up", args)
        self.assertNotIn("pull", args)
        self.assertNotIn(dig, str(status))

    def test_two_digest_refs_pass_with_overlay(self):
        (self.path / "searxng.compose.yaml").write_text("services: {}")
        a = "unclecode/crawl4ai@sha256:" + "1" * 64
        b = "docker.io/searxng/searxng@sha256:" + "2" * 64
        with patch.object(pins.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, a + "\n" + b + "\n", "")) as runner:
            self.assertTrue(pins.report(self.path)["all_digest_pinned"])
        self.assertEqual(runner.call_args.args[0].count("-f"), 2)

    def test_incomplete_service_image_listing_never_passes(self):
        (self.path / "searxng.compose.yaml").write_text("services: {}")
        dig = "unclecode/crawl4ai@sha256:" + "1" * 64
        with patch.object(pins.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, dig + "\n", "")):
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                pins.report(self.path)

    def test_errors_are_sanitized(self):
        with patch.object(pins.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "TOKEN=private")):
            with self.assertRaisesRegex(ValueError, "image inspection failed") as cm:
                pins.report(self.path)
            self.assertNotIn("private", str(cm.exception))
        with patch.object(pins.subprocess, "run", side_effect=OSError("TOKEN=private")):
            with self.assertRaises(ValueError) as cm:
                pins.report(self.path)
            self.assertNotIn("private", str(cm.exception))

    def test_symlinks_are_denied(self):
        elsewhere = self.path / "existing"
        elsewhere.write_text("config")
        (self.path / "searxng.compose.yaml").symlink_to(elsewhere)
        with self.assertRaises(ValueError):
            pins.report(self.path)

    def test_missing_or_linked_private_env_is_rejected(self):
        envfile = self.path / ".env"
        envfile.unlink()
        with self.assertRaisesRegex(ValueError, "Private Compose .env"):
            pins.report(self.path)
        other = self.path / "other.env"
        other.write_text("CRAWL4AI_IMAGE=example:latest\\n")
        envfile.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "Private Compose .env"):
            pins.report(self.path)


if __name__ == "__main__":
    unittest.main()
