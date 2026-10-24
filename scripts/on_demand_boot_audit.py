"""Read-only host preflight for an explicitly configured on-demand reboot.

This never modifies systemd, Docker, sudoers or nftables, performs no sudo,
and never substitutes for a live crawler firewall probe or reboot test.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess

from scripts import backend_inventory, on_demand_settings

ROOT = Path(__file__).resolve().parents[1]
GUARD_SOURCE = ROOT / "scripts" / "crawler_egress_guard.py"
SERVICE_SOURCE = ROOT / "config" / "systemd-system" / "local-search-mcp-crawler-egress.service"
GUARD_TARGET = Path("/usr/local/libexec/local-search-mcp/crawler_egress_guard.py")
SERVICE_TARGET = Path("/etc/systemd/system/local-search-mcp-crawler-egress.service")
ROOT_SERVICE = "local-search-mcp-crawler-egress.service"
USER_SERVICES = ("local-search-mcp-bridge.service", "local-search-mcp-tunnel.service")
MAX_FILE = 256 * 1024
EXPECTED_EXEC = (
    "/usr/bin/python3 /usr/local/libexec/local-search-mcp/"
    "crawler_egress_guard.py --container crawl4ai --startup"
)


def _run(*args: str) -> str | None:
    """Only fixed, read-only systemd/loginctl queries; redact command errors."""
    if not args or args[0] not in ("systemctl", "loginctl"):
        raise ValueError("Unsupported audit command")
    try:
        value = subprocess.run(
            args, capture_output=True, text=True, timeout=8, check=False,
            env={**os.environ, "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"})
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    if value.returncode or len(value.stdout) > 8192:
        return None
    return value.stdout.strip()


def _reviewed_root_file(target: Path, source: Path, mode: int) -> bool:
    """Inspect without following symlinks; compare installed bytes to checkout."""
    fd = None
    try:
        for parent in target.parents:
            metadata = parent.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o022):
                return False
        expected = source.read_bytes()
        if not expected or len(expected) > MAX_FILE:
            return False
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        found = os.fstat(fd)
        if (not stat.S_ISREG(found.st_mode) or found.st_uid != 0
                or found.st_gid != 0 or stat.S_IMODE(found.st_mode) != mode
                or found.st_size != len(expected)):
            return False
        with os.fdopen(fd, "rb") as stream:
            fd = None
            installed = stream.read(MAX_FILE + 1)
        return hashlib.sha256(installed).digest() == hashlib.sha256(expected).digest()
    except (OSError, ValueError):
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _root_effective_start() -> bool:
    """Inspect the *effective* systemd command, not merely a drop-in file."""
    raw = _run("systemctl", "show", ROOT_SERVICE, "-p", "ExecStart", "--value")
    if raw is None or len(raw) > 8192:
        return False
    commands = re.findall(r"\{([^{}]*)\}", raw)
    if len(commands) != 1:
        return False
    fields = {}
    for item in commands[0].split(";"):
        key, sep, value = item.strip().partition("=")
        if sep:
            fields[key] = value.strip()
    return (fields.get("path") == "/usr/bin/python3"
            and fields.get("argv[]") == EXPECTED_EXEC
            and fields.get("ignore_errors") == "no")


def _is_enabled_and_active(service: str, *, user: bool) -> bool:
    prefix = ("systemctl", "--user") if user else ("systemctl",)
    return (_run(*prefix, "is-enabled", service) == "enabled"
            and _run(*prefix, "is-active", service) == "active")


def audit(*, inventory: dict | None = None, settings: dict | None = None) -> dict:
    """Static candidate gate only; does not claim that boot security is proven."""
    checks = {
        "on_demand_enabled": False,
        "dedicated_docker_ownership": False,
        "backends_have_no_boot_restart": False,
        "root_guard_matches_checkout": False,
        "root_service_matches_checkout": False,
        "root_service_effective_startup_gate": False,
        "root_firewall_service_enabled_active": False,
        "bridge_enabled_active": False,
        "tunnel_enabled_active": False,
        "user_linger_enabled": False,
        "tunnel_effective_gate": False,
    }
    try:
        selected = on_demand_settings.read() if settings is None else settings
        checks["on_demand_enabled"] = (
            type(selected) is dict and selected.get("enabled") is True
            and type(selected.get("idle_minutes")) is int
            and 1 <= selected["idle_minutes"] <= 1440)
    except (OSError, ValueError, TypeError):
        pass

    try:
        docker = backend_inventory.report() if inventory is None else inventory
        services = docker.get("verified_services")
        policies = docker.get("verified_restart_policies")
        checks["dedicated_docker_ownership"] = (
            docker.get("state") in ("running", "stopped", "partially_running")
            and type(docker.get("managed_running")) is int
            and type(docker.get("managed_stopped")) is int
            and docker["managed_running"] + docker["managed_stopped"] == 2
            and docker.get("unverified_project_containers") == 0
            and docker.get("other_published_crawlers") == 0
            and services == {"crawl4ai": 1, "searxng": 1})
        checks["backends_have_no_boot_restart"] = (
            checks["dedicated_docker_ownership"]
            and policies == {"crawl4ai": "no", "searxng": "no"})
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    checks["root_guard_matches_checkout"] = _reviewed_root_file(
        GUARD_TARGET, GUARD_SOURCE, 0o755)
    checks["root_service_matches_checkout"] = _reviewed_root_file(
        SERVICE_TARGET, SERVICE_SOURCE, 0o644)
    checks["root_service_effective_startup_gate"] = _root_effective_start()
    checks["root_firewall_service_enabled_active"] = _is_enabled_and_active(
        ROOT_SERVICE, user=False)
    checks["bridge_enabled_active"] = _is_enabled_and_active(
        USER_SERVICES[0], user=True)
    checks["tunnel_enabled_active"] = _is_enabled_and_active(
        USER_SERVICES[1], user=True)
    try:
        account = pwd.getpwuid(os.getuid()).pw_name
        checks["user_linger_enabled"] = (
            _run("loginctl", "show-user", account, "-p", "Linger", "--value")
            == "yes")
    except (KeyError, OSError):
        pass
    try:
        from scripts import install_secure_tunnel
        install_secure_tunnel._verify_effective_tunnel_probe()
        checks["tunnel_effective_gate"] = True
    except (OSError, ValueError, RuntimeError):
        pass

    ready = all(checks.values())
    return {
        "mode": "read-only",
        "state": "ready_for_manual_reboot_test" if ready else "blocked",
        "checks": checks,
        "safe_to_reboot_proven": False,
        "note": (
            "Static checks only: neither nftables runtime enforcement nor "
            "cross-manager boot ordering or successful cold wake is proven. "
            "Review the sudoers policy, run the guarded live probe and perform "
            "an attended reboot test before unattended use."),
        "next_steps": (
            "See docs/EGRESS-SYSTEMD.md for operator-reviewed privileged "
            "migration and ON-DEMAND.md for controlled host reboot testing."),
    }
