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

    def test_initial_docker_unavailability_recovers_without_waking(self):
        """A pre-Docker user bridge start must not leave an enduring false failure."""
        available = [False]

        def inspect(name):
            if name == "crawl4ai" and not available[0]:
                raise LifecycleError("Docker not ready")
            return Target(name, "a" * 64, False, "no")

        with (patch.object(sup.docker, "_safe_target", side_effect=inspect),
              patch.object(sup.threading.Thread, "start")):
            manager = sup.Supervisor(1)
        self.assertEqual(manager.snapshot()["crawl4ai"]["state"], "unverified")
        self.assertEqual(manager.snapshot()["searxng"]["state"], "sleeping")

        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.docker, "_safe_target", side_effect=inspect),
              patch.object(sup.docker, "start",
                           side_effect=AssertionError("Passive startup retry woke Docker")),
              patch.object(sup.docker, "stop",
                           side_effect=AssertionError("Passive startup retry stopped Docker"))):
            manager.idle_once()
            self.assertEqual(manager.snapshot()["crawl4ai"]["state"], "unverified")
            available[0] = True
            manager.idle_once()
        self.assertEqual(manager.snapshot()["crawl4ai"]["state"], "sleeping")
        self.assertFalse(manager.snapshot()["crawl4ai"]["armed"])

    def test_boot_reconcile_arms_verified_already_running_backend(self):
        calls = [0]

        def inspect(name):
            if name == "crawl4ai" and calls[0] == 0:
                calls[0] += 1
                raise LifecycleError("Docker not ready")
            return Target(name, "a" * 64, name == "crawl4ai", "no")

        with (patch.object(sup.docker, "_safe_target", side_effect=inspect),
              patch.object(sup.threading.Thread, "start")):
            manager = sup.Supervisor(1)
        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.docker, "_safe_target", side_effect=inspect),
              patch.object(sup.docker, "start",
                           side_effect=AssertionError("Passive retry must not start Docker")),
              patch.object(sup.docker, "stop",
                           side_effect=AssertionError("Passive retry must not stop Docker"))):
            manager.idle_once()
        self.assertEqual(manager.snapshot()["crawl4ai"]["state"], "running")
        self.assertTrue(manager.snapshot()["crawl4ai"]["armed"])
        # Passive inventory does not grant the 10-second security verification
        # lease: first actual MCP request must still invoke guarded docker.start.
        self.assertEqual(manager._verified_at["crawl4ai"], 0.0)
        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.time, "monotonic", return_value=5.0),
              patch.object(sup.docker, "start", return_value=False) as guarded):
            with manager.lease("crawl4ai"):
                pass
        self.assertEqual(guarded.call_count, 1)
        self.assertIs(guarded.call_args.kwargs["crawler_probe"], sup._probe_crawler)

    def test_actual_wake_failure_not_passively_cleared(self):
        manager = self.new_supervisor()
        manager._state["crawl4ai"] = "failed"
        manager.tracker.disarm("crawl4ai")
        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.docker, "_safe_target",
                           side_effect=AssertionError("Real failure must not be auto-cleared")),
              patch.object(sup.docker, "start",
                           side_effect=AssertionError("No autonomous wake")),
              patch.object(sup.docker, "stop",
                           side_effect=AssertionError("No autonomous stop"))):
            manager.idle_once()
        self.assertEqual(manager.snapshot()["crawl4ai"]["state"], "failed")

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
                self.assertFalse(any(c.args == ("searxng",) for c in
                                     stopper.call_args_list))
                self.assertEqual(manager.snapshot()["searxng"]["active_requests"], 1)
            starter.assert_called_once()
            manager.idle_once()
            self.assertFalse(any(c.args == ("searxng",) for c in
                                 stopper.call_args_list))
            self.now[0] += 60
            manager.idle_once()
            stopper.assert_any_call("searxng")
            self.assertEqual(manager.snapshot()["searxng"]["state"], "sleeping")

    def test_quick_concurrent_requests_share_wake_and_probe(self):
        manager = self.new_supervisor()
        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.docker, "start", return_value=False) as starter):
            with manager.lease("crawl4ai"):
                pass
            with manager.lease("crawl4ai"):
                pass
            starter.assert_called_once()
            manager._verified_at["crawl4ai"] = 0.0
            with manager.lease("crawl4ai"):
                pass
            self.assertEqual(starter.call_count, 2)

    def test_warm_request_does_not_take_cross_backend_docker_lock(self):
        manager = self.new_supervisor()
        manager._verified_at["searxng"] = sup.time.monotonic()
        with (patch.object(manager, "_enabled", return_value=True),
              patch.object(sup.coordination, "exclusive",
                           side_effect=AssertionError("No global lock for warm work")),
              patch.object(sup.docker, "start",
                           side_effect=AssertionError("No warm Docker start"))):
            with manager.lease("searxng"):
                self.assertEqual(manager.snapshot()["searxng"]["active_requests"], 1)
        self.assertEqual(manager.snapshot()["searxng"]["active_requests"], 0)

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

    def test_prestart_guard_uses_distinct_fixed_privileged_unit(self):
        response = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(sup.subprocess, "run", return_value=response) as command:
            self.assertTrue(sup._preflight_crawler())
        self.assertEqual(command.call_args.args[0],
                         ["systemctl", "--user", "start", sup.PREFLIGHT_UNIT])
        self.assertFalse(command.call_args.kwargs.get("shell", False))
        with patch.object(sup.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, "", "private")):
            self.assertFalse(sup._preflight_crawler())

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
