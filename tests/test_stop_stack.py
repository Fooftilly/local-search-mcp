"""Offline tests: shutdown touches only verified local project resources."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import stop_stack


def process(command, code=0, stdout=""):
    return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="PRIVATE_TOKEN")


class StopStackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.commands = []
        self.units = self.base / "systemd" / "user"
        self.units.mkdir(parents=True)
        self.compose_dir = self.base / "local-search-mcp" / "crawler-compose"
        self.compose_dir.mkdir(parents=True, mode=0o700)
        (self.compose_dir / "compose.yaml").write_text("name: local-search-crawler\n")
        self.containers = {"searxng": "searx-id", "crawl4ai": "crawl-id"}

    def own_units(self):
        for name in stop_stack.UNITS:
            (self.units / name).write_text(stop_stack.MARKER + "\n[Unit]\n")

    def fake_run(self, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(kwargs["capture_output"], True)
        self.assertEqual(kwargs["check"], False)
        if command[:2] == ["systemctl", "--system"]:
            return process(command, 1)
        if command[:2] == ["systemctl", "--user"]:
            if "is-active" in command:
                return process(command, 3, "inactive\n")
            return process(command)
        if command[:2] == ["docker", "inspect"]:
            cid = command[-1]
            service = next((name for name, id_ in self.containers.items()
                            if id_ == cid), None)
            labels = {
                "com.docker.compose.service": service,
                "com.docker.compose.project": "local-search-crawler",
                "com.docker.compose.project.config_files":
                    str((self.compose_dir / "compose.yaml").resolve()),
            }
            return process(command, 0, json.dumps(labels))
        if command[:2] == ["docker", "compose"] and "ps" in command:
            service = command[-1]
            return process(command, 0, self.containers.get(service, "") + "\n"
                           if service in self.containers else "")
        if command[:2] == ["docker", "compose"] and "stop" in command:
            return process(command)
        if command[:2] == ["docker", "update"]:
            return process(command)
        raise AssertionError("Unexpected command: " + repr(command))

    def run_job(self, runner=None):
        with patch.object(stop_stack.subprocess, "run",
                          side_effect=runner or self.fake_run):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = stop_stack.Stop()
                result.user_units()
                if result.tunnel_safe:
                    result.compose()
                result.root_guard()
        return result, output.getvalue()

    def test_ownership_order_two_backends_restart_policy_before_stop(self):
        self.own_units()
        (self.compose_dir / "searxng.compose.yaml").write_text("services: {}\n")
        result, output = self.run_job()
        self.assertFalse(result.errors)
        self.assertNotIn("PRIVATE_TOKEN", output)
        cmds = self.commands
        self.assertLess(
            cmds.index(["systemctl", "--user", "disable", "--now",
                        "local-search-mcp-tunnel.service"]),
            next(i for i, c in enumerate(cmds) if c[:2] == ["docker", "compose"]))
        for service, ident in self.containers.items():
            update = cmds.index(["docker", "update", "--restart=no", ident])
            stop = next(i for i, command in enumerate(cmds)
                        if command[:2] == ["docker", "compose"]
                        and command[-1] == service and "stop" in command)
            self.assertLess(update, stop)
            self.assertTrue(any("searxng.compose.yaml" in " ".join(c)
                                for c in cmds if c[:2] == ["docker", "compose"]))
        self.assertFalse(any("down" in c or "rm" in c or "pull" in c
                             for command in cmds for c in command))
        self.assertFalse(any(c[:2] == ["sudo", "systemctl"] for c in cmds))

    def test_absent_optional_backend_and_partial_services_are_normal(self):
        self.containers = {}  # No service container has been created yet.
        result, _ = self.run_job()
        self.assertFalse(result.errors)
        self.assertFalse(any(c[:2] == ["docker", "update"] for c in self.commands))
        self.assertFalse(any("searxng" in c for command in self.commands
                             for c in command))

    def test_missing_compose_project_does_not_touch_unrelated_hermes(self):
        (self.compose_dir / "compose.yaml").unlink()
        self.compose_dir.rmdir()
        result, output = self.run_job()
        self.assertFalse(result.errors)
        self.assertIn("no local-search-mcp-managed project", output)
        self.assertFalse(any(c[0] == "docker" for c in self.commands))

    def test_foreign_or_unverified_container_is_never_stopped(self):
        original = self.fake_run

        def fake(command, **kw):
            result = original(command, **kw)
            if command[:2] == ["docker", "inspect"]:
                return process(command, 0, json.dumps({
                    "com.docker.compose.project": "hermes-web",
                    "com.docker.compose.service": "crawl4ai",
                    "com.docker.compose.project.config_files":
                        "/home/user/hermes-web/docker-compose.yml",
                }))
            return result

        result, _ = self.run_job(fake)
        self.assertTrue(result.errors)
        self.assertFalse(any(c[:2] == ["docker", "update"] or "stop" in c
                             for c in self.commands))

    def test_update_failure_prevents_stopping_that_backend(self):
        original = self.fake_run

        def fake(command, **kw):
            if command[:2] == ["docker", "update"]:
                self.commands.append(command)
                return process(command, 1)
            return original(command, **kw)

        result, output = self.run_job(fake)
        self.assertTrue(result.errors)
        self.assertIn("restart policy not fully disabled", output)
        self.assertFalse(any("stop" in c and c[:2] == ["docker", "compose"]
                             for c in self.commands))

    def test_untrusted_unit_file_never_stopped_and_blocks_backend_shutdown(self):
        self.own_units()
        (self.units / stop_stack.UNITS[0]).write_text("foreign service")
        result, _ = self.run_job()
        self.assertFalse(result.tunnel_safe)
        self.assertTrue(result.errors)
        self.assertFalse(any(c[0] == "docker" for c in self.commands))
        self.assertFalse(any(c[-1] == stop_stack.UNITS[0] and "disable" in c
                             for c in self.commands))

    def test_root_firewall_guard_requires_manual_privileged_shutdown(self):
        def fake(command, **kwargs):
            if command[:2] == ["systemctl", "--system"]:
                self.commands.append(command)
                return process(command, 0, "enabled\n")
            return self.fake_run(command, **kwargs)

        result, output = self.run_job(fake)
        self.assertTrue(result.errors)
        self.assertIn("firewall rules are retained", output)
        self.assertFalse(any(c[0] == "sudo" for c in self.commands))


if __name__ == "__main__":
    unittest.main()
