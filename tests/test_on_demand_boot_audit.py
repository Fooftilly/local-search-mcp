"""No-root tests for read-only on-demand boot prerequisites."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
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


class ReadonlyBootAuditTests(unittest.TestCase):
    def _successful(self, *, docker=None, settings=None, responses=None,
                    tunnel_gate=True, root_file=True):
        called = []

        def run(*args):
            called.append(args)
            if responses is not None and args in responses:
                return responses[args]
            if args[:2] == ("systemctl", "show"):
                return GOOD_ROOT_EXEC
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
        self.assertEqual(len(result["checks"]), 11)
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(files.call_count, 2)
        self.assertIn(("systemctl", "show", audit.ROOT_SERVICE,
                       "-p", "ExecStart", "--value"), calls)
        self.assertFalse(any(cmd[0] in ("sudo", "docker", "nft", "git", "pip")
                             for cmd in calls))
        self.assertFalse(any("enable" in cmd and "--now" in cmd for cmd in calls))
        self.assertNotIn("SECRET", json.dumps(result))

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

    def test_effective_startup_command_cannot_be_overridden(self):
        for effective in (
                "", "ExecStart=/usr/bin/true",
                GOOD_ROOT_EXEC.replace("--startup", "--apply"),
                GOOD_ROOT_EXEC.replace("ignore_errors=no", "ignore_errors=yes"),
                GOOD_ROOT_EXEC + GOOD_ROOT_EXEC):
            with (self.subTest(output=effective[:40]),
                  patch.object(audit, "_run", return_value=effective)):
                self.assertFalse(audit._root_effective_start())

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

    def test_run_rejects_mutating_commands_and_redacts_errors(self):
        with self.assertRaises(ValueError):
            audit._run("docker", "start", "crawl4ai")
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
