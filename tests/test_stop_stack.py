"""Offline shutdown verification; never invoke real Docker or systemd."""
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


def response(argv, rc=0, output=""):
    return subprocess.CompletedProcess(argv, rc, stdout=output, stderr="PRIVATE_SECRET")


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.units = self.home / "systemd" / "user"
        self.units.mkdir(parents=True)
        self.dir = self.home / "local-search-mcp" / "crawler-compose"
        self.dir.mkdir(parents=True, mode=0o700)
        (self.dir / "compose.yaml").write_text("name: local-search-crawler\n")
        self.containers = {"crawl4ai": "crawl-id"}
        self.calls = []

    def install_units(self):
        for name in stop_stack.UNITS:
            (self.units / name).write_text(stop_stack.MARKER + "\n[Unit]\n")

    def fake_run(self, argv, **kw):
        self.calls.append(list(argv))
        self.assertTrue(kw["capture_output"])
        self.assertFalse(kw["check"])
        self.assertNotIn("DOCKER_CONTEXT", kw["env"])
        self.assertNotIn("COMPOSE_PROJECT_NAME", kw["env"])
        if argv[0] == "systemctl":
            if "show" in argv:
                name = argv[3]
                root = argv[1] == "--system"
                installed = not root and (self.units / name).is_file()
                return response(argv, 0, "LoadState=" +
                                ("loaded" if installed else "not-found") +
                                "\nActiveState=inactive\nUnitFileState=" +
                                ("disabled" if installed else "") + "\n")
            return response(argv)
        if argv[0] == "docker":
            self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
            if argv[3] == "ps":
                return response(argv, 0, "\n".join(self.containers.values()) + "\n")
            if argv[3] == "inspect":
                ident = argv[-1]
                if "State.Running" in " ".join(argv):
                    return response(argv, 0, "false no\n")
                service = next((s for s, cid in self.containers.items() if cid == ident), "")
                paths = [str((self.dir / "compose.yaml").resolve())]
                if service == "searxng":
                    paths.append(str((self.dir / "searxng.compose.yaml").resolve()))
                return response(argv, 0, json.dumps({
                    "com.docker.compose.project": stop_stack.PROJECT,
                    "com.docker.compose.service": service,
                    "com.docker.compose.project.config_files": ",".join(paths),
                }))
            if argv[3] in ("update", "stop"):
                return response(argv)
        raise AssertionError("Unexpected command " + repr(argv))

    def job(self, run=None):
        out = io.StringIO()
        with (patch.object(stop_stack.subprocess, "run", side_effect=run or self.fake_run),
              contextlib.redirect_stdout(out)):
            job = stop_stack.Stop()
            job.user_units()
            if job.tunnel_safe:
                job.compose()
            else:
                job.error("Docker backends not touched: tunnel shutdown unverified")
            job.root_guard()
        return job, out.getvalue()

    def test_owned_units_and_exact_containers_shutdown_in_safe_order(self):
        self.install_units()
        (self.dir / "searxng.compose.yaml").write_text("services: {}\n")
        self.containers["searxng"] = "searx-id"
        job, output = self.job()
        self.assertFalse(job.errors)
        self.assertTrue(job.tunnel_safe)
        self.assertNotIn("PRIVATE_SECRET", output)
        updater = next(i for i, c in enumerate(self.calls)
                       if c[:4] == ["systemctl", "--user", "stop",
                                   "local-search-mcp-update.service"])
        tunnel = next(i for i, c in enumerate(self.calls)
                      if c[:4] == ["systemctl", "--user", "disable", "--now"]
                      and c[-1] == stop_stack.TUNNEL)
        docker = next(i for i, c in enumerate(self.calls) if c[0] == "docker")
        self.assertLess(updater, tunnel)
        self.assertLess(tunnel, docker)
        for ident in self.containers.values():
            u = self.calls.index(["docker", "--host", "unix:///var/run/docker.sock",
                                  "update", "--restart=no", ident])
            s = self.calls.index(["docker", "--host", "unix:///var/run/docker.sock",
                                  "stop", "--time", "10", ident])
            self.assertLess(u, s)
        self.assertFalse(any("compose" in c or "down" in c or "prune" in c
                             or "--remove-orphans" in c for args in self.calls
                             for c in args))

    def test_missing_components_and_empty_project_are_harmless(self):
        self.containers = {}
        result, _ = self.job()
        self.assertFalse(result.errors)
        self.assertFalse(any("update" in args or "stop" in args
                             for args in self.calls if args[0] == "docker"))
        self.assertTrue(result.tunnel_safe)

    def test_removed_private_files_report_residual_containers(self):
        (self.dir / "compose.yaml").unlink()
        self.dir.rmdir()
        result, text = self.job()
        self.assertTrue(result.errors)
        self.assertIn("residual project containers", text)
        self.assertFalse(any(c[3] in ("stop", "update") for c in self.calls
                             if c[0] == "docker"))

    def test_foreign_project_overlay_labels_cannot_stop_containers(self):
        (self.dir / "searxng.compose.yaml").write_text("services: {}\n")
        self.containers = {"searxng": "searx-id"}
        def foreign(argv, **kw):
            result = self.fake_run(argv, **kw)
            if argv[0] == "docker" and argv[3] == "inspect":
                return response(argv, 0, json.dumps({
                    "com.docker.compose.service": "searxng",
                    "com.docker.compose.project": stop_stack.PROJECT,
                    "com.docker.compose.project.config_files":
                        str(self.dir / "compose.yaml") + ",/foreign/overlay.yml",
                }))
            return result
        result, _ = self.job(foreign)
        self.assertTrue(result.errors)
        self.assertFalse(any(c[3] in ("stop", "update") for c in self.calls
                             if c[0] == "docker"))

    def test_missing_searxng_overlay_reports_orphan(self):
        self.containers = {"searxng": "orphan"}
        result, _ = self.job()
        self.assertTrue(result.errors)
        self.assertFalse(any(c[3] in ("stop", "update") for c in self.calls
                             if c[0] == "docker"))

    def test_docker_restart_update_error_blocks_that_stop(self):
        def fails(argv, **kw):
            if argv[0] == "docker" and argv[3] == "update":
                self.calls.append(list(argv))
                return response(argv, 1)
            return self.run(argv, **kw)
        result, text = self.job(fails)
        self.assertTrue(result.errors)
        self.assertIn("failed to disable", text)
        self.assertFalse(any(c[3] == "stop" for c in self.calls
                             if c[0] == "docker"))

    def test_operational_systemctl_error_is_not_inactive_proof(self):
        def failed_bus(argv, **kw):
            if argv[0] == "systemctl" and "show" in argv:
                self.calls.append(list(argv))
                return response(argv, 1)
            return self.run(argv, **kw)
        result, _ = self.job(failed_bus)
        self.assertTrue(result.errors)
        self.assertFalse(result.tunnel_safe)
        self.assertFalse(any(c[0] == "docker" for c in self.calls))

    def test_non_owned_tunnel_never_disables_foreign_unit(self):
        self.install_units()
        (self.units / stop_stack.TUNNEL).write_text("Foreign\n")
        result, _ = self.job()
        self.assertTrue(result.errors)
        self.assertFalse(result.tunnel_safe)
        self.assertFalse(any(c[:3] == ["systemctl", "--user", "disable"]
                             and c[-1] == stop_stack.TUNNEL for c in self.calls))

    def test_root_guard_active_requires_separate_privileged_action(self):
        def enabled(argv, **kw):
            if argv[:2] == ["systemctl", "--system"] and "show" in argv:
                self.calls.append(list(argv))
                return response(argv, 0, "LoadState=loaded\nActiveState=active\n"
                                "UnitFileState=enabled\n")
            return self.run(argv, **kw)
        result, output = self.job(enabled)
        self.assertTrue(result.errors)
        self.assertIn("firewall rules are retained", output)
        self.assertFalse(any(c[0] == "sudo" for c in self.calls))

    def test_new_container_after_shutdown_is_detected(self):
        scans = [0]
        def race(argv, **kw):
            if argv[0] == "docker" and argv[3] == "ps":
                scans[0] += 1
                if scans[0] == 2:
                    self.calls.append(list(argv))
                    return response(argv, 0, "crawl-id\nrecreated-id\n")
            return self.run(argv, **kw)
        result, output = self.job(race)
        self.assertTrue(result.errors)
        self.assertIn("changed during shutdown", output)

    def test_tilde_xdg_config_home_is_expanded(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "~/fixture"}), patch.object(
                stop_stack.Path, "home", return_value=self.home):
            self.assertEqual(stop_stack.config_home(), self.home / "fixture")


if __name__ == "__main__":
    unittest.main()
