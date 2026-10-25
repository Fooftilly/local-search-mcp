#!/usr/bin/env python3
"""Read-only maintenance gate for project-owned systemd user units.

A pass is a momentary observation, not a lock or permission to change files.
It does not stop anything, execute source code, access Docker or restart units.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

if __package__:
    from . import stop_stack, on_demand_settings, rollback_candidate
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import stop_stack, on_demand_settings, rollback_candidate

# Only the project's known names. Never accept caller-supplied systemctl units.
UNITS = stop_stack.UNITS
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
PROPS = ("LoadState", "ActiveState", "UnitFileState",
         "FragmentPath", "DropInPaths", "NeedDaemonReload")
INACTIVE = ("inactive", "failed")
QUIET_UNIT_FILES = ("disabled", "masked", "static", "not-found", "")
MAX_OUTPUT = 8192


def _owned_unit(path: Path) -> bool:
    """Require the known installed unit file, not a symlink or foreign override."""
    if path.is_symlink():
        return False
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022):
            return False
        return stream.readline(256).rstrip(b"\r\n") == stop_stack.MARKER.encode()


def _verified_guard_dropin(path: Path) -> bool:
    """Accept only the repository-reviewed tunnel egress startup guard.

    Never authorize another drop-in, even one that affects only ExecStartPre.
    """
    expected = (Path(__file__).resolve().parents[1] / "config" / "systemd" /
                "guards" / "10-crawler-egress.conf")
    try:
        if path.is_symlink() or expected.is_symlink():
            return False
        if not _private_public_file(path):
            return False
        if not expected.is_file():
            return False
        # Compare bounded bytes; don't print the local path or template.
        return (path.stat().st_size <= 8192
                and path.read_bytes() == expected.read_bytes())
    except (OSError, ValueError):
        return False


def _private_public_file(path: Path) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and not info.st_mode & 0o022)


def _unit_state(name: str) -> dict[str, str]:
    command = ["systemctl", "--user", "show", name,
               *(f"--property={key}" for key in PROPS)]
    # No shell; captured diagnostics can contain local paths and are never emitted.
    result = subprocess.run(command, capture_output=True, check=False,
                            timeout=15)
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        raise ValueError("Systemd user-manager query unavailable")
    values = {}
    for line in result.stdout.decode("utf-8", "strict").splitlines():
        key, equals, value = line.partition("=")
        if not equals or key not in PROPS or key in values:
            raise ValueError("Unexpected systemd user-manager response")
        values[key] = value
    if set(values) != set(PROPS):
        raise ValueError("Incomplete systemd user-manager response")
    return values


def inspect(*, config_home: Path | None = None,
            candidate: Path | None = None) -> dict[str, object]:
    """Inspect only. Fail closed on foreign unit names, triggers and active work."""
    if os.geteuid() == 0:
        raise ValueError("Maintenance gate must not run as root")
    base = (config_home if config_home is not None else
            stop_stack.config_home()) / "systemd" / "user"
    blocked: list[str] = []
    managed = 0
    for name in UNITS:
        path = base / name
        try:
            state = _unit_state(name)
            installed = path.exists() or path.is_symlink()
            if installed:
                if not _owned_unit(path):
                    blocked.append(name + ": unverified installed unit")
                    continue
                managed += 1
                fragment = state["FragmentPath"]
                if (state["LoadState"] != "loaded" or not fragment
                        or Path(fragment).resolve(strict=True) != path.resolve(strict=True)):
                    blocked.append(name + ": different effective unit")
                    continue
            elif (state["LoadState"] != "not-found"
                  or state["FragmentPath"]):
                blocked.append(name + ": unmanaged unit is still loaded")
                continue
            if state["NeedDaemonReload"] != "no":
                blocked.append(name + ": systemd definition requires reload")
            if state["DropInPaths"]:
                guard = base / "local-search-mcp-tunnel.service.d" / "10-crawler-egress.conf"
                if (name != "local-search-mcp-tunnel.service"
                        or Path(state["DropInPaths"]).resolve() != guard.resolve()
                        or not _verified_guard_dropin(guard)):
                    blocked.append(name + ": unreviewed systemd drop-in")
            if state["ActiveState"] not in INACTIVE:
                blocked.append(name + ": not safely inactive")
            if state["UnitFileState"] not in QUIET_UNIT_FILES:
                blocked.append(name + ": unit could be started by systemd")
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            blocked.append(name + ": state/ownership could not be verified")
    try:
        if on_demand_settings.read()["enabled"]:
            blocked.append("on-demand wake remains enabled")
    except (OSError, ValueError, KeyError, TypeError):
        blocked.append("on-demand preference could not be verified")
    if candidate is not None:
        try:
            result = rollback_candidate.inspect(candidate)
            if result["ready_for_live_use"] is not False:
                blocked.append("candidate has unsupported readiness state")
        except (OSError, ValueError, RecursionError, KeyError):
            blocked.append("private rollback candidate failed verification")
    return {
        "ready_for_offline_maintenance": not blocked,
        "managed_units": managed,
        "checked_units": len(UNITS),
        "candidate_integrity_checked": candidate is not None,
        "blocked": blocked,
        "live_rollback_authorized": False,
        "services_changed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path,
                        help="Optional private candidate for integrity verification")
    args = parser.parse_args(argv)
    try:
        report = inspect(candidate=args.candidate)
    except (OSError, ValueError, subprocess.SubprocessError):
        print('{"ready_for_offline_maintenance": false, "error": "preflight unavailable"}',
              file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready_for_offline_maintenance"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
