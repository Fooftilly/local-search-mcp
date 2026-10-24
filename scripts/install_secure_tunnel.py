#!/usr/bin/env python3
"""Explicit, non-destructive protection of an existing dedicated ChatGPT tunnel.

This script must run as the regular operator. sudo is used only for copying
fixed reviewed files, validating the exact sudoers rule, running the installed
root-owned helper, and enabling the fixed firewall service. It never executes
an editable checkout as root. Preview has no sudo calls or side effects.
"""
from __future__ import annotations

import argparse
import pwd
import os
from pathlib import Path
import re
import subprocess
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Allow both python -m scripts.install_secure_tunnel and direct script launch
# by setup_wizard.run_existing(), without depending on ambient PYTHONPATH.
if not __package__:
    sys.path.insert(0, str(ROOT))
GUARD = ROOT / "scripts" / "crawler_egress_guard.py"
SERVICE = ROOT / "config" / "systemd-system" / "local-search-mcp-crawler-egress.service"
DROPIN = ROOT / "config" / "systemd" / "guards" / "10-crawler-egress.conf"
ROOT_GUARD = Path("/usr/local/libexec/local-search-mcp/crawler_egress_guard.py")
ROOT_SERVICE = Path("/etc/systemd/system/local-search-mcp-crawler-egress.service")
SUDOERS = Path("/etc/sudoers.d/local-search-mcp-crawler")
USER_TUNNEL = "local-search-mcp-tunnel.service"
USER_BRIDGE = "local-search-mcp-bridge.service"


class InstallError(RuntimeError):
    """Guarded setup cannot prove a safe installation."""


def _run(*args: str, timeout: int = 45, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(args), text=True, capture_output=True, check=False, timeout=timeout,
            env={**os.environ, "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError("Required setup command unavailable or timed out") from exc
    if check and result.returncode:
        # Do not include potentially sensitive stderr, sudo or Docker output.
        raise InstallError("Required setup command failed: " + args[0])
    return result


def _sudo(*args: str, timeout: int = 45, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run("sudo", *args, timeout=timeout, check=check)


def _existing_root(path: Path) -> bool:
    # A sudo failure can also exit 1. Prove privileged commands remain
    # authorized before interpreting test -e/-L exit 1 as "missing".
    _sudo("true")
    linked = _sudo("test", "-L", str(path), check=False)
    if linked.returncode == 0:
        raise InstallError("Existing privileged path is a symlink; refuse overwrite")
    if linked.returncode != 1:
        raise InstallError("Cannot safely inspect privileged path")
    found = _sudo("test", "-e", str(path), check=False)
    if found.returncode == 0:
        return True
    if found.returncode != 1:
        raise InstallError("Cannot safely inspect privileged path")
    _sudo("true")
    return False


def _root_mode(path: Path) -> int:
    """Read numeric, locale-invariant root-owned metadata from GNU stat.

    %f includes the file type bits (unlike %a) and does not depend on
    translated descriptions such as 'directory' or 'regular file'.
    """
    fields = _sudo("stat", "-c", "%u:%g:%f", str(path)).stdout.strip().split(":")
    if (len(fields) != 3 or fields[:2] != ["0", "0"]
            or re.fullmatch(r"[0-9a-fA-F]{4,8}", fields[2]) is None):
        raise InstallError("Privileged security-file metadata or ownership is unsafe")
    return int(fields[2], 16)


def _root_parent_safety(path: Path, *, allow_missing: bool = False) -> None:
    """Never install/execute a root helper through attacker-writable parents."""
    for parent in reversed(path.parents):
        if parent == Path("/"):
            continue
        if _existing_root(parent) is False:
            if allow_missing:
                continue
            raise InstallError("Privileged security-file parent missing")
        mode = _root_mode(parent)
        if not stat.S_ISDIR(mode):
            raise InstallError("Privileged security-file parent is not a directory")
        if stat.S_IMODE(mode) & 0o022:
            raise InstallError("Privileged security-file parent is writable by others")


def _install_root_file(source: Path, target: Path, permission: str) -> None:
    if not source.is_file() or source.is_symlink():
        raise InstallError("Repository security template missing or linked")
    _root_parent_safety(target, allow_missing=True)
    if _existing_root(target):
        same = _sudo("cmp", "-s", str(source), str(target), check=False)
        if same.returncode != 0:
            raise InstallError("Installed security file differs; review it manually before replacing")
        mode = _root_mode(target)
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != int(permission, 8):
            raise InstallError("Existing privileged file owner or mode is unsafe")
        _root_parent_safety(target)
        return
    _sudo("install", "-D", "-o", "root", "-g", "root", "-m", permission,
          str(source), str(target))
    _root_parent_safety(target)


def _sudoers_line() -> str:
    name = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name):
        raise InstallError("Username cannot be written safely in sudoers")
    fixed = (name + " ALL=(root) NOPASSWD: /usr/bin/python3 "
             "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py "
             "--container crawl4ai ")
    # Both operations are fixed, narrow and documented; preflight is used
    # only when explicitly enabling opt-in on-demand backends later.
    return fixed + "--probe\n" + fixed + "--preflight\n"


def _install_sudoers() -> None:
    expected = _sudoers_line()
    _root_parent_safety(SUDOERS)
    if _existing_root(SUDOERS):
        # The exact content is read as root but may contain only this fixed
        # nonsecret rule; never print arbitrary existing sudoers contents.
        actual = _sudo("cat", str(SUDOERS)).stdout
        if actual != expected:
            raise InstallError("Existing local-search-mcp sudoers differs; refuse overwrite")
        mode = _root_mode(SUDOERS)
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o440:
            raise InstallError("Existing sudoers has unsafe ownership or mode")
        _sudo("visudo", "-cf", str(SUDOERS))
        return

    # The parent is user-owned and private; the fixed rule is verified by
    # visudo BEFORE it can be installed with privileged ownership.
    with tempfile.TemporaryDirectory(prefix="lsm-sudoers-") as temp:
        path = Path(temp) / "policy"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o440)
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(expected)
        _sudo("visudo", "-cf", str(path))
        _sudo("install", "-o", "root", "-g", "root", "-m", "0440",
              str(path), str(SUDOERS))
    _sudo("visudo", "-cf", str(SUDOERS))


def _user_dropin() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or
                str(Path.home() / ".config")).expanduser()
    if not base.is_absolute():
        raise InstallError("XDG config root must be absolute")
    return base / "systemd" / "user" / (USER_TUNNEL + ".d") / DROPIN.name


def _install_user_dropin() -> None:
    if DROPIN.is_symlink() or not DROPIN.is_file():
        raise InstallError("Repository tunnel drop-in is missing or linked")
    content = DROPIN.read_bytes()
    target = _user_dropin()
    if target.is_symlink():
        raise InstallError("Existing tunnel drop-in is a symlink")
    if target.exists():
        if not target.is_file() or target.read_bytes() != content:
            raise InstallError("Existing tunnel drop-in differs; refuse overwrite")
        return
    if target.parent.is_symlink():
        raise InstallError("Existing tunnel drop-in directory is a symlink")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as out:
        out.write(content)


def _verify_backends() -> dict:
    from scripts import backend_inventory
    from health_cli import snapshot
    inventory = backend_inventory.report()
    if (inventory.get("state") != "running"
            or inventory.get("verified_services") != {"crawl4ai": 1, "searxng": 1}
            or inventory.get("managed_running") != 2
            or inventory.get("managed_stopped") != 0
            or inventory.get("unverified_project_containers") != 0
            or inventory.get("other_published_crawlers") != 0):
        raise InstallError("Dedicated project ownership/running backends not proven")
    result = snapshot()["checks"]
    if not all(result.get(key) is True for key in
               ("crawl4ai", "searxng", "mcp_listener", "dashboard_http")):
        raise InstallError("Required bridge and backends are not healthy")
    return inventory


def preview() -> dict:
    inventory = _verify_backends()
    if os.geteuid() == 0:
        raise InstallError("Run the wizard as the normal user")
    return {
        "mode": "read-only-preview", "client": "ChatGPT",
        "dedicated_backend_state": inventory["state"],
        "privileged_install": [
            "Install missing fixed root-owned crawler egress guard and systemd service",
            "Enable and verify the root firewall service (live nftables checks)",
            "Validate and install two exact NOPASSWD sudoers rules: root-owned --probe and --preflight (the latter reserved for future on-demand use)",
        ],
        "user_install": [
            "Install only the fixed tunnel egress-probe drop-in",
            "Reload systemd user units and start the tunnel/health timer",
        ],
        "constraints": ("Existing differing files are never overwritten; no Docker "
                        "start/recreate, update, secrets, or other MCP client changes"),
        "confirmation_required": True,
    }


# The command must actually survive systemd's drop-in merge, not just
# appear in a unit file (a later drop-in can reset ExecStartPre=).
_REQUIRED_PROBE_ARGV = (
    "/usr/bin/sudo -n /usr/bin/python3 "
    "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py "
    "--container crawl4ai --probe"
)


def _verify_effective_tunnel_probe() -> None:
    raw = _run("systemctl", "--user", "show", USER_TUNNEL,
               "--property=ExecStartPre", "--value").stdout.strip()
    if len(raw) > 8192:
        raise InstallError("Effective tunnel pre-start commands are too large")
    entries = re.findall(r"\{([^{}]*)\}", raw)
    # One exact, non-ignored root probe is required. Refuse missing probes,
    # additional commands or unexpected serialization rather than guessing.
    if len(entries) != 1:
        raise InstallError("Effective tunnel firewall startup probe is missing or overridden")
    fields = {}
    for field in entries[0].split(";"):
        key, separator, value = field.strip().partition("=")
        if separator and key not in fields:
            fields[key] = value.strip()
    if (fields.get("path") != "/usr/bin/sudo"
            or fields.get("argv[]") != _REQUIRED_PROBE_ARGV
            or fields.get("ignore_errors") != "no"):
        raise InstallError("Effective tunnel firewall startup probe is not enforced")


def install() -> None:
    preview()
    # No silent attempt to relax security on an already running tunnel.
    active = _run("systemctl", "--user", "is-active", USER_TUNNEL, check=False)
    if active.stdout.strip() in ("active", "activating", "reloading"):
        raise InstallError("Stop the running tunnel before installing startup protections")
    if _run("systemctl", "--user", "show", USER_TUNNEL,
            "--property=LoadState").stdout.strip() != "LoadState=loaded":
        raise InstallError("Managed tunnel user unit is not installed")

    # A single password prompt may be needed. No editable Python is run as root.
    print("Authenticating for fixed root-owned firewall installation...")
    _sudo("-v")
    _install_root_file(GUARD, ROOT_GUARD, "0755")
    _install_root_file(SERVICE, ROOT_SERVICE, "0644")
    _sudo("systemctl", "daemon-reload")
    _sudo("systemctl", "enable", "--now", ROOT_SERVICE.stem + ".service", timeout=300)
    if _sudo("systemctl", "is-active", ROOT_SERVICE.stem + ".service").stdout.strip() != "active":
        raise InstallError("System firewall service failed verification")

    print("Validating the restricted firewall-probe sudoers policy...")
    _install_sudoers()
    _sudo("-n", "/usr/bin/python3", str(ROOT_GUARD),
          "--container", "crawl4ai", "--probe", timeout=180)
    print("Installing tunnel pre-start protection...")
    _install_user_dropin()
    _run("systemctl", "--user", "daemon-reload")
    _verify_effective_tunnel_probe()
    # Only after a successful live root-owned probe can the tunnel be started.
    try:
        _run("systemctl", "--user", "enable", "--now", USER_TUNNEL, timeout=180)
        active = _run("systemctl", "--user", "is-active", USER_TUNNEL)
        if active.stdout.strip() != "active":
            raise InstallError("Protected tunnel did not reach active state")
        _run("systemctl", "--user", "enable", "--now",
             "local-search-mcp-health.timer", timeout=180)
    except (InstallError, KeyboardInterrupt):
        # Interrupted or partly successful systemctl commands can leave the
        # tunnel active. Always attempt to shut it off; the operator must
        # still verify its status if this best-effort cleanup fails.
        try:
            _run("systemctl", "--user", "disable", "--now",
                 USER_TUNNEL, timeout=30)
        except InstallError:
            print("WARNING: automatic tunnel shutdown failed; "
                  "the tunnel may still be running. Verify with "
                  "systemctl --user is-active " + USER_TUNNEL +
                  " and stop/disable it manually.", file=sys.stderr)
        raise
    print("Protected ChatGPT tunnel enabled. Verify local MCP tools and remote registration.")
    print("WARNING: Running tunnel is not revoked if nftables is later manually removed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true",
                        help="install privileged protection and start protected tunnel")
    parser.add_argument("--confirm", choices=("SECURE",),
                        help="required for installation; preview is default")
    args = parser.parse_args(argv)
    if args.confirm and not args.install:
        parser.error("--confirm requires --install")
    if args.install and args.confirm != "SECURE":
        parser.error("Explicit --confirm SECURE required")
    try:
        if args.install:
            install()
        else:
            import json
            print(json.dumps(preview(), sort_keys=True, indent=2))
        return 0
    except (InstallError, OSError, ValueError) as exc:
        # Only literal, reviewed operator advice may reach stderr. Exceptions
        # from subprocesses, paths or future dependencies can contain secrets.
        advice = {
            "Existing local-search-mcp sudoers differs; refuse overwrite":
                "Existing restricted sudoers policy differs; compare its contents "
                "against the two fixed --probe/--preflight rules without "
                "overwriting unrelated policy.",
            "Existing sudoers has unsafe ownership or mode":
                "Existing restricted sudoers policy must be root-owned, "
                "a regular file and mode 0440.",
            "Existing privileged file owner or mode is unsafe":
                "An installed root-owned security file has incorrect permissions; "
                "inspect and reconcile the reviewed file manually.",
            "Installed security file differs; review it manually before replacing":
                "An installed security file differs from the reviewed template; "
                "inspect the difference without overwriting it.",
            "Effective tunnel firewall startup probe is missing or overridden":
                "The effective tunnel pre-start firewall probe is absent or reset "
                "by another systemd drop-in; inspect systemctl --user show.",
            "Effective tunnel firewall startup probe is not enforced":
                "The effective tunnel pre-start command is not the exact "
                "non-ignored root-owned firewall probe.",
        }
        if type(exc) is InstallError:
            detail = advice.get(str(exc), "A guarded prerequisite failed; "
                                "inspect the root firewall, sudoers and user "
                                "systemd unit without weakening protections")
        else:
            detail = "A local setup operation failed; inspect filesystem and service state"
        print("Secure tunnel setup incomplete: " + detail +
              ". Check systemctl --user is-active local-search-mcp-tunnel.service; "
              "stop and disable it if protection is uncertain.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
