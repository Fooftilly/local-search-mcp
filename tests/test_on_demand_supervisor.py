"""Mocked process supervisor / security boundary regressions; never use Docker."""
from __future__ import annotations

from contextlib import contextmanager
import subprocess
import unittest
from unittest.mock import patch

from scripts import on_demand_supervisor as sup
from scripts.on_demand_docker import Target, LifecycleError


class OnDemandSupervisorTests(unittest.TestCase):
    def new_supervisor(self):
        def fake_target(name):
            return Target(name, "a" * 64, True, "unless-stopped")
        with (patch.object(sup.docker, "_safe_target", side_effect=fake_target),
              patch.object(sup.threading.Thread, "start")):
            manager = sup.Supervisor(1)
        self.now = [1000.0]
        manager.tracker._clock = lambda: self.now[0]
        for name in ("crawl4ai", "searxng"):
            manager.tracker.disarm(name)
            manager.tracker.arm_running(name)
        return manager

    def test_disabled_mode_never_instantiates_supervisor(self):
        with (patch.object(sup.settings, "read",
                           return_value={"enabled": False, "idle_minutes": 30}),
              patch.object(sup, "Supervisor",
                           side_effect=AssertionError("No lifecycle activation"))):
            with sup.lease("searxng"):
                pass

    def test_concurrent_real_work_prevents_idle_stop(self):
        manager = self.new_supervisor()
        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.docker, "start", return_value=False) as starter,
              patch.object(sup.docker, "stop") as stopper):
            with manager.lease("searxng"):
                self.now[0] += 300
                manager.idle_once()
                stopper.assert_not_called()
                self.assertEqual(manager.snapshot()["searxng"]["active_requests"], 1)
            starter.assert_called_once()
            manager.idle_once()
            stopper.assert_not_called()
            self.now[0] += 60
            manager.idle_once()
            stopper.assert_any_call("searxng")
            self.assertEqual(manager.snapshot()["searxng"]["state"], "sleeping")

    def test_settings_disable_suppresses_stop_before_mutation(self):
        manager = self.new_supervisor()
        self.now[0] += 100
        with (patch.object(manager, "_enabled", return_value=False),
              patch.object(sup.docker, "stop",
                           side_effect=AssertionError("No docker on full stop"))):
            manager.idle_once()
        self.assertFalse(manager.snapshot()["crawl4ai"]["armed"])

    def test_crawler_guard_is_mandatory_and_failure_not_success(self):
        manager = self.new_supervisor()
        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.docker, "start",
                           side_effect=LifecycleError("private credentials"))):
            with self.assertRaises(sup.BackendUnavailable) as error:
                with manager.lease("crawl4ai"):
                    pass
        self.assertNotIn("private credentials", str(error.exception))
        self.assertEqual(manager.snapshot()["crawl4ai"]["active_requests"], 0)
        self.assertEqual(manager.snapshot()["crawl4ai"]["state"], "failed")

    def test_fixed_guard_service_never_executes_from_git_or_shell(self):
        response = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(sup.subprocess, "run", return_value=response) as command:
            self.assertTrue(sup._probe_crawler())
        self.assertEqual(command.call_args.args[0],
                         ["systemctl", "--user", "start", sup.PROBE_UNIT])
        self.assertFalse(command.call_args.kwargs.get("shell", False))

    def test_probe_failure_fails_closed(self):
        response = subprocess.CompletedProcess([], 1, "", "secret-token")
        with patch.object(sup.subprocess, "run", return_value=response):
            self.assertFalse(sup._probe_crawler())

    def test_local_status_does_not_start_supervisor(self):
        with (patch.object(sup, "_supervisor", None),
              patch.object(sup.settings, "read",
                           return_value={"enabled": False, "idle_minutes": 30}),
              patch.object(sup, "Supervisor",
                           side_effect=AssertionError("No background thread"))):
            result = sup.local_status()
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["backends"])


if __name__ == "__main__":
    unittest.main()
