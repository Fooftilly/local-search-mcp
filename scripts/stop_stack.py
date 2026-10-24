#!/usr/bin/env python3
"""Stop only verified local-search-mcp resources; preserve all private state."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

if __package__:
    from . import on_demand_settings
else:
    # stop.sh intentionally launches this script by path, placing only
    # scripts/ on sys.path. Restore the repository root for package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import on_demand_settings

PROJECT = "local-search-crawler"
ROOT_GUARD = "local-search-mcp-crawler-egress.service"
MARKER = "# Managed by local-search-mcp; contains no credentials."
DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")

# Disable triggers before stopping a currently running update checker; it can
# restart the tunnel. The remote entry point is then disabled and verified.
UNITS = (
    "local-search-mcp-update.path",
    "local-search-mcp-update.timer",
    "local-search-mcp-health.timer",
    "local-search-mcp-update.service",
    "local-search-mcp-health.service",
    "local-search-mcp-tunnel.service",
    "local-search-mcp-bridge.service",
    "local-search-mcp-guard-probe.service",
)
TUNNEL = "local-search-mcp-tunnel.service"
CHECKERS = {"local-search-mcp-health.service", "local-search-mcp-update.service"}


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or
                str(Path.home() / ".config")).expanduser()


class Stop:
    def __init__(self):
        self.errors: list[str] = []
        self.tunnel_safe = False

    def inhibit_on_demand(self) -> None:
        """Disarm future wake before stopping any bridge/tunnel services."""
        try:
            saved = on_demand_settings.read()
            if saved["enabled"]:
                on_demand_settings.save(enabled=False,
                                        idle_minutes=saved["idle_minutes"])
                print("On-demand wake: disabled before full stop")
        except (OSError, ValueError):
            self.error("On-demand wake suppression could not be verified")

    def error(self, detail: str) -> None:
        self.errors.append(detail)
        print("ERROR: " + detail)

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        # No shell, no user-controlled arguments, no output of Docker/systemd
        # diagnostics (which might contain private configuration).
        environment = os.environ.copy()
        for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE",
                     "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "DOCKER_CONFIG"):
            environment.pop(name, None)
        try:
            return subprocess.run(argv, capture_output=True, text=True,
                                  timeout=30, check=False, env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.error(f"{argv[0]} unavailable or timed out ({type(exc).__name__})")
            return None

    def unit_state(self, name: str, *, system: bool = False) -> dict[str, str] | None:
        result = self.run(["systemctl", "--system" if system else "--user",
                           "show", name, "--property=LoadState",
                           "--property=ActiveState", "--property=UnitFileState"])
        if result is None or result.returncode:
            self.error(f"{name}: cannot verify systemd state")
            return None
        values = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        if (values.get("LoadState") not in ("loaded", "not-found", "masked")
                or values.get("ActiveState") not in
                ("inactive", "failed", "active", "activating", "deactivating", "reloading")):
            self.error(f"{name}: systemd returned unknown state")
            return None
        return values

    def user_units(self) -> None:
        base = config_home() / "systemd" / "user"
        for name in UNITS:
            path = base / name
            try:
                if path.is_symlink():
                    raise ValueError("linked unit is not verified")
                if not path.exists():
                    state = self.unit_state(name)
                    if (state is None or state["LoadState"] != "not-found"
                            or state["ActiveState"] not in ("inactive", "failed")):
                        self.error(f"user unit {name}: missing owned file or still active")
                    else:
                        print(f"user unit {name}: not installed")
                    continue
                if not path.is_file() or not path.read_text(
                        encoding="utf-8", errors="replace").startswith(MARKER):
                    raise ValueError("missing managed-unit marker")
            except (OSError, ValueError):
                self.error(f"user unit {name}: not a verifiable owned unit")
                continue
            mode = ["stop"] if name in CHECKERS else ["disable", "--now"]
            result = self.run(["systemctl", "--user", *mode, name])
            if result is None or result.returncode:
                self.error(f"user unit {name}: stop/disable failed")
                continue
            state = self.unit_state(name)
            if state is None or state["ActiveState"] not in ("inactive", "failed"):
                self.error(f"user unit {name}: could not verify stopped")
                continue
            if name not in CHECKERS and state.get("UnitFileState") not in (
                    "disabled", "masked", "not-found", ""):
                self.error(f"user unit {name}: not verified disabled")
                continue
            print(f"user unit {name}: stopped" +
                  ("" if name in CHECKERS else " and disabled"))

        # Stop and verify the tunnel again after all update/checker jobs.
        # A failed/foreign/missing tunnel check is never proof of isolation.
        path = base / TUNNEL
        try:
            owned = (not path.is_symlink() and path.is_file()
                     and path.read_text(encoding="utf-8",
                                        errors="replace").startswith(MARKER))
        except OSError:
            owned = False
        if owned:
            res = self.run(["systemctl", "--user", "disable", "--now", TUNNEL])
            if res is None or res.returncode:
                self.error("tunnel final stop/disable failed")
                return
        state = self.unit_state(TUNNEL)
        # LoadState=not-found can coexist with ActiveState=active when a
        # unit file is deleted/reloaded but its process survives. Also do not
        # proceed if an update/health trigger could not be stopped: it may
        # restart the tunnel after this final check.
        if (state is not None
                and state.get("ActiveState") in ("inactive", "failed")
                and not self.errors
                and ((owned and state.get("UnitFileState") in
                      ("disabled", "masked", "not-found", ""))
                     or (not owned and state.get("LoadState") == "not-found"))):
            self.tunnel_safe = True
        else:
            self.error("tunnel final state is unverified; backends will not be stopped")

    def project_containers(self) -> list[str] | None:
        # Use full container IDs: truncated display IDs are not stable
        # operation targets when another process removes/recreates containers.
        command = [*DOCKER, "ps", "--all", "--no-trunc", "--filter",
                   f"label=com.docker.compose.project={PROJECT}",
                   "--format", "{{.ID}}"]
        result = self.run(command)
        if result is None or result.returncode:
            self.error("Docker: cannot verify local Compose project inventory")
            return None
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def labels(self, container_id: str) -> dict | None:
        result = self.run([*DOCKER, "inspect", "--type=container",
                           "--format", "{{json .Config.Labels}}", container_id])
        if result is None or result.returncode:
            return None
        try:
            labels = json.loads(result.stdout)
            return labels if isinstance(labels, dict) else None
        except (ValueError, TypeError):
            return None

    def compose(self) -> None:
        # Scan the project EVEN WHEN config files have disappeared: a stale
        # container with restart:unless-stopped must not be reported as off.
        inventory = self.project_containers()
        if inventory is None:
            return
        root = config_home() / "local-search-mcp" / "crawler-compose"
        if not root.exists() and not root.is_symlink():
            if inventory:
                self.error("Compose: residual project containers exist without private config")
            else:
                print("Compose: no private project or residual containers")
            return
        try:
            if (root.is_symlink() or not root.is_dir()
                    or root.stat().st_uid != os.getuid()
                    or stat.S_IMODE(root.stat().st_mode) & 0o077):
                raise ValueError("invalid private directory")
            crawler = root / "compose.yaml"
            if crawler.is_symlink() or not crawler.is_file():
                raise ValueError("missing or linked crawler compose file")
            overlay = root / "searxng.compose.yaml"
            if overlay.is_symlink() or (overlay.exists() and not overlay.is_file()):
                raise ValueError("invalid optional overlay file")
            expected = {str(crawler.resolve())}
            if overlay.exists():
                expected.add(str(overlay.resolve()))
        except (OSError, ValueError):
            self.error("Compose: invalid/unsafe private project files")
            return
        if not inventory:
            print("Compose: no managed containers")
            return

        verified: list[str] = []
        for ident in inventory:
            label = self.labels(ident)
            if label is None:
                self.error("Compose: container labels could not be verified")
                continue
            service = label.get("com.docker.compose.service")
            try:
                raw_files = label["com.docker.compose.project.config_files"].split(",")
                files = set(raw_files)
            except (KeyError, AttributeError):
                raw_files, files = [], set()
            # A shared crawler file is insufficient evidence of ownership:
            # never touch a deployment with a foreign Compose override.
            # Crawl4AI may have been started alone before the optional overlay
            # was installed; SearXNG must reference both managed files.
            valid = (label.get("com.docker.compose.project") == PROJECT
                     and service in ("crawl4ai", "searxng")
                     and len(raw_files) == len(files)
                     and str(crawler.resolve()) in files
                     and files.issubset(expected)
                     and (service != "searxng"
                          or overlay.exists() and str(overlay.resolve()) in files))
            if not valid:
                self.error("Compose: foreign, orphaned or unverified project container")
                continue
            verified.append(ident)

        # Never apply a service-name stop that could select a freshly recreated,
        # never-verified container between inspection and execution.
        for ident in verified:
            changed = self.run([*DOCKER, "update", "--restart=no", ident])
            if changed is None or changed.returncode:
                self.error("Compose: failed to disable a container restart policy")
                continue
            stopped = self.run([*DOCKER, "stop", "--time", "10", ident])
            if stopped is None or stopped.returncode:
                self.error("Compose: failed to stop a verified container")
                continue
            state = self.run([*DOCKER, "inspect", "--type=container",
                              "--format",
                              "{{.State.Running}} {{.HostConfig.RestartPolicy.Name}}",
                              ident])
            if state is None or state.returncode or state.stdout.strip() != "false no":
                self.error("Compose: stopped/restart-disabled state unverified")
            else:
                print("Compose: verified container stopped, restart policy disabled")

        after = self.project_containers()
        if after is None or set(after) != set(inventory):
            self.error("Compose: project container list changed during shutdown")

    def root_guard(self) -> None:
        # root-owned oneshot should remain opt-in; never touch nftables here.
        state = self.unit_state(ROOT_GUARD, system=True)
        if state is None:
            return
        if ((state["LoadState"] == "not-found"
             and state["ActiveState"] in ("inactive", "failed"))
                or (state["ActiveState"] in ("inactive", "failed")
                    and state.get("UnitFileState") in ("disabled", "masked"))):
            print("root egress guard: no enabled/active unit; firewall unchanged")
        else:
            self.error("root egress guard needs separate administrator shutdown; "
                       "firewall rules are retained")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("Run as normal user; root guard requires separate authorization")
    job = Stop()
    job.inhibit_on_demand()
    job.user_units()
    if job.tunnel_safe:
        job.compose()
    else:
        job.error("Docker backends not touched: tunnel shutdown unverified")
    job.root_guard()
    print(f"Shutdown: {len(job.errors)} incomplete step(s). "
          "All project data, volumes and firewall rules retained.")
    return int(bool(job.errors))


if __name__ == "__main__":
    raise SystemExit(main())
