"""No Docker use: test the on-demand cross-process coordination primitive."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import stat
import tempfile
import threading
import unittest

from scripts import on_demand_coordination as coordination


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "private"

    def test_exclusive_lock_serializes_two_callers(self):
        started = threading.Event()
        acquired = threading.Event()

        def another():
            started.set()
            with coordination.exclusive(folder=self.folder):
                acquired.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with coordination.exclusive(folder=self.folder):
                future = pool.submit(another)
                self.assertTrue(started.wait(3))
                self.assertFalse(acquired.wait(0.15))
            self.assertTrue(acquired.wait(3))
            future.result(timeout=3)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(
            (self.folder / coordination.NAME).stat().st_mode), 0o600)

    def test_symlinked_or_permissive_lock_fails_closed(self):
        self.folder.mkdir(mode=0o700)
        path = self.folder / coordination.NAME
        outside = self.folder.parent / "outside"
        outside.write_text("UNCHANGED")
        path.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            with coordination.exclusive(folder=self.folder):
                self.fail("Symlink lock must be rejected")
        self.assertEqual(outside.read_text(), "UNCHANGED")
        path.unlink()
        path.write_text("no secrets")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            with coordination.exclusive(folder=self.folder):
                self.fail("Loose lock must be rejected")


if __name__ == "__main__":
    unittest.main()
