"""No-root tests for read-only on-demand boot prerequisites."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import on_demand_boot_audit as audit
from scripts import install_secure_tunnel, setup_wizard


GOOD_DOCKER = {
    "state": "stopped",
    "managed_running": 0,
    "managed_stopped": 2,
    "verified_services": {"crawl4ai": 1, "searxng": 1},
    "verified_restart_policies": {"crawl4ai": "no", "searxng": "no"},
    "unverified_project_containers": 0,
    "other_published_crawlers": 0,
}
GOOD_SETTINGS = {"enabled": True, "idle_minutes": 30}
GOOD_ROOT_EXEC = (
    "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
    "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py "
    "--container crawl4ai --startup ; ignore_errors=no ; "
    "start_time=[n/a] ; stop_time=[n/a] }"
)


def user_exec(role: str, *, root: Path | None = None,
              python: str | None = None) -> str:
    interpreter = sys.executable if python is None else python
    launch = (audit.ROOT if root is None else root) / "bootstrap.py"
    return (f"{{ path={interpreter} ; argv[]={interpreter} "
            f"{launch} --service {role} ; ignore_errors=no ; "
            "start_time=[n/a] ; stop_time=[n/a] }")


class ReadonlyBootAuditTests(unittest.TestCase):
    def _successful(self, *, docker=None, settings=None, responses=None,
                    tunnel_gate=True, root_file=True, runtime_files=None):
        called = []

        def run(*args):
            called.append(args)
            if responses is not None and args in responses:
                return responses[args]
            if args[:2] == ("systemctl", "show"):
                return GOOD_ROOT_EXEC
            if args[:3] == ("systemctl", "--user", "show"):
                if args[3] in audit.USER_SERVICES and args[-2] == "ExecStart":
                    return user_exec(
                        "bridge" if args[3] == audit.USER_SERVICES[0]
                        else "tunnel")
                if args[-2] == "LoadState":
                    return "loaded"
                if args[-2] == "ExecStart":
                    mode = ("preflight" if "preflight" in args[3] else "probe")
                    return ("{ path=/usr/bin/sudo ; argv[]=/usr/bin/sudo -n "
                            "/usr/bin/python3 /usr/local/libexec/local-search-mcp/"
                            "crawler_egress_guard.py --container crawl4ai --"
                            + mode + " ; ignore_errors=no ; "
                            "start_time=[n/a] ; stop_time=[n/a] }")
            if args[0] == "loginctl":
                return "yes"
            if args[-2] == "is-enabled":
                return "enabled"
            if args[-2] == "is-active":
                return "active"
            return None

        def gate():
            if not tunnel_gate:
                raise install_secure_tunnel.InstallError("redacted")

        with (patch.object(audit, "_run", side_effect=run),
              patch.object(audit, "_required_runtime_files",
                           side_effect=lambda role: (
                               runtime_files.get(role, True)
                               if runtime_files is not None else True)),
              patch.object(audit, "_reviewed_root_file",
                           return_value=root_file) as file_probe,
              patch.object(install_secure_tunnel,
                           "_verify_effective_tunnel_probe", side_effect=gate)):
            result = audit.audit(
                inventory=GOOD_DOCKER if docker is None else docker,
                settings=GOOD_SETTINGS if settings is None else settings)
        return result, called, file_probe

    def test_stopped_managed_stack_can_pass_only_static_reboot_prerequisites(self):
        result, calls, files = self._successful()
        self.assertEqual(result["state"], "ready_for_manual_reboot_test")
        self.assertFalse(result["safe_to_reboot_proven"])
        self.assertEqual(len(result["checks"]), 17)
        for unit in audit.USER_SERVICES:
            self.assertIn(("systemctl", "--user", "show", unit,
                           "-p", "ExecStart", "--value"), calls)
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(files.call_count, 2)
        self.assertIn(("systemctl", "show", audit.ROOT_SERVICE,
                       "-p", "ExecStart", "--value"), calls)
        # _run's fixed-command allowlist is exercised using a real subprocess
        # mock in test_run_rejects_mutating_commands_and_redacts_errors.

    def test_non_no_docker_restart_policy_blocks_reboot_audit(self):
        for policy in ("unless-stopped", "always", "on-failure", None):
            with self.subTest(policy=policy):
                docker = {**GOOD_DOCKER,
                          "verified_restart_policies": {
                              "crawl4ai": policy, "searxng": "no"}}
                result, _, _ = self._successful(docker=docker)
                self.assertFalse(result["checks"]["backends_have_no_boot_restart"])
                self.assertEqual(result["state"], "blocked")
        result, _, _ = self._successful(
            docker={**GOOD_DOCKER, "other_published_crawlers": 1})
        self.assertFalse(result["checks"]["dedicated_docker_ownership"])
        self.assertEqual(result["state"], "blocked")

    def test_missing_privileged_install_or_tunnel_gate_fails_closed(self):
        for options in ({"root_file": False}, {"tunnel_gate": False}):
            with self.subTest(options=options):
                result, _, _ = self._successful(**options)
                self.assertEqual(result["state"], "blocked")
                self.assertFalse(all(result["checks"].values()))
        result, _, _ = self._successful(settings={"enabled": False, "idle_minutes": 30})
        self.assertFalse(result["checks"]["on_demand_enabled"])

    def test_owner_triggered_guard_units_are_required_and_exact(self):
        for unit, mode in (
            ("local-search-mcp-guard-preflight.service", "preflight"),
            ("local-search-mcp-guard-probe.service", "probe"),
        ):
            expected = (
                "{ path=/usr/bin/sudo ; argv[]=/usr/bin/sudo -n /usr/bin/python3 "
                "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py "
                "--container crawl4ai --" + mode
                + " ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] }")
            for value, allowed in (
                (expected, True),
                (expected.replace("ignore_errors=no", "ignore_errors=yes"), False),
                (expected.replace("--" + mode, "--apply"), False),
                (expected.replace("path=/usr/bin/sudo", "path=/usr/bin/sh"), False),
                (expected + expected, False),
            ):
                with (self.subTest(unit=unit, allowed=allowed, value=value[:40]),
                      patch.object(audit, "_run", side_effect=["loaded", value])):
                    self.assertEqual(audit._user_guard_unit(unit, mode), allowed)
            with patch.object(audit, "_run", return_value=None):
                self.assertFalse(audit._user_guard_unit(unit, mode))

    def test_absent_cold_wake_units_block_preflight_even_if_tunnel_is_up(self):
        default, _, _ = self._successful()
        self.assertEqual(default["state"], "ready_for_manual_reboot_test")
        for unit in ("local-search-mcp-guard-preflight.service",
                     "local-search-mcp-guard-probe.service"):
            with self.subTest(unit=unit):
                missing = {
                    ("systemctl", "--user", "show", unit,
                     "-p", "LoadState", "--value"): "not-found"}
                report, _, _ = self._successful(responses=missing)
                self.assertEqual(report["state"], "blocked")
                self.assertFalse(all(report["checks"].values()))

    def test_effective_startup_command_cannot_be_overridden(self):
        for effective in (
                "", "ExecStart=/usr/bin/true",
                GOOD_ROOT_EXEC.replace("--startup", "--apply"),
                GOOD_ROOT_EXEC.replace("ignore_errors=no", "ignore_errors=yes"),
                GOOD_ROOT_EXEC + GOOD_ROOT_EXEC):
            with (self.subTest(output=effective[:40]),
                  patch.object(audit, "_run", return_value=effective)):
                self.assertFalse(audit._root_effective_start())

    def test_stale_or_swapped_bridge_and_tunnel_commands_block_reboot_audit(self):
        """Active processes do not prove that systemd can restart the service."""
        for unit in audit.USER_SERVICES:
            role = "bridge" if unit == audit.USER_SERVICES[0] else "tunnel"
            other = "tunnel" if role == "bridge" else "bridge"
            original = user_exec(role)
            wrong_commands = (
                user_exec(role, root=Path("/home/old-checkout")),
                user_exec(other),
                user_exec(role, python="/nonexistent/python3"),
                original.replace("ignore_errors=no", "ignore_errors=yes"),
                original + original,
                "",
                None,
            )
            for wrong in wrong_commands:
                with self.subTest(unit=unit, value=repr(wrong)[:120]):
                    overrides = {("systemctl", "--user", "show",
                                  unit, "-p", "ExecStart", "--value"): wrong}
                    report, _, _ = self._successful(responses=overrides)
                    gate = (f"{role}_effective_entrypoint")
                    self.assertFalse(report["checks"][gate])
                    self.assertEqual(report["state"], "blocked")
                    # A process can still be active under a stale command.
                    self.assertTrue(report["checks"][f"{role}_enabled_active"])
        for unit, role in ((audit.USER_SERVICES[0], "tunnel"),
                           ("untrusted.service", "bridge")):
            with self.subTest(unit=unit, role=role), patch.object(
                    audit, "_run", side_effect=AssertionError(
                        "unsupported name must not be queried")):
                self.assertFalse(audit._user_effective_entrypoint(unit, role))

    def test_effective_user_entrypoint_accepts_only_current_checkout(self):
        for unit, role in zip(audit.USER_SERVICES, ("bridge", "tunnel")):
            with self.subTest(unit=unit):
                with patch.object(audit, "_run", return_value=user_exec(role)) as query:
                    self.assertTrue(audit._user_effective_entrypoint(unit, role))
                query.assert_called_once_with(
                    "systemctl", "--user", "show", unit,
                    "-p", "ExecStart", "--value")

    def test_runtime_files_are_required_even_when_services_are_active(self):
        """Only filesystem checks: no module execution, Docker, or sudo."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            venv = root / ".venv" / "bin"
            venv.mkdir(parents=True)
            (root / "bridge.py").write_text("not executable\n")
            python = venv / "python"
            python.write_text("no runtime is executed\n")
            python.chmod(0o755)
            tunnel = root / "tunnel-client"
            tunnel.write_text("not executed\n")
            tunnel.chmod(0o755)
            with patch.object(audit, "ROOT", root), patch.object(
                    audit, "_run", side_effect=AssertionError(
                        "Runtime check must not run subprocesses")):
                self.assertTrue(audit._required_runtime_files("bridge"))
                self.assertTrue(audit._required_runtime_files("tunnel"))
                self.assertFalse(audit._required_runtime_files("invalid"))
                python.unlink()
                self.assertFalse(audit._required_runtime_files("bridge"))
                python.write_text("not executed\n")
                python.chmod(0o644)
                self.assertFalse(audit._required_runtime_files("bridge"))
                python.chmod(0o755)
                (root / "bridge.py").unlink()
                self.assertFalse(audit._required_runtime_files("bridge"))
                (root / "bridge.py").write_text("not executed\n")
                tunnel.unlink()
                self.assertFalse(audit._required_runtime_files("tunnel"))
                tunnel.write_text("not executed\n")
                tunnel.chmod(0o644)
                self.assertFalse(audit._required_runtime_files("tunnel"))
                tunnel.unlink()
                tunnel.symlink_to(python)
                self.assertFalse(audit._required_runtime_files("tunnel"))

    def test_missing_runtime_files_block_reboot_audit(self):
        for role in ("bridge", "tunnel"):
            # Full audit still reports both services active, but the
            # statically missing exec-time dependency blocks boot readiness.
            with self.subTest(role=role):
                report, _, _ = self._successful(runtime_files={role: False})
            self.assertEqual(report["state"], "blocked")
            self.assertFalse(report["checks"][f"{role}_runtime_files"])
            self.assertTrue(report["checks"][f"{role}_enabled_active"])

    def test_service_and_linger_states_are_exact_and_readonly(self):
        service = "local-search-mcp-crawler-egress.service"
        with patch.object(audit, "_run", side_effect=[
                "enabled", "active"]) as run:
            self.assertTrue(audit._is_enabled_and_active(service, user=False))
        self.assertEqual(run.call_count, 2)
        with patch.object(audit, "_run", side_effect=["enabled", "inactive"]):
            self.assertFalse(audit._is_enabled_and_active(service, user=False))

    def test_install_file_symlink_never_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "source.py"
            actual = folder / "target.py"
            alias = folder / "link.py"
            source.write_text("print('safe')\n")
            actual.write_bytes(source.read_bytes())
            alias.symlink_to(actual)
            # Isolate kernel O_NOFOLLOW: otherwise the user-owned /tmp path
            # rejects before reaching os.open and the test gives a false pass.
            with (patch.object(audit, "_trusted_root_parents", return_value=True),
                  patch.object(audit, "_trusted_root_fd", return_value=True)):
                self.assertTrue(audit._reviewed_root_file(actual, source, 0o755))
                self.assertFalse(audit._reviewed_root_file(alias, source, 0o755))
                self.assertFalse(audit._reviewed_root_file(folder / "absent", source, 0o755))

    def test_installed_file_validation_still_rejects_untrusted_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "source.py"
            target = folder / "target.py"
            source.write_text("immutable review baseline")
            target.write_bytes(source.read_bytes())
            with patch.object(audit, "_trusted_root_parents", return_value=False):
                self.assertFalse(audit._reviewed_root_file(target, source, 0o755))
            with (patch.object(audit, "_trusted_root_parents", return_value=True),
                  patch.object(audit, "_trusted_root_fd", return_value=False)):
                self.assertFalse(audit._reviewed_root_file(target, source, 0o755))
            with (patch.object(audit, "_trusted_root_parents", return_value=True),
                  patch.object(audit, "_trusted_root_fd", return_value=True)):
                self.assertTrue(audit._reviewed_root_file(target, source, 0o755))
                target.write_text("content differs, never grant readiness")
                self.assertFalse(audit._reviewed_root_file(target, source, 0o755))

    def test_real_root_metadata_check_rejects_untrusted_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "untrusted"
            target.write_text("owned by ordinary caller")
            fd = audit.os.open(target, audit.os.O_RDONLY)
            try:
                # No metadata or ownership mocks: a local unprivileged
                # temp file must not count as a privileged installed helper.
                self.assertFalse(audit._trusted_root_fd(
                    fd, mode=0o755, size=target.stat().st_size))
                self.assertFalse(audit._trusted_root_fd(
                    fd, mode=0o644, size=target.stat().st_size + 1))
            finally:
                audit.os.close(fd)

    def test_disabled_user_linger_blocks_static_reboot_readiness(self):
        account = audit.pwd.getpwuid(audit.os.getuid()).pw_name
        response = {
            ("loginctl", "show-user", account, "-p", "Linger", "--value"): "no"}
        result, _, _ = self._successful(responses=response)
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(result["checks"]["user_linger_enabled"])

    def test_run_rejects_mutating_commands_and_redacts_errors(self):
        with patch.object(audit.subprocess, "run") as run:
            for command in (
                ("docker", "start", "crawl4ai"),
                ("systemctl", "restart", audit.ROOT_SERVICE),
                ("systemctl", "--user", "disable", audit.USER_SERVICES[0]),
                ("loginctl", "enable-linger", "root"),
                ("systemctl", "--user", "show",
                 "untrusted.service", "-p", "ExecStart", "--value"),
                ("systemctl", "--user", "show",
                 audit.USER_SERVICES[0], "-p", "Environment", "--value"),
            ):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    audit._run(*command)
            run.assert_not_called()
        with patch.object(audit.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=["systemctl"], returncode=1,
                stdout="SECRET_TOKEN", stderr="SECRET_TOKEN")):
            self.assertIsNone(audit._run("systemctl", "is-active", audit.ROOT_SERVICE))
        with patch.object(audit.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["systemctl"], 0, "active\n", "")
            self.assertEqual(audit._run("systemctl", "is-active", audit.ROOT_SERVICE),
                             "active")
            self.assertEqual(run.call_args.kwargs["timeout"], 8)
            self.assertFalse(run.call_args.kwargs["check"])

    def test_setup_wizard_cli_reports_blocking_status_without_side_effects(self):
        ready = {"state": "ready_for_manual_reboot_test", "checks": {}}
        blocked = {"state": "blocked", "checks": {}}
        for payload, expected in ((ready, 0), (blocked, 1)):
            with (self.subTest(payload=payload["state"]),
                  patch.object(audit, "audit", return_value=payload) as probe,
                  patch.object(setup_wizard, "run_existing",
                               side_effect=AssertionError("unexpected execution")),
                  contextlib.redirect_stdout(io.StringIO()) as output):
                self.assertEqual(setup_wizard.main(["--boot-audit"]), expected)
            self.assertEqual(json.loads(output.getvalue()), payload)
            probe.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
