"""Core user-unit installation and explicit legacy watcher cleanup."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_user_services as installer


class ServiceInstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name) / "user"
        self.dir.mkdir()
        self.env = patch.object(installer, "destination", return_value=self.dir)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_preview_and_install_never_create_watchers(self):
        generated = installer.unit_files("/usr/bin/python3", installer.ROOT)
        self.assertEqual(set(generated), set(installer.NAMES))
        self.assertFalse(set(generated) & set(installer.LEGACY_UPDATE_NAMES))
        with (patch.object(installer.os, "geteuid", return_value=1000),
              patch.object(installer, "write_owned") as written):
            self.assertEqual(installer.main(["--install"]), 0)
        self.assertEqual(written.call_count, len(installer.NAMES))
        self.assertFalse(any("local-search-mcp-update" in str(x.args[0])
                             for x in written.call_args_list))

    def test_explicit_cleanup_only_deletes_marker_bearing_old_units(self):
        keep = self.dir / installer.NAMES[0]
        keep.write_text(installer.MARKER + "core\n")
        for name in installer.LEGACY_UPDATE_NAMES:
            (self.dir / name).write_text(installer.MARKER + "old\n")
        with patch.object(installer.os, "geteuid", return_value=1000):
            self.assertEqual(installer.main(["--remove-auto-restart"]), 0)
        self.assertTrue(keep.is_file())
        for name in installer.LEGACY_UPDATE_NAMES:
            self.assertFalse((self.dir / name).exists())

    def test_never_remove_foreign_or_linked_legacy_unit(self):
        name = installer.LEGACY_UPDATE_NAMES[0]
        target = self.dir / name
        with patch.object(installer.os, "geteuid", return_value=1000):
            target.write_text("foreign unit")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                installer.main(["--remove-auto-restart"])
            target.unlink()
            target.symlink_to(self.dir / "unrelated")
            with self.assertRaisesRegex(ValueError, "symlink"):
                installer.main(["--remove-auto-restart"])

    def test_legacy_install_flag_is_removed(self):
        with patch.object(installer.os, "geteuid", return_value=1000):
            with self.assertRaises(SystemExit) as context:
                installer.main(["--install", "--auto-restart"])
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
