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

    def test_cloudflare_optional_units_are_separate_and_loopback_only(self):
        result = installer.unit_files(
            "/usr/bin/python3", installer.ROOT, cloudflare_portal=True)
        self.assertEqual(set(result), set(installer.CLOUDFLARE_NAMES))
        self.assertFalse(set(result) & set(installer.NAMES))
        gateway = result["local-search-mcp-remote-gateway.service"]
        origin = result["local-search-mcp-cloudflare-origin.service"]
        self.assertIn("--serve-loopback", gateway)
        self.assertIn("--serve-loopback", origin)
        self.assertIn("remote_agent_server", gateway)
        self.assertIn("remote_agent_cloudflare_origin", origin)
        self.assertIn("Wants=local-search-mcp-remote-gateway.service", origin)
        self.assertNotIn("BindsTo=", origin)
        self.assertNotIn("0.0.0.0", gateway + origin)
        self.assertNotIn("cloudflared", gateway + origin)
        # sudo -n root-guard probe and nftables need this privilege/netlink.
        self.assertIn("NoNewPrivileges=false", gateway)
        self.assertIn("AF_NETLINK", gateway)
        self.assertIn("NoNewPrivileges=true", origin)

    def test_optional_install_uses_private_project_python_not_system_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv = root / ".venv" / "bin"
            venv.mkdir(parents=True)
            (venv / "python").write_text("placeholder")
            with (patch.object(installer, "ROOT", root),
                  patch.object(installer.os, "geteuid", return_value=1000),
                  patch.object(installer, "write_owned") as written):
                self.assertEqual(installer.main(
                    ["--install", "--cloudflare-portal"]), 0)
            self.assertEqual(written.call_count, len(installer.CLOUDFLARE_NAMES))
            expected_exe = installer.quote(str(venv / "python"))
            for call in written.call_args_list:
                self.assertIn(f"ExecStart={expected_exe} -m ", call.args[1])
                self.assertIn(call.args[0].name, installer.CLOUDFLARE_NAMES)
            self.assertFalse(any("/usr/bin/python3" in call.args[1]
                                 for call in written.call_args_list))

    def test_cloudflare_working_directory_is_systemd_escaped_not_quoted(self):
        root = Path('/tmp/project with % space')
        units = installer.unit_files(
            '/usr/bin/python3', root, cloudflare_portal=True)
        for unit in units.values():
            self.assertIn(r'WorkingDirectory=/tmp/project\x20with\x20%%\x20space',
                          unit)
            self.assertNotIn('WorkingDirectory="', unit)
        with self.assertRaisesRegex(ValueError, "absolute"):
            installer.working_directory(Path("relative/private project"))

    def test_cloudflare_remove_preserves_core_units(self):
        core = self.dir / installer.NAMES[0]
        core.write_text(installer.MARKER + "core\\n")
        for name in installer.CLOUDFLARE_NAMES:
            (self.dir / name).write_text(installer.MARKER + "optional\\n")
        with patch.object(installer.os, "geteuid", return_value=1000):
            self.assertEqual(installer.main(
                ["--remove", "--cloudflare-portal"]), 0)
        self.assertTrue(core.exists())
        for name in installer.CLOUDFLARE_NAMES:
            self.assertFalse((self.dir / name).exists())

    def test_legacy_install_flag_is_removed(self):
        with patch.object(installer.os, "geteuid", return_value=1000):
            with self.assertRaises(SystemExit) as context:
                installer.main(["--install", "--auto-restart"])
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
