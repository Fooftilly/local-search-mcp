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
    @staticmethod
    def effective_probe():
        # Keep this literal independent of the production constant: any
        # weakening of sudo -n, helper path, or probe arguments must fail.
        return ("{ path=/usr/bin/sudo ; argv[]=/usr/bin/sudo -n /usr/bin/python3 "
                "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py "
                "--container crawl4ai --probe ; ignore_errors=no ; "
                "start_time=[n/a] ; stop_time=[n/a] }")

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
                      else "0:0:8120" if args[0] == "stat"
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
                               [], 0, "0:0:41ff", "")),
              self.assertRaisesRegex(secure.InstallError, "writable")):
            secure._root_parent_safety(secure.ROOT_GUARD)

    def test_numeric_stat_format_is_locale_independent(self):
        # Mocked metadata cannot prove installed locale support; instead
        # assert every privileged stat uses untranslated numeric type/mode.
        responses = {
            secure.ROOT_GUARD: "0:0:81ed",
            secure.SUDOERS: "0:0:8120",
        }

        def check(*args, **_kwargs):
            if args[0] == "stat":
                self.assertEqual(args[1:3], ("-c", "%u:%g:%f"))
                return subprocess.CompletedProcess(
                    args, 0, responses.get(Path(args[-1]), "0:0:41ed"), "")
            if args[0] == "cat":
                return subprocess.CompletedProcess(args, 0, secure._sudoers_line(), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with (patch.object(secure, "_sudo", side_effect=check) as privileged,
              patch.object(secure, "_existing_root", return_value=True)):
            secure._root_parent_safety(secure.ROOT_GUARD)
            self.assertEqual(secure._root_mode(secure.ROOT_GUARD), 0o100755)
            secure._install_sudoers()
        stat_calls = [c.args for c in privileged.call_args_list
                      if c.args[0] == "stat"]
        self.assertGreaterEqual(len(stat_calls), 3)
        self.assertTrue(all(call[1:3] == ("-c", "%u:%g:%f")
                            for call in stat_calls))

    def test_numeric_metadata_rejects_unsafe_modes_and_wrong_types(self):
        cases = [
            ("0:0:41ff", "writable"),  # 0777 directory
            ("0:0:81ed", "not a directory"),  # regular file in parent chain
            ("1000:0:41ed", "ownership"),
            ("0:0:directory", "metadata"),
        ]
        for raw, problem in cases:
            with (self.subTest(raw=raw),
                  patch.object(secure, "_existing_root", return_value=True),
                  patch.object(secure, "_sudo", return_value=subprocess.CompletedProcess(
                      [], 0, raw, "")),
                  self.assertRaisesRegex(secure.InstallError, problem)):
                secure._root_parent_safety(secure.ROOT_GUARD)

    def test_effective_prestart_rejects_later_dropin_resets_and_bypasses(self):
        protected = self.effective_probe()
        cases = (
            (protected, True),
            ("", False),  # A later drop-in ExecStartPre= clears the guard.
            ("ExecStartPre=", False),
            ("ExecStartPre=+/usr/bin/sudo -n /usr/bin/python3 " +
             "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py " +
             "--container crawl4ai --probe", False),  # Unit text, not effective state.
            (protected.replace("ignore_errors=no", "ignore_errors=yes"), False),
            (protected.replace("--probe", "--preflight"), False),
            (protected.replace("path=/usr/bin/sudo", "path=/bin/false"), False),
            (protected + protected, False),  # No unreviewed extra pre-start command.
        )
        for output, allowed in cases:
            with (self.subTest(output=output[:80]),
                  patch.object(secure, "_run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, output, "")) as invoke):
                if allowed:
                    secure._verify_effective_tunnel_probe()
                else:
                    with self.assertRaises(secure.InstallError):
                        secure._verify_effective_tunnel_probe()
                invoke.assert_called_once_with(
                    "systemctl", "--user", "show", secure.USER_TUNNEL,
                    "--property=ExecStartPre", "--value")

    def test_later_dropin_reset_refuses_tunnel_activation(self):
        calls = []

        def systemctl(*args, **_kwargs):
            calls.append(args)
            if "--property=ExecStartPre" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(
                args, 0,
                "inactive" if "is-active" in args else
                "LoadState=loaded" if "--property=LoadState" in args else "", "")

        with (patch.object(secure, "preview", return_value=self.inventory()),
              patch.object(secure, "_run", side_effect=systemctl),
              patch.object(secure, "_sudo", return_value=subprocess.CompletedProcess(
                  [], 0, "active", "")),
              patch.object(secure, "_install_root_file"),
              patch.object(secure, "_install_sudoers"),
              patch.object(secure, "_install_user_dropin"),
              self.assertRaisesRegex(secure.InstallError,
                                     "startup probe is missing or overridden")):
            secure.install()
        self.assertTrue(any("--property=ExecStartPre" in call for call in calls))
        self.assertFalse(any("enable" in call and secure.USER_TUNNEL in call
                             for call in calls))

    def test_failed_tunnel_start_attempts_to_disable_partly_started_unit(self):
        commands = []

        def systemctl(*args, **_kw):
            commands.append(args)
            if args[:4] == ("systemctl", "--user", "enable", "--now") and (
                    secure.USER_TUNNEL in args):
                raise secure.InstallError("mock partial tunnel start")
            return subprocess.CompletedProcess(args, 0,
                "inactive" if "is-active" in args else
                self.effective_probe() if "--property=ExecStartPre" in args else
                "LoadState=loaded" if "--property=LoadState" in args
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

    def test_cleanup_failure_warns_and_preserves_original_error(self):
        import contextlib
        import io
        for shutdown_failure in ("nonzero", "exception"):
            with self.subTest(shutdown_failure=shutdown_failure):
                commands = []

                def systemctl_process(argv, **_kwargs):
                    args = tuple(argv)
                    commands.append(args)
                    if args[:4] == ("systemctl", "--user", "disable", "--now"):
                        if shutdown_failure == "exception":
                            raise OSError("simulated inaccessible systemctl")
                        return subprocess.CompletedProcess(args, 1, "", "failure")
                    if (args[:4] == ("systemctl", "--user", "enable", "--now")
                            and secure.USER_TUNNEL in args):
                        return subprocess.CompletedProcess(args, 1, "", "failure")
                    return subprocess.CompletedProcess(
                        args, 0,
                        "inactive" if "is-active" in args else
                        self.effective_probe()
                        if "--property=ExecStartPre" in args else
                        "LoadState=loaded" if "--property=LoadState" in args else "", "")

                stderr = io.StringIO()
                with (patch.object(secure, "preview", return_value=self.inventory()),
                      patch.object(secure.subprocess, "run",
                                   side_effect=systemctl_process),
                      patch.object(secure, "_sudo",
                                   return_value=subprocess.CompletedProcess(
                                       [], 0, "active", "")),
                      patch.object(secure, "_install_root_file"),
                      patch.object(secure, "_install_sudoers"),
                      patch.object(secure, "_install_user_dropin"),
                      contextlib.redirect_stderr(stderr),
                      self.assertRaisesRegex(secure.InstallError,
                                            "Required setup command failed: systemctl")):
                    secure.install()
                self.assertIn("WARNING: automatic tunnel shutdown failed",
                              stderr.getvalue())
                self.assertIn("systemctl --user is-active", stderr.getvalue())
                self.assertIn(("systemctl", "--user", "disable", "--now",
                               secure.USER_TUNNEL), commands)

    def test_ctrl_c_during_tunnel_start_disables_partial_activation(self):
        commands = []

        def interrupted_systemctl(*args, **_kwargs):
            commands.append(args)
            if (args[:4] == ("systemctl", "--user", "enable", "--now")
                    and secure.USER_TUNNEL in args):
                raise KeyboardInterrupt()
            return subprocess.CompletedProcess(
                args, 0,
                "inactive" if "is-active" in args else
                self.effective_probe()
                if "--property=ExecStartPre" in args else
                "LoadState=loaded" if "--property=LoadState" in args else "", "")

        with (patch.object(secure, "preview", return_value=self.inventory()),
              patch.object(secure, "_run", side_effect=interrupted_systemctl),
              patch.object(secure, "_sudo", return_value=subprocess.CompletedProcess(
                  [], 0, "active", "")),
              patch.object(secure, "_install_root_file"),
              patch.object(secure, "_install_sudoers"),
              patch.object(secure, "_install_user_dropin"),
              self.assertRaises(KeyboardInterrupt)):
            secure.install()
        self.assertIn(("systemctl", "--user", "disable", "--now",
                       secure.USER_TUNNEL), commands)

    def test_preview_discloses_probe_and_preflight_permissions(self):
        with (patch("scripts.backend_inventory.report", return_value=self.inventory()),
              patch("health_cli.snapshot", return_value=self.healthy())):
            report = secure.preview()
        privileged = " ".join(report["privileged_install"])
        self.assertIn("--probe", privileged)
        self.assertIn("--preflight", privileged)
        self.assertIn("NOPASSWD", privileged)

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


    def test_installer_conflicts_have_actionable_redacted_diagnostics(self):
        import contextlib
        import io

        scenarios = (
            ("Existing local-search-mcp sudoers differs; refuse overwrite",
             "Existing restricted sudoers policy differs"),
            ("Existing sudoers has unsafe ownership or mode",
             "regular file and mode 0440"),
            ("Effective tunnel firewall startup probe is missing or overridden",
             "effective tunnel pre-start firewall probe"),
            ("Dedicated project ownership/running backends not proven",
             "./setup.sh --docker-status"),
            ("Required bridge and backends are not healthy",
             "python3 health_cli.py --json"),
            ("Stop the running tunnel before installing startup protections",
             "Stop and disable the active tunnel"),
            ("Managed tunnel user unit is not installed",
             "managed tunnel user unit is missing"),
            ("Existing tunnel drop-in differs; refuse overwrite",
             "existing tunnel startup drop-in differs"),
        )
        for reason, expected in scenarios:
            with self.subTest(reason=reason):
                stderr = io.StringIO()
                with (patch.object(secure, "install",
                                   side_effect=secure.InstallError(reason)),
                      contextlib.redirect_stderr(stderr)):
                    status = secure.main(["--install", "--confirm", "SECURE"])
                self.assertEqual(status, 1)
                self.assertIn(expected.lower(), stderr.getvalue().lower())
                self.assertNotIn(".. Check", stderr.getvalue())
                self.assertIn("Check systemctl --user is-active", stderr.getvalue())

    def test_every_advice_key_matches_a_literal_install_error_in_source(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(secure))
        allowlist = next(node.value for node in ast.walk(tree)
                         if isinstance(node, ast.Assign)
                         and any(isinstance(name, ast.Name) and name.id == "advice"
                                 for name in node.targets))
        self.assertIsInstance(allowlist, ast.Dict)
        keys = {key.value for key in allowlist.keys}
        messages = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "InstallError"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                messages.add(node.args[0].value)
        self.assertTrue(keys, "Expected reviewed advice mapping")
        self.assertEqual(keys - messages, set(), "Stale diagnostic messages")

    def test_unrecognized_errors_cannot_expose_private_data(self):
        import contextlib
        import io

        for error in (secure.InstallError("SECRET_TOKEN=/private/path"),
                      OSError("SECRET_TOKEN=/private/path"),
                      ValueError("SECRET_TOKEN=/private/path")):
            with self.subTest(error=type(error).__name__):
                stderr = io.StringIO()
                with (patch.object(secure, "install", side_effect=error),
                      contextlib.redirect_stderr(stderr)):
                    status = secure.main(["--install", "--confirm", "SECURE"])
                self.assertEqual(status, 1)
                self.assertNotIn("SECRET_TOKEN", stderr.getvalue())
                self.assertNotIn("/private/path", stderr.getvalue())
                self.assertIn("Secure tunnel setup incomplete", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
