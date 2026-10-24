"""Secure tunnel setup: dry-run, privilege boundaries, and fail-closed behavior."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scripts import install_secure_tunnel as secure
from scripts import setup_wizard


class SecureTunnelSetupTests(unittest.TestCase):
    def setUp(self):
        # These fixtures exercise the unprivileged installer regardless of CI UID.
        patcher = patch.object(secure.os, "geteuid", return_value=1000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def inventory(self):
        return {
            "state": "running", "managed_running": 2, "managed_stopped": 0,
            "verified_services": {"crawl4ai": 1, "searxng": 1},
            "unverified_project_containers": 0, "other_published_crawlers": 0,
        }

    def healthy(self):
        return {"checks": {
            "crawl4ai": True, "searxng": True, "dashboard_http": True,
            "mcp_listener": True, "tunnel_listener": False}}

    def test_readonly_preview_cannot_invoke_sudo_or_write(self):
        with (patch("scripts.backend_inventory.report", return_value=self.inventory()),
              patch("health_cli.snapshot", return_value=self.healthy()),
              patch.object(secure, "_sudo", side_effect=AssertionError("sudo called")),
              patch.object(secure, "_run", side_effect=AssertionError("command called")),
              patch.object(secure, "_install_user_dropin",
                           side_effect=AssertionError("wrote file"))):
            result = secure.preview()
        self.assertEqual(result["mode"], "read-only-preview")
        self.assertTrue(result["confirmation_required"])

    def test_rejects_shared_stopped_and_unhealthy_backends_before_sudo(self):
        for field, value in (("state", "stopped"),
                             ("managed_running", 1),
                             ("unverified_project_containers", 1),
                             ("other_published_crawlers", 1)):
            inv = self.inventory()
            inv[field] = value
            with (
                self.subTest(field=field),
                patch("scripts.backend_inventory.report", return_value=inv),
                patch("health_cli.snapshot", return_value=self.healthy()),
                patch.object(secure, "_sudo", side_effect=AssertionError("sudo called")),
                self.assertRaises(secure.InstallError),
            ):
                secure.install()
        unhealthy = self.healthy()
        unhealthy["checks"]["mcp_listener"] = False
        with (patch("scripts.backend_inventory.report", return_value=self.inventory()),
              patch("health_cli.snapshot", return_value=unhealthy),
              patch.object(secure, "_sudo", side_effect=AssertionError("sudo called")),
              self.assertRaises(secure.InstallError)):
            secure.install()

    def test_credential_environment_cannot_change_sudoers_username(self):
        from collections import namedtuple
        identity = namedtuple("Pw", "pw_name")("realoperator")
        with (patch.dict(os.environ, {"USER": "otherroot", "LOGNAME": "otherroot"}),
              patch.object(secure.pwd, "getpwuid", return_value=identity)):
            line = secure._sudoers_line()
        self.assertTrue(line.startswith("realoperator ALL=(root) NOPASSWD: "))
        self.assertIn("--container crawl4ai --probe", line)
        self.assertIn("--container crawl4ai --preflight", line)
        self.assertEqual(line.count("NOPASSWD:"), 2)
        self.assertNotIn("--apply", line)
        self.assertNotIn("--remove", line)
        self.assertNotIn("otherroot", line)

    def test_existing_privileged_files_are_not_overwritten(self):
        with (patch.object(secure, "_existing_root", return_value=True),
              patch.object(secure, "_root_parent_safety"),
              patch.object(secure, "_sudo", return_value=subprocess.CompletedProcess(
                  [], 1, "", "")) as run):
            with self.assertRaises(secure.InstallError):
                secure._install_root_file(Path(__file__), secure.ROOT_GUARD, "0755")
        self.assertFalse(any("install" in c.args for c in run.call_args_list))

    def test_correct_existing_sudoers_does_not_rewrite(self):
        with (patch.object(secure, "_existing_root", return_value=True),
              patch.object(secure, "_root_parent_safety"),
              patch.object(secure, "_sudo", side_effect=lambda *args, **kw:
                  subprocess.CompletedProcess(args, 0,
                      secure._sudoers_line() if args[0] == "cat"
                      else "root:root:440:regular file" if args[0] == "stat"
                      else "", "")) as root):
            secure._install_sudoers()
        self.assertNotIn("install", [c.args[0] for c in root.call_args_list])
        self.assertIn("visudo", [c.args[0] for c in root.call_args_list])

    def test_install_aborts_before_starting_tunnel_on_probe_failure(self):
        with (patch.object(secure, "preview", return_value=self.inventory()),
              patch.object(secure, "_run", side_effect=lambda *args, **kw:
                  subprocess.CompletedProcess(args, 0,
                      "inactive" if args[:3] == ("systemctl", "--user", "is-active")
                      else "LoadState=loaded" if "show" in args else "", "")) as run,
              patch.object(secure, "_install_root_file"),
              patch.object(secure, "_install_sudoers"),
              patch.object(secure, "_sudo", side_effect=lambda *args, **kw:
                  (_ for _ in ()).throw(secure.InstallError("no probe"))
                  if "--probe" in args else subprocess.CompletedProcess(args, 0, "active", "")) as root,
              patch.object(secure, "_install_user_dropin",
                           side_effect=AssertionError("Unverified tunnel started"))):
            with self.assertRaises(secure.InstallError):
                secure.install()
        self.assertTrue(any("--probe" in c.args for c in root.call_args_list))
        self.assertFalse(any("enable" in c.args and "local-search-mcp-tunnel.service"
                             in c.args for c in run.call_args_list))

    def test_privileged_parent_chain_rejects_writable_directory(self):
        # Untrusted parents must block the root-owned sudo-executed helper.
        with (patch.object(secure, "_existing_root", return_value=True),
              patch.object(secure, "_sudo",
                           return_value=subprocess.CompletedProcess(
                               [], 0, "0:0:777:directory", "")),
              self.assertRaisesRegex(secure.InstallError, "writable")):
            secure._root_parent_safety(secure.ROOT_GUARD)

    def test_failed_tunnel_start_attempts_to_disable_partly_started_unit(self):
        commands = []

        def systemctl(*args, **_kw):
            commands.append(args)
            if args[:4] == ("systemctl", "--user", "enable", "--now") and (
                    secure.USER_TUNNEL in args):
                raise secure.InstallError("mock partial tunnel start")
            return subprocess.CompletedProcess(args, 0,
                "inactive" if "is-active" in args else
                "LoadState=loaded" if "show" in args else
                "ExecStartPre=+/usr/bin/sudo -n /usr/bin/python3" if "cat" in args
                else "", "")

        with (patch.object(secure, "preview", return_value=self.inventory()),
              patch.object(secure, "_run", side_effect=systemctl),
              patch.object(secure, "_sudo", return_value=subprocess.CompletedProcess(
                  [], 0, "active", "")),
              patch.object(secure, "_install_root_file"),
              patch.object(secure, "_install_sudoers"),
              patch.object(secure, "_install_user_dropin")):
            with self.assertRaisesRegex(secure.InstallError, "mock partial"):
                secure.install()
        self.assertIn(("systemctl", "--user", "disable", "--now",
                       secure.USER_TUNNEL), commands)

    def test_readonly_tunnel_option_is_separate_from_confirmation(self):
        with patch.object(setup_wizard, "run_existing", return_value=0) as run:
            self.assertEqual(setup_wizard.main(["--secure-tunnel-preview"]), 0)
            run.assert_called_once_with("scripts/install_secure_tunnel.py")
        with patch.object(setup_wizard, "run_existing", return_value=0) as run:
            self.assertEqual(setup_wizard.main([
                "--secure-tunnel", "--confirm-secure-tunnel", "SECURE"]), 0)
            run.assert_called_once_with("scripts/install_secure_tunnel.py",
                                        "--install", "--confirm", "SECURE")
        with patch.object(setup_wizard, "run_existing",
                          side_effect=AssertionError("Unexpected command")):
            with self.assertRaises(SystemExit):
                setup_wizard.main(["--secure-tunnel"])
            with self.assertRaises(SystemExit):
                setup_wizard.main(["--confirm-secure-tunnel", "SECURE"])

    def test_help_and_dry_run_have_no_external_side_effects(self):
        import contextlib
        import io
        with (patch.object(setup_wizard, "run_existing",
                           side_effect=AssertionError("Executed")),
              patch.object(setup_wizard, "configured_sources",
                           side_effect=AssertionError("Read config")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            with self.assertRaises(SystemExit) as help_exit:
                setup_wizard.main(["--help"])
            self.assertEqual(help_exit.exception.code, 0)
            self.assertIn("--secure-tunnel", output.getvalue())
            self.assertEqual(setup_wizard.main(["--dry-run"]), 0)


if __name__ == "__main__":
    unittest.main()
