"""Offline SearXNG installer tests."""
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_searxng_compose as s
from scripts.crawler_compose import ComposeSetupError


class SearxngTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "private"
        self.dir.mkdir(mode=0o700)
        (self.dir / "compose.yaml").write_text("old crawler")
        (self.dir / ".env").write_text("CRAWL4AI_API_TOKEN=hidden")

    def test_exact_git_compose_private_key_and_preserved_existing(self):
        s.install(self.dir)
        self.assertEqual((self.dir / s.NAMES[0]).read_bytes(),
                         (s.SOURCE / s.NAMES[0]).read_bytes())
        self.assertIn("127.0.0.1:8888:8080", (self.dir / s.NAMES[0]).read_text())
        self.assertNotIn("crawler-egress", (self.dir / s.NAMES[0]).read_text())
        settings = (self.dir / s.NAMES[1]).read_text()
        self.assertRegex(settings, r"secret_key: '[a-f0-9]{64}'")
        self.assertNotIn(s.PLACEHOLDER, settings)
        self.assertEqual(stat.S_IMODE((self.dir / s.NAMES[0]).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.dir / s.NAMES[1]).stat().st_mode), 0o644)
        self.assertEqual((self.dir / ".env").read_text(), "CRAWL4AI_API_TOKEN=hidden")

    def test_no_overwrite_or_partial_update(self):
        for name in s.NAMES:
            with self.subTest(name=name):
                path = self.dir / name
                path.write_text("custom")
                with self.assertRaises(ComposeSetupError):
                    s.install(self.dir)
                self.assertEqual(path.read_text(), "custom")
                self.assertFalse((self.dir / next(n for n in s.NAMES if n != name)).exists())
                path.unlink()
        self.dir.chmod(0o755)
        with self.assertRaises(ComposeSetupError):
            s.install(self.dir)
        self.dir.chmod(0o700)

    def test_linked_targets_and_sources_refused(self):
        other = self.dir.parent / "other"
        other.write_text("safe")
        (self.dir / s.NAMES[0]).symlink_to(other)
        with self.assertRaises(ComposeSetupError):
            s.install(self.dir)
        self.assertEqual(other.read_text(), "safe")
        with tempfile.TemporaryDirectory() as temp:
            (self.dir / s.NAMES[0]).unlink()
            with patch.object(s, "SOURCE", Path(temp)):
                with self.assertRaises(ComposeSetupError):
                    s.install(self.dir)


if __name__ == "__main__":
    unittest.main()
