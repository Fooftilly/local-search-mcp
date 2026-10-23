#!/usr/bin/env python3
"""Safely stop only local-search-mcp-owned user units and Compose services.

Does not remove nftables rules, images, containers, volumes, secrets or data.
Does not manage the external Hermes Web Docker Compose deployment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess

UNITS = (
    "local-search-mcp-tunnel.service",  # Remote entry must stop first.
    "local-search-mcp-update.path",
    "local-search-mcp-update.timer",
    "local-search-mcp-health.timer",
    "local-search-mcp-update.service",
    "local-search-mcp-health.service",
    "local-search-mcp-bridge.service",
)
MARKER = "# Managed by local-search-mcp; contains no credentials."
ROOT_GUARD = "local-search-mcp-crawler-egress.service"


class Stop:
    def __init__(self):
        self.errors: list[str] = []
        self.tunnel_safe = True

    def error(self, message: str) -> None:
        self.errors.append(message)
        print("ERROR: " + message)

    def run(self, command: list[str]) -> subprocess.CompletedProcess[str] | None:
        # Never print command stderr or stdout: Compose may include environment
        # values in diagnostics. Only fixed operation names are reported.
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  check=False, timeout=25)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.error(f"{command[0]} unavailable or timed out ({type(exc).__name__})")
            return None

    def user_units(self) -> None:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or
                    (Path.home() / ".config")) / "systemd" / "user"
        for name in UNITS:
            path = base / name
            if path.is_symlink():
                self.error(f"user unit {name}: symlink; ownership not verified")
                if name == UNITS[0]:
                    self.tunnel_safe = False
                continue
            if not path.exists():
                # A name collision or linked unit elsewhere can still be
                # active. Never stop a unit whose content is not ours.
                state = self.run(["systemctl", "--user", "is-active", name])
                if state is None or state.returncode == 0:
                    self.error(f"user unit {name}: not an installed managed file; inspect manually")
                    if name == UNITS[0]:
                        self.tunnel_safe = False
                else:
                    print(f"user unit {name}: not installed/active")
                continue
            if not path.is_file() or not path.read_text(
                    encoding="utf-8", errors="replace").startswith(MARKER):
                self.error(f"user unit {name}: missing managed marker; refused")
                if name == UNITS[0]:
                    self.tunnel_safe = False
                continue
            # Health/update services are triggered oneshots without [Install].
            # Their timers/paths are disabled above; stop the jobs themselves.
            one_shot = name in ("local-search-mcp-health.service",
                                "local-search-mcp-update.service")
            command = (["systemctl", "--user", "stop", name] if one_shot else
                       ["systemctl", "--user", "disable", "--now", name])
            result = self.run(command)
            if result is None or result.returncode:
                self.error(f"user unit {name}: stop/disable failed")
                if name == UNITS[0]:
                    self.tunnel_safe = False
                continue
            active = self.run(["systemctl", "--user", "is-active", name])
            if active is None or active.returncode == 0:
                self.error(f"user unit {name}: could not verify stopped")
                if name == UNITS[0]:
                    self.tunnel_safe = False
            else:
                print(f"user unit {name}: stopped" +
                      ("" if one_shot else " and disabled"))

    def compose(self) -> None:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or
                    (Path.home() / ".config")) / "local-search-mcp" / "crawler-compose"
        if not base.exists() and not base.is_symlink():
            print("Compose backends: no local-search-mcp-managed project installed")
            return
        if (base.is_symlink() or not base.is_dir() or base.stat().st_uid != os.getuid()
                or stat.S_IMODE(base.stat().st_mode) & 0o077):
            self.error("Compose backends: private project directory ownership/mode invalid")
            return
        crawler = base / "compose.yaml"
        if crawler.is_symlink() or not crawler.is_file():
            self.error("Compose backends: missing or linked compose.yaml")
            return
        overlay = base / "searxng.compose.yaml"
        if overlay.is_symlink() or (overlay.exists() and not overlay.is_file()):
            self.error("Compose backends: unsafe optional SearXNG overlay")
            return
        files = [crawler] + ([overlay] if overlay.exists() else [])
        compose = ["docker", "compose", "--project-directory", str(base)]
        for file in files:
            compose += ["-f", str(file)]
        for service in (["searxng"] if overlay.exists() else []) + ["crawl4ai"]:
            lookup = self.run(compose + ["ps", "--all", "-q", service])
            if lookup is None or lookup.returncode:
                self.error(f"Compose {service}: cannot list project containers")
                continue
            ids = [line.strip() for line in lookup.stdout.splitlines() if line.strip()]
            if not ids:
                print(f"Compose {service}: no managed container present")
                continue
            valid = True
            for container_id in ids:
                info = self.run(["docker", "inspect", "--type=container",
                                 "--format", "{{json .Config.Labels}}", container_id])
                if info is None or info.returncode:
                    self.error(f"Compose {service}: unable to verify container ownership")
                    valid = False
                    continue
                try:
                    labels = json.loads(info.stdout)
                    paths = labels["com.docker.compose.project.config_files"].split(",")
                    owned = (labels["com.docker.compose.service"] == service
                             and str(crawler.resolve()) in paths
                             and labels.get("com.docker.compose.project"))
                except (ValueError, TypeError, AttributeError, KeyError):
                    owned = False
                if not owned:
                    self.error(f"Compose {service}: foreign/unverifiable container refused")
                    valid = False
            if not valid:
                continue
            # Update every verified container before stopping the service.
            # This prevents Docker daemon reboot from restarting it.
            for container_id in ids:
                changed = self.run(["docker", "update", "--restart=no", container_id])
                if changed is None or changed.returncode:
                    self.error(f"Compose {service}: failed to disable Docker restart policy")
                    valid = False
            if not valid:
                print(f"Compose {service}: stop skipped (restart policy not fully disabled)")
                continue
            result = self.run(compose + ["stop", "--timeout", "10", service])
            if result is None or result.returncode:
                self.error(f"Compose {service}: stop failed")
            else:
                print(f"Compose {service}: stopped; Docker restart disabled")

    def root_guard(self) -> None:
        # No sudo and no firewall operations in a regular shutdown.
        enabled = self.run(["systemctl", "--system", "is-enabled", ROOT_GUARD])
        active = self.run(["systemctl", "--system", "is-active", ROOT_GUARD])
        if ((enabled and enabled.returncode == 0)
                or (active and active.returncode == 0)):
            self.error("root egress guard requires separate administrator shutdown; "
                       "firewall rules are retained")
        else:
            print("root egress guard: no active/enabled unit detected; firewall unchanged")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.error("Run as the normal user; root-managed guard needs separate approval")
    job = Stop()
    job.user_units()
    if job.tunnel_safe:
        job.compose()
    else:
        job.error("Docker backends not touched: tunnel shutdown could not be verified")
    job.root_guard()
    print(f"Shutdown: {len(job.errors)} incomplete step(s). "
          "Configuration, history, secrets, containers, volumes and firewall preserved.")
    return int(bool(job.errors))


if __name__ == "__main__":
    raise SystemExit(main())
