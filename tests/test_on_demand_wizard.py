"""No live Docker, root privileges or backend writes in setup tests."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import setup_wizard as wizard
from scripts import on_demand_settings, on_demand_docker, on_demand_supervisor
from scripts import install_user_services
from scripts import stop_stack


class SetupOnDemandTests(unittest.TestCase):
    def test_enable_requires_verified_running_pair_and_live_guard(self):
        inventory = {"state": "running", "managed_running": 2,
                     "managed_stopped": 0, "unverified_project_containers": 0,
                     "other_published_crawlers": 0,
                     "verified_services": {"crawl4ai": 1, "searxng": 1},
                     "verified_restart_policies": {
                         "crawl4ai": "unless-stopped", "searxng": "unless-stopped"}}
        def target(name):
            return on_demand_docker.Target(name, "a" * 64, True, "unless-stopped")
        with (patch.object(wizard, "docker_status", return_value=inventory),
              patch.object(on_demand_docker, "_safe_target", side_effect=target),
              patch.object(on_demand_supervisor, "_healthy", return_value=True),
              patch.object(on_demand_supervisor, "_probe_crawler",
                           return_value=True) as probe,
              patch.object(on_demand_settings, "save",
                           return_value={"enabled": True, "idle_minutes": 45}) as save):
            result = wizard.configure_on_demand(enabled=True, idle_minutes=45)
        self.assertTrue(result["enabled"])
        probe.assert_called_once_with()
        save.assert_called_once_with(enabled=True, idle_minutes=45)

    def test_shared_backends_cannot_enable_and_no_setting_is_written(self):
        with (patch.object(wizard, "docker_status",
                           return_value={"state": "not_started",
                                         "other_published_crawlers": 1}),
              patch.object(on_demand_settings, "save",
                           side_effect=AssertionError("No unsafe activation"))):
            with self.assertRaises(ValueError):
                wizard.configure_on_demand(enabled=True)

    def test_root_probe_failure_refuses_activation(self):
        candidate = {"state": "manual_verification_required"}
        with (patch("scripts.on_demand_eligibility.eligibility",
                    return_value=candidate),
              patch.object(wizard, "docker_status", return_value={}),
              patch.object(on_demand_docker, "_safe_target",
                           return_value=on_demand_docker.Target(
                               "crawl4ai", "a" * 64, True, "unless-stopped")),
              patch.object(on_demand_supervisor, "_healthy", return_value=True),
              patch.object(on_demand_supervisor, "_probe_crawler", return_value=False),
              patch.object(on_demand_settings, "save",
                           side_effect=AssertionError("No writes"))):
            with self.assertRaises(ValueError):
                wizard.configure_on_demand(enabled=True)

    def test_disable_saved_first_and_optional_wake_is_scoped(self):
        saves = []
        wake = []
        with (patch.object(on_demand_settings, "save",
                           side_effect=lambda **kw: saves.append(kw) or kw),
              patch.object(on_demand_docker, "start",
                           side_effect=lambda name, **kw: wake.append(name))):
            result = wizard.configure_on_demand(
                enabled=False, idle_minutes=30, wake_before_disable=True)
        self.assertFalse(result["enabled"])
        self.assertEqual(saves, [{"enabled": False, "idle_minutes": 30}])
        self.assertEqual(wake, ["searxng", "crawl4ai"])

    def test_setup_flags_require_explicit_confirmation(self):
        with patch.object(wizard, "configure_on_demand",
                          side_effect=AssertionError("No unauthorized config")):
            with self.assertRaises(SystemExit):
                wizard.main(["--on-demand-enable"])
        with (patch.object(wizard, "configure_on_demand",
                           return_value={"enabled": False, "idle_minutes": 30}) as mutate,
              patch.object(wizard.os, "geteuid", return_value=1000)):
            self.assertEqual(wizard.main([
                "--on-demand-disable", "--confirm-on-demand", "DISABLE"]), 0)
        mutate.assert_called_once_with(enabled=False, idle_minutes=30,
                                      wake_before_disable=False)

    def test_optional_probe_unit_is_fixed_and_not_default(self):
        default = install_user_services.unit_files("/usr/bin/python3", Path("/repo"))
        opted = install_user_services.unit_files("/usr/bin/python3", Path("/repo"),
                                                 guard_probe=True)
        self.assertNotIn(install_user_services.GUARD_PROBE, default)
        unit = opted[install_user_services.GUARD_PROBE]
        self.assertIn("--container crawl4ai --probe", unit)
        self.assertNotIn("ExecStart=/bin/sh", unit)

    def test_supported_stop_script_help_runs_by_path(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        response = subprocess.run(
            [sys.executable, str(root / "scripts" / "stop_stack.py"), "--help"],
            cwd=root, capture_output=True, text=True, timeout=10)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("usage:", response.stdout)

    def test_full_stop_disables_wake_before_any_services(self):
        events = []
        with (patch.object(stop_stack.on_demand_settings, "read",
                           return_value={"enabled": True, "idle_minutes": 60}),
              patch.object(stop_stack.on_demand_settings, "save",
                           side_effect=lambda **kw: events.append(("save", kw)))):
            stop_stack.Stop().inhibit_on_demand()
        self.assertEqual(events, [("save", {"enabled": False, "idle_minutes": 60})])


if __name__ == "__main__":
    unittest.main()
