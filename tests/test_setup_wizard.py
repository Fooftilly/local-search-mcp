"""Offline checks for the conservative ChatGPT-first setup entry point."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

from scripts import setup_wizard as wizard


class SetupWizardTests(unittest.TestCase):
    def call(self, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = wizard.main(list(args))
        return status, stdout.getvalue()

    def test_help_has_no_imports_commands_or_health_probes(self):
        with (patch.object(wizard, "check_report", side_effect=AssertionError("check")),
              patch.object(wizard, "status_report", side_effect=AssertionError("status")),
              patch.object(wizard, "run_existing", side_effect=AssertionError("run")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            with self.assertRaises(SystemExit) as exc:
                wizard.main(["--help"])
        self.assertEqual(exc.exception.code, 0)
        for option in ("--check", "--status", "--dry-run", "--chatgpt", "--update-status", "--on-demand-status", "--boot-audit"):
            self.assertIn(option, out.getvalue())
        self.assertIn("30 minutes", out.getvalue())

    def test_shell_help_is_read_only_and_noninteractive(self):
        exe = Path(__file__).resolve().parents[1] / "setup.sh"
        result = subprocess.run(["bash", str(exe), "--help"], capture_output=True,
                                text=True, check=False, timeout=10, input="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Examples:", result.stdout)

    def test_real_setup_entrypoint_imports_root_diagnostics_without_pythonpath(self):
        # The shell entry point runs from any working directory; importing
        # bootstrap and health_cli must not rely on a developer PYTHONPATH.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as config_root:
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["XDG_CONFIG_HOME"] = config_root
            env["XDG_STATE_HOME"] = config_root
            for option in ("--status", "--check"):
                with self.subTest(option=option):
                    result = subprocess.run(
                        ["bash", str(root / "setup.sh"), option],
                        cwd=config_root, env=env, capture_output=True, text=True,
                        check=False, timeout=35, input="")
                    self.assertIn(result.returncode, (0, 1), result.stderr)
                    data = json.loads(result.stdout)
                    health = (data["local_health"] if option == "--status"
                              else data["runtime"]["local_health"])
                    self.assertIn("TCP listener checks", health["note"])
                    if option == "--check":
                        self.assertTrue(data["configuration"]["available"])

    def test_project_venv_dependency_probe_uses_local_checker_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            payload = {"remote_requested": False, "packages": [
                {"package": "fastmcp", "installed": "4.1.0"},
                {"package": "httpx", "installed": "0.28.1"},
                {"package": "idna", "installed": "3.11"},
            ]}
            result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))
            with (patch.object(wizard, "ROOT", root),
                  patch.object(wizard.subprocess, "run", return_value=result) as invoke):
                report = wizard.dependency_report()
            self.assertEqual(report["state"], "available")
            self.assertEqual(report["packages"][0]["installed"], "4.1.0")
            argv = invoke.call_args.args[0]
            self.assertEqual(argv, [str(python), "-I", "-B",
                                    str(root / "scripts" / "check_dependency_versions.py")])
            self.assertEqual(invoke.call_args.kwargs["cwd"], root)
            self.assertTrue(invoke.call_args.kwargs["capture_output"])
            self.assertFalse(invoke.call_args.kwargs["check"])

    def test_missing_venv_is_not_silent_system_python_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(wizard, "ROOT", Path(tmp)),
                  patch.object(wizard.subprocess, "run",
                               side_effect=AssertionError("must not run"))):
                result = wizard.dependency_report()
            self.assertEqual(result["state"], "missing")
            self.assertEqual(result["packages"], [])

    def test_failed_venv_probe_redacts_subprocess_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            failed = subprocess.CompletedProcess([], 1, stdout="sensitive output",
                                                 stderr="SECRET_TOKEN")
            with (patch.object(wizard, "ROOT", root),
                  patch.object(wizard.subprocess, "run", return_value=failed)):
                result = wizard.dependency_report()
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("SECRET_TOKEN", json.dumps(result))
            self.assertNotIn("sensitive output", json.dumps(result))

    def test_check_uses_project_venv_report_for_readiness(self):
        dependencies = {"state": "available", "packages": [
            {"package": "fastmcp", "installed": "4.1"},
            {"package": "httpx", "installed": "0.28"},
            {"package": "idna", "installed": "3.11"},
        ]}
        dedicated = {"state": "running", "managed_running": 2,
                     "managed_stopped": 0, "unverified_project_containers": 0,
                     "other_published_crawlers": 0,
                     "verified_services": {"crawl4ai": 1, "searxng": 1}}
        runtime = {"local_health": {"status": "healthy"},
                   "docker_project": dedicated}
        with (patch.object(wizard, "dependency_report", return_value=dependencies),
              patch.object(wizard, "configured_sources",
                           return_value={"available": True}),
              patch.object(wizard, "status_report", return_value=runtime),
              patch.object(wizard, "project_python", return_value=Path("/virtual/python"))):
            report = wizard.check_report()
        self.assertTrue(report["ready"])
        self.assertTrue(report["dedicated_docker_ready"])
        self.assertEqual(report["dependency_versions_in_project_venv"], dependencies)
        self.assertNotIn("dependency_versions_in_current_interpreter", report)
        dependencies = {**dependencies, "packages": [
            *dependencies["packages"][:-1],
            {"package": "idna", "installed": None},
        ]}
        with (patch.object(wizard, "dependency_report", return_value=dependencies),
              patch.object(wizard, "configured_sources",
                           return_value={"available": True}),
              patch.object(wizard, "status_report", return_value=runtime)):
            self.assertFalse(wizard.check_report()["ready"])

    def test_check_separates_shared_backend_health_from_owned_docker(self):
        dependencies = {"state": "available", "packages": [
            {"package": name, "installed": "1"} for name in
            ("fastmcp", "httpx", "idna")]}
        dedicated = {"state": "running", "managed_running": 2,
                     "managed_stopped": 0, "unverified_project_containers": 0,
                     "other_published_crawlers": 0,
                     "verified_services": {"crawl4ai": 1, "searxng": 1}}
        cases = (
            {}, {"state": "unknown"},
            {**dedicated, "state": "unverified"},
            {**dedicated, "state": "not_started"},
            {**dedicated, "managed_running": 1},
            {**dedicated, "managed_stopped": 1},
            {**dedicated, "unverified_project_containers": 1},
            {**dedicated, "unverified_project_containers": True},
            {**dedicated, "other_published_crawlers": 1},
            {**dedicated, "other_published_crawlers": None},
            {**dedicated, "verified_services": {"crawl4ai": 2, "searxng": 0}},
            {**dedicated, "verified_services": {"crawl4ai": True, "searxng": 1}},
        )
        with (patch.object(wizard, "dependency_report", return_value=dependencies),
              patch.object(wizard, "configured_sources",
                           return_value={"available": True}),
              patch.object(wizard, "project_python", return_value=Path("/virtual/python"))):
            for inventory in cases:
                with self.subTest(inventory=inventory), patch.object(
                    wizard, "status_report",
                    return_value={"local_health": {"status": "healthy"},
                                  "docker_project": inventory}):
                    result = wizard.check_report()
                    self.assertFalse(result["dedicated_docker_ready"])
                    # Healthy shared/non-Docker backends remain supported,
                    # but an explicitly unverified project is still unsafe.
                    unsafe = (inventory.get("state") == "unverified"
                              or type(inventory.get("unverified_project_containers", 0))
                              is not int
                              or inventory.get("unverified_project_containers", 0) != 0)
                    self.assertEqual(result["ready"], not unsafe)

    def test_menu_dependency_inspection_only_reads_project_venv(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["5", "0"]),
              patch.object(wizard, "dependency_report",
                           return_value={"state": "missing", "packages": []}) as report,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("no system python helper")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(wizard.main([]), 0)
        report.assert_called_once_with()
        self.assertIn('"state": "missing"', output.getvalue())

    def test_dry_run_never_checks_health_or_runs_programs(self):
        with (patch.object(wizard, "check_report", side_effect=AssertionError("check")),
              patch.object(wizard, "status_report", side_effect=AssertionError("status")),
              patch.object(wizard, "run_existing", side_effect=AssertionError("run"))):
            rc, output = self.call("--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output)["default_client"].split()[0], "ChatGPT")
        self.assertEqual(len(json.loads(output)["actions"]), 13)

    def test_menu_boot_audit_is_read_only(self):
        from scripts import on_demand_boot_audit
        sample = {"state": "blocked", "checks": {"root_guard_matches_checkout": False}}
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["13", "0"]),
              patch.object(on_demand_boot_audit, "audit", return_value=sample) as auditor,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("No child process")),
              patch.object(wizard, "check_report",
                           side_effect=AssertionError("No health probes")),
              patch.object(wizard, "docker_status",
                           side_effect=AssertionError("No extra Docker probing")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(wizard.main([]), 0)
        auditor.assert_called_once_with()
        self.assertIn('"root_guard_matches_checkout": false', output.getvalue())

    def test_status_returns_only_allowlisted_service_state(self):
        with (patch.object(wizard, "health",
                           return_value={"status": "unavailable", "checks": {}}),
              patch.object(wizard, "user_unit_states",
                           return_value={"tunnel": {"state": "unknown"}}),
              patch.object(wizard, "run_existing", side_effect=AssertionError("run"))):
            rc, out = self.call("--status")
        report = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(report["local_health"]["status"], "unavailable")
        # Safe status descriptions may say "private deployment"; reject
        # credential-like material instead of an ordinary adjective.
        self.assertNotIn("private token", out.lower())

    def test_check_failure_redacts_and_fails_closed(self):
        with (patch.object(wizard, "configured_sources",
                           return_value={"available": False, "error": "unsafe config"}),
              patch.object(wizard, "status_report",
                           return_value={"local_health": {"status": "unavailable"}})):
            rc, out = self.call("--check")
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ready"])
        self.assertEqual(json.loads(out)["configuration"]["available"], False)

    def test_unavailable_systemd_is_unknown_not_inactive(self):
        with patch.object(wizard.subprocess, "run",
                          return_value=Mock(returncode=1, stdout="private token")):
            result = wizard.user_unit_states()
        self.assertTrue(all(s["state"] == "unknown" for s in result.values()))

    def test_non_tty_never_invokes_setup_or_shutdown(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: False)),
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("run"))):
            self.assertEqual(wizard.main([]), 2)

    def test_cancelled_mutating_action_has_no_side_effect(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["7", "NO", "0"]),
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("run")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(wizard.main([]), 0)
        self.assertIn("Cancelled", out.getvalue())

    def test_confirmed_setup_uses_existing_script_only(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["3", "CONFIGURE", "0"]),
              patch.object(wizard, "run_existing", return_value=0) as command,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(wizard.main([]), 0)
        command.assert_called_once_with("bootstrap.py", "--setup-only")

    def test_docker_status_flag_uses_read_only_report(self):
        data = {"state": "unverified", "managed_running": 0,
                "other_published_crawlers": 1}
        with (patch.object(wizard, "docker_status", return_value=data) as probe,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("must not launch tools"))):
            rc, result = self.call("--docker-status")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(result), data)
        probe.assert_called_once_with()

    def test_docker_status_is_in_status_report_and_unsafe_check_not_ready(self):
        with (patch.object(wizard, "health",
                           return_value={"status": "healthy", "checks": {}}),
              patch.object(wizard, "user_unit_states", return_value={}),
              patch.object(wizard, "docker_status",
                           return_value={"state": "unverified"}) as probe):
            report = wizard.status_report()
        self.assertEqual(report["docker_project"]["state"], "unverified")
        probe.assert_called_once_with()
        with (patch.object(wizard, "dependency_report",
                           return_value={"state": "available", "packages": [
                               {"package": name, "installed": "1.0"}
                               for name in ("fastmcp", "httpx", "idna")]}),
              patch.object(wizard, "configured_sources",
                           return_value={"available": True}),
              patch.object(wizard, "status_report", return_value=report)):
            self.assertFalse(wizard.check_report()["ready"])

    def test_update_status_flag_is_read_only_and_does_not_mutate(self):
        response = {"mode": "read-only", "state": "inspected",
                    "remote_checked": False, "updates_applied": False}
        with (patch.object(wizard, "local_update_status",
                           return_value=response) as inspect,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("unexpected program run"))):
            code, output = self.call("--update-status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), response)
        inspect.assert_called_once_with()

    def test_update_menu_shows_existing_local_preflight_only(self):
        response = {"mode": "read-only", "state": "unavailable",
                    "remote_checked": False, "updates_applied": False}
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["6", "0"]),
              patch.object(wizard, "local_update_status",
                           return_value=response) as inspect,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("unexpected side effect")),
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(wizard.main([]), 0)
        inspect.assert_called_once_with()
        self.assertIn('"remote_checked": false', out.getvalue())
        self.assertIn("No automatic Git fetch", out.getvalue())

    def test_on_demand_diagnostic_is_inert_and_uses_single_inventory(self):
        sample = {"state": "unverified", "other_published_crawlers": 2}
        with (patch.object(wizard, "docker_status", return_value=sample) as docker,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("No mutating process")),
              patch("scripts.on_demand_eligibility.eligibility",
                    return_value={"enabled": False, "can_activate": False,
                                  "state": "blocked"}) as inspect):
            rc, result = self.call("--on-demand-status")
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(result)["state"], "blocked")
            docker.assert_called_once_with()
            inspect.assert_called_once_with(inventory=sample)

    def test_on_demand_status_and_check_share_docker_inventory(self):
        sample = {"state": "unverified", "other_published_crawlers": 1}
        with (patch.object(wizard, "docker_status", return_value=sample) as docker,
              patch.object(wizard, "health", return_value={"status": "unavailable"}),
              patch.object(wizard, "user_unit_states", return_value={}),
              patch("scripts.on_demand_eligibility.eligibility",
                    return_value={"enabled": False, "can_activate": False,
                                  "state": "blocked"}) as inspect):
            result = wizard.status_report()
        docker.assert_called_once_with()
        inspect.assert_called_once_with(inventory=sample)
        self.assertEqual(result["on_demand_preflight"]["state"], "blocked")

    def test_on_demand_menu_option_nine_does_not_start_containers(self):
        sample = {"mode": "read-only", "enabled": False,
                  "can_activate": False, "state": "blocked"}
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["9", "0"]),
              patch.object(wizard, "on_demand_status", return_value=sample) as inspect,
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("mutating process")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(wizard.main([]), 0)
        inspect.assert_called_once_with()
        self.assertIn('"can_activate": false', output.getvalue())

    def test_enabled_on_demand_reports_runtime_not_stale_candidate_gates(self):
        inventory = {"state": "running"}
        active = {"mode": "on-demand", "enabled": True, "idle_minutes": 30,
                  "backends": {
                      "crawl4ai": {"state": "running"},
                      "searxng": {"state": "running"}}}
        cases = (
            (active, "running"),
            ({**active, "backends": {
                "crawl4ai": {"state": "sleeping"},
                "searxng": {"state": "sleeping"}}}, "sleeping"),
            ({**active, "backends": {
                "crawl4ai": {"state": "sleeping"},
                "searxng": {"state": "running"}}}, "partially_running"),
            ({**active, "backends": {
                "crawl4ai": {"state": "waking"},
                "searxng": {"state": "running"}}}, "transitioning"),
            ({**active, "backends": {
                "crawl4ai": {"state": "failed"},
                "searxng": {"state": "running"}}}, "failed"),
            ({"mode": "disabled", "enabled": False, "backends": None},
             "unverified"),
            ({"state": "unavailable"}, "unverified"),
            ({**active, "backends": None}, "unverified"),
        )
        for runtime, state in cases:
            with (self.subTest(state=state, runtime=runtime),
                  patch("scripts.on_demand_eligibility.eligibility",
                        return_value={"state": "manual_verification_required",
                                      "reason": "Potential dedicated deployment",
                                      "next_gates": ["Old pre-activation check"]}),
                  patch("scripts.on_demand_settings.read",
                        return_value={"enabled": True, "idle_minutes": 30}),
                  patch.object(wizard, "on_demand_runtime_status",
                               return_value=runtime)):
                report = wizard.on_demand_status(inventory=inventory)
            self.assertEqual(report["state"], state)
            self.assertTrue(report["enabled"])
            self.assertFalse(report["can_activate"])
            self.assertIn("read-only", report["activation_note"])
            self.assertNotIn("Old pre-activation check", report["next_gates"])
            self.assertNotIn("Potential dedicated deployment", report["reason"])

    def test_live_state_takes_priority_over_lagging_verified_docker_inventory(self):
        def inventory(state, running, stopped):
            return {"state": state, "managed_running": running,
                    "managed_stopped": stopped,
                    "other_published_crawlers": 0, "unverified_project_containers": 0,
                    "verified_services": {"crawl4ai": 1, "searxng": 1},
                    "verified_restart_policies": {
                        "crawl4ai": "no", "searxng": "no"}}

        def live(crawler, search):
            return {"mode": "on-demand", "enabled": True, "idle_minutes": 30,
                    "backends": {"crawl4ai": {"state": crawler},
                                 "searxng": {"state": search}}}

        cases = (
            (inventory("stopped", 0, 2), live("running", "running"), "running"),
            (inventory("running", 2, 0), live("sleeping", "sleeping"), "sleeping"),
            (inventory("stopped", 0, 2),
             live("sleeping", "running"), "partially_running"),
            (inventory("partially_running", 1, 1),
             live("waking", "running"), "transitioning"),
        )
        for docker, activity, expected in cases:
            with (self.subTest(expected=expected),
                  patch("scripts.on_demand_settings.read",
                        return_value={"enabled": True, "idle_minutes": 30}),
                  patch.object(wizard, "on_demand_runtime_status",
                               return_value=activity)):
                report = wizard.on_demand_status(inventory=docker)
            self.assertEqual(report["state"], expected)
            self.assertEqual(report["runtime_activity"], activity)
            self.assertFalse(report["can_activate"])

    def test_enabled_mode_never_hides_new_docker_ownership_blocker(self):
        blocked = {"state": "blocked",
                   "reason": "Another crawler publishes this port; never take over",
                   "next_gates": ["Resolve foreign crawler ownership"]}
        live = {"mode": "on-demand", "enabled": True, "idle_minutes": 30,
                "backends": {"crawl4ai": {"state": "running"},
                             "searxng": {"state": "running"}}}
        with (patch("scripts.on_demand_eligibility.eligibility",
                    return_value=blocked),
              patch("scripts.on_demand_settings.read",
                    return_value={"enabled": True, "idle_minutes": 30}),
              patch.object(wizard, "on_demand_runtime_status", return_value=live)):
            result = wizard.on_demand_status(
                inventory={"state": "running", "other_published_crawlers": 1})
        self.assertTrue(result["enabled"])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["reason"], blocked["reason"])
        self.assertEqual(result["next_gates"], blocked["next_gates"])
        self.assertEqual(result["runtime_activity"], live)

    def test_enabled_blocker_still_reports_independent_supervisor_failure(self):
        blocked = {"state": "blocked",
                   "reason": "Foreign crawler detected",
                   "next_gates": ["Resolve Docker ownership"]}
        runtime = {"mode": "on-demand", "enabled": True, "idle_minutes": 30,
                   "backends": {"crawl4ai": {"state": "failed"},
                                "searxng": {"state": "running"}}}
        with (patch("scripts.on_demand_eligibility.eligibility",
                    return_value=blocked),
              patch("scripts.on_demand_settings.read",
                    return_value={"enabled": True, "idle_minutes": 30}),
              patch.object(wizard, "on_demand_runtime_status", return_value=runtime)):
            report = wizard.on_demand_status(inventory={"state": "unverified"})
        self.assertEqual(report["state"], "blocked")
        self.assertIn("Foreign crawler detected", report["reason"])
        self.assertIn("supervisor also reports a failed backend", report["reason"])
        self.assertEqual(report["next_gates"], blocked["next_gates"])

    def test_dashboard_activity_rejects_unexpected_or_secret_responses(self):
        healthy = {"mode": "on-demand", "enabled": True, "idle_minutes": 30,
                   "backends": {
                       service: {"state": "running", "armed": True,
                                 "active_requests": 0,
                                 "last_activity_ago_seconds": 3.5,
                                 "next_idle_stop_seconds": 1796.5,
                                 "stop_claimed": False}
                       for service in ("crawl4ai", "searxng")}}
        cases = (
            (200, healthy, "ok"),
            (200, {**healthy, "backends": {
                **healthy["backends"],
                "crawl4ai": {**healthy["backends"]["crawl4ai"],
                             "last_activity_ago_seconds": 86400 * 9.5}}}, "ok"),
            (500, {"TOKEN": "SECRET_VALUE"}, "unavailable"),
            (200, {**healthy, "backends": {
                **healthy["backends"],
                "crawl4ai": {**healthy["backends"]["crawl4ai"],
                             "state": "SECRET_VALUE"}}}, "unavailable"),
            (200, {**healthy, "backends": {
                **healthy["backends"],
                "searxng": {**healthy["backends"]["searxng"],
                             "active_requests": "SECRET_VALUE"}}}, "unavailable"),
            (200, {**healthy, "backends": {
                **healthy["backends"],
                "searxng": {**healthy["backends"]["searxng"],
                             "last_activity_ago_seconds": -1}}}, "unavailable"),
            (200, {**healthy, "backends": {
                **healthy["backends"],
                "searxng": {**healthy["backends"]["searxng"],
                             "last_activity_ago_seconds": float("nan")}}}, "unavailable"),
        )
        for status, payload, outcome in cases:
            with self.subTest(outcome=outcome, status=status):
                response = Mock(status=status)
                response.read.return_value = json.dumps(payload).encode()
                connection = Mock()
                connection.getresponse.return_value = response
                with patch.object(wizard, "HTTPConnection", return_value=connection):
                    report = wizard.on_demand_runtime_status()
                connection.request.assert_called_once_with(
                    "GET", "/on-demand-state", headers={
                        "Host": "127.0.0.1:8791",
                        "Origin": "http://127.0.0.1:8791"})
                connection.close.assert_called_once_with()
                if outcome == "ok":
                    self.assertEqual(report, payload)
                else:
                    self.assertEqual(report["state"], "unavailable")
                    self.assertNotIn("SECRET_VALUE", json.dumps(report))

    def test_dry_run_does_not_claim_on_demand_is_unimplemented(self):
        status, output = self.call("--dry-run")
        self.assertEqual(status, 0)
        report = json.loads(output)
        self.assertIn("implemented", report["limitations"])
        self.assertNotIn("not implemented", report["limitations"])
        self.assertIn("on-demand", report["limitations"])

    def test_on_demand_preflight_error_is_redacted_and_fail_closed(self):
        with (patch("scripts.on_demand_eligibility.eligibility",
                    side_effect=OSError("TOKEN=sensitive"))) as _:
            result = wizard.on_demand_status(inventory={})
        self.assertEqual(result["state"], "blocked")
        self.assertFalse(result["can_activate"])
        self.assertNotIn("sensitive", json.dumps(result))

    def test_optional_client_guides_are_read_only_and_local_only(self):
        for client, hints in (
            ("cursor", ("~/.cursor/mcp.json", "mcpServers", "Grok",
                        "http://127.0.0.1:8790/mcp", "cloud bot")),
            ("hermes", ("hermes mcp add local-search --url",
                        "hermes mcp test local-search", "/reload-mcp",
                        "http://127.0.0.1:8790/mcp")),
            ("claude", ("Claude Code CLI", "claude mcp list",
                        "claude mcp add --transport http local-search",
                        "http://127.0.0.1:8790/mcp", "within Claude Code use /mcp",
                        "replace the example name with an unused",
                        "--scope user", "Claude.ai", "HTTPS"))):
            with (self.subTest(client=client),
                  patch.object(wizard, "run_existing",
                               side_effect=AssertionError("No launched process")),
                  patch.object(wizard, "health",
                               side_effect=AssertionError("No network health probe")),
                  patch.object(wizard, "docker_status",
                               side_effect=AssertionError("No Docker probe"))):
                status, output = self.call("--client", client)
            self.assertEqual(status, 0)
            for hint in hints:
                self.assertIn(hint, output)
            self.assertIn("No client configuration", output)
            self.assertNotIn("Authorization: Bearer", output)
            self.assertIn("unused", output)
            if client == "cursor":
                self.assertIn('{"mcpServers": {}}', output)
                self.assertIn('If mcpServers is missing', output)
                self.assertIn('"local-search": {', output)

    def test_claude_is_manual_opt_in_no_config_or_subprocess_side_effects(self):
        with (patch.object(wizard, "run_existing",
                           side_effect=AssertionError("No shell commands")),
              patch.object(wizard, "docker_status",
                           side_effect=AssertionError("No Docker access")),
              patch.object(wizard, "check_report",
                           side_effect=AssertionError("No network access")),
              patch.object(wizard.os, "geteuid",
                           side_effect=AssertionError("No privilege checks"))):
            result, output = self.call("--client", "claude")
        self.assertEqual(result, 0)
        self.assertIn("Local-scope registration", output)
        self.assertIn("verify that name with claude mcp list", output)
        self.assertIn("not Claude.ai cloud", output)
        self.assertNotIn("Authorization: Bearer", output)
        self.assertIn("No client configuration", output)

        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["12", "claude", "0"]),
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("No program invocation")),
              contextlib.redirect_stdout(io.StringIO()) as captured):
            self.assertEqual(wizard.main([]), 0)
        self.assertIn("claude mcp add --transport http", captured.getvalue())

    def test_only_one_client_can_be_selected(self):
        with self.assertRaises(SystemExit):
            wizard.make_parser().parse_args(["--client", "cursor", "--chatgpt"])
        with self.assertRaises(SystemExit):
            wizard.make_parser().parse_args(["--client", "remote-grok"])

    def test_optional_client_menu_does_not_write_without_selection(self):
        with (patch.object(wizard.sys, "stdin", Mock(isatty=lambda: True)),
              patch.object(wizard.os, "geteuid", return_value=1000),
              patch("builtins.input", side_effect=["12", "invalid", "0"]),
              patch.object(wizard, "run_existing",
                           side_effect=AssertionError("No program execution")),
              contextlib.redirect_stdout(io.StringIO()) as captured):
            self.assertEqual(wizard.main([]), 0)
        self.assertIn("Cancelled; no client settings changed", captured.getvalue())

    def test_other_clients_not_silently_installed(self):
        rc, message = self.call("--chatgpt")
        self.assertEqual(rc, 0)
        self.assertIn("manually", message)
        self.assertIn("No settings for Cursor, Claude, or Hermes", message)


if __name__ == "__main__":
    unittest.main()
