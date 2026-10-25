#!/usr/bin/env python3
"""One attended update: reviewed fast-forward, one state backup, controlled restart.

Never called by MCP tools or the web dashboard. Temporary downtime is expected.
Failures after checkout changes require *manual* recovery; no automatic rollback.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__:
    from . import backup_state
else:
    sys.path.insert(0, str(ROOT))
    from scripts import backup_state
BRIDGE = "local-search-mcp-bridge.service"
TUNNEL = "local-search-mcp-tunnel.service"
HEALTH = "local-search-mcp-health.timer"
WATCHERS = ("local-search-mcp-update.path", "local-search-mcp-update.timer")
CHECKERS = ("local-search-mcp-update.service", "local-search-mcp-health.service")
UNITS = (*WATCHERS, HEALTH, *CHECKERS, TUNNEL, BRIDGE)
MARKER = "# Managed by local-search-mcp; contains no credentials."
REMOTES = {
    "https://github.com/Fooftilly/local-search-mcp",
    "https://github.com/Fooftilly/local-search-mcp.git",
    "git@github.com:Fooftilly/local-search-mcp.git",
    "ssh://git@github.com/Fooftilly/local-search-mcp.git",
}


class UpdateError(Exception):
    """Operator-actionable stop; never include untrusted subprocess output."""


def command(args: list[str], *, timeout: int = 30, env: dict | None = None) -> str:
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                                check=False, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("Required local command could not run (" +
                          type(exc).__name__ + ")") from None
    if result.returncode:
        raise UpdateError("Local command failed: " + args[0])
    return result.stdout.strip()


def git(*args: str, timeout: int = 30) -> str:
    # Do not inherit agent-injected Git environment or invoke checkout hooks.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return command(["git", "-c", "core.hooksPath=/dev/null",
                    "--no-optional-locks", *args], timeout=timeout, env=env)


def checkout() -> str:
    if git("rev-parse", "--show-toplevel") != str(ROOT.resolve()):
        raise UpdateError("Run setup from the expected Git checkout")
    if git("symbolic-ref", "--short", "HEAD") != "master":
        raise UpdateError("Update requires the checked-out master branch")
    if git("remote", "get-url", "origin") not in REMOTES:
        raise UpdateError("Origin must be the reviewed Fooftilly/local-search-mcp GitHub repository")
    if git("status", "--porcelain=v1", "--untracked-files=normal"):
        raise UpdateError("Git checkout contains changes; review/stash them manually first")
    head = git("rev-parse", "--verify", "HEAD^{commit}")
    if len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        raise UpdateError("Current revision identity could not be verified")
    return head


def unit_file(name: str) -> Path:
    return (Path(os.environ.get("XDG_CONFIG_HOME") or
                 str(Path.home() / ".config")) / "systemd" / "user" / name)


def approved_units() -> dict[str, bool]:
    """Fail closed for foreign/missing-but-active units before service mutation."""
    result = {}
    for name in UNITS:
        path = unit_file(name)
        details = command(["systemctl", "--user", "show", name,
                           "--property=LoadState", "--property=ActiveState",
                           "--property=FragmentPath"])
        state = dict(line.split("=", 1) for line in details.splitlines() if "=" in line)
        if (state.get("LoadState") == "not-found"
                and state.get("ActiveState") == "inactive"
                and not path.exists() and not path.is_symlink()):
            result[name] = False
            continue
        if (path.is_symlink() or not path.is_file()
                or not path.read_text(encoding="utf-8").startswith(MARKER)
                or state.get("LoadState") != "loaded"
                or state.get("FragmentPath") != str(path)
                or state.get("ActiveState") not in ("inactive", "active", "failed", "activating")):
            raise UpdateError("Unknown or unverified systemd unit: " + name)
        result[name] = state["ActiveState"] in ("active", "activating")
    return result


def ensure_stopped() -> None:
    for name, active in approved_units().items():
        if active:
            raise UpdateError("Project-owned service unexpectedly active: " + name)
    for _ in range(20):
        if not listener_ready():
            return
        time.sleep(0.25)
    raise UpdateError("MCP bridge listener is still active after stop")


def backup_path(previous: str) -> Path:
    state = backup_state.state_directory()
    if state.is_symlink() or not state.is_dir():
        raise UpdateError("Existing private state directory is required before update")
    info = state.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise UpdateError("Private state directory requires operator ownership and mode 0700")
    folder = state / "backups"
    if folder.is_symlink():
        raise UpdateError("Backup directory cannot be a symlink")
    if not folder.exists():
        folder.mkdir(mode=0o700)
    info = folder.stat()
    if (not folder.is_dir() or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise UpdateError("Backup directory requires operator ownership and mode 0700")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return folder / f"before-update-{stamp}-{previous[:12]}.zip"


def listener_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8790), timeout=1):
            return True
    except OSError:
        return False


def restart(previously_active: dict[str, bool]) -> None:
    # Never reenable retired Git watcher units. Restore only the services
    # previously active; leave Docker containers and root-owned firewall alone.
    for name in (BRIDGE, TUNNEL, HEALTH):
        if previously_active.get(name):
            command(["systemctl", "--user", "start", name])
            command(["systemctl", "--user", "is-active", "--quiet", name])
    if previously_active.get(BRIDGE):
        for _ in range(20):
            if listener_ready():
                break
            time.sleep(0.5)
        else:
            raise UpdateError("Bridge did not listen on loopback after restart")


def confirmation(word: str) -> bool:
    try:
        return input(f"Type {word} to proceed (anything else cancels): ").strip() == word
    except (EOFError, KeyboardInterrupt):
        return False


def update() -> int:
    if os.geteuid() == 0 or not sys.stdin.isatty():
        raise UpdateError("Run ./setup.sh --update interactively as the normal user")
    old = checkout()
    if not (ROOT / ".venv" / "bin" / "python").is_file():
        raise UpdateError("Existing project virtualenv is required; use guided setup first")
    units = approved_units()
    if listener_ready() and not units.get(BRIDGE):
        raise UpdateError("Unmanaged bridge is listening; stop the foreground ./start.sh session first")
    print("Current revision:", old)
    print("Update source: official Fooftilly/local-search-mcp origin, master.")
    print("FETCH contacts GitHub but does not update running services.")
    if not confirmation("FETCH"):
        print("Cancelled before network access")
        return 0
    git("fetch", "--no-tags", "--", "origin", "master", timeout=180)
    target = git("rev-parse", "--verify", "FETCH_HEAD^{commit}")
    # No stdout on success; git() raises if the target isn't a descendant.
    git("merge-base", "--is-ancestor", old, target)
    if target == old:
        print("Already at the reviewed remote revision; no changes made")
        return 0
    count = git("rev-list", "--count", f"{old}..{target}")
    changed_files = git("diff", "--name-only", old, target).splitlines()
    if len(changed_files) > 250 or any(
            len(name) > 300 or any(ord(c) < 32 or ord(c) == 127 for c in name)
            for name in changed_files):
        raise UpdateError("Unexpected update file list; review Git changes manually")
    print(f"Proposed fast-forward: {old[:12]} -> {target[:12]} ({count} commits).")
    for name in changed_files:
        print(" ", name)
    print("UPDATE stops owned units, disables legacy update watcher, backs up state,")
    print("updates code/dependencies, and restarts only previously active units.")
    print("Temporary downtime is expected. Recovery after a partial update is manual.")
    if not confirmation("UPDATE"):
        print("Cancelled before changing services or code")
        return 0
    if checkout() != old or git("rev-parse", "FETCH_HEAD") != target:
        raise UpdateError("Checkout or reviewed target changed; update cancelled")
    latest = approved_units()
    # A short-lived health/update oneshot may finish during operator review.
    if any(latest[name] != units[name] for name in
           (*WATCHERS, HEALTH, TUNNEL, BRIDGE)):
        raise UpdateError("Service states changed during review; update cancelled")
    if listener_ready() and not latest.get(BRIDGE):
        raise UpdateError("Unmanaged bridge started during review; stop it before updating")
    archive = None
    changed = False
    try:
        for name in WATCHERS:
            # Intentionally retire the watcher even when it was disabled before
            # updating. Reboot must not reenable the old automatic update path.
            if unit_file(name).is_file():
                command(["systemctl", "--user", "disable", "--now", name])
        for name in (HEALTH, *CHECKERS, TUNNEL, BRIDGE):
            # A oneshot checker may start after the initial state inspection.
            # Stop the verified owned checker even if it was previously idle.
            if units.get(name) or (name in CHECKERS and unit_file(name).is_file()):
                command(["systemctl", "--user", "stop", name])
        ensure_stopped()
        archive = backup_path(old)
        backup_state.backup(archive)
        backup_state.verified_payload(archive)
        print("Verified private state backup:", archive)
        if checkout() != old:
            raise UpdateError("Checkout changed during backup")
        # Treat even a *failed* merge as potentially mutating the checkout.
        # Never restart services automatically until manual inspection.
        changed = True
        git("merge", "--ff-only", "--", target, timeout=90)
        if git("rev-parse", "HEAD") != target:
            raise UpdateError("Updated revision identity mismatch")
        command([str(ROOT / ".venv" / "bin" / "python"), "-m", "pip",
                 "install", "--disable-pip-version-check", "-r",
                 str(ROOT / "requirements.txt")], timeout=360)
        restart(units)
    except (OSError, ValueError, UpdateError, KeyboardInterrupt) as exc:
        if not changed:
            # No code/dependency changes were made; restore previously active
            # services, but leave deprecated watcher disabled.
            try:
                restart(units)
            except UpdateError:
                pass
        print("Update interrupted. Do not run automatic rollback.", file=sys.stderr)
        print("Previous revision:", old, file=sys.stderr)
        if archive:
            print("Backup location:", archive, file=sys.stderr)
        print("Stop bridge/tunnel, inspect checkout and dependencies, then manually "
              "return to the previous revision if required.", file=sys.stderr)
        if isinstance(exc, UpdateError):
            print("Reason:", exc, file=sys.stderr)
        return 1
    print("Update successful:", target)
    print("Git watcher remains disabled; updates now run only from setup.sh.")
    print("Check the ChatGPT connection and a permitted search/fetch from your agent.")
    return 0


def main() -> int:
    try:
        return update()
    except UpdateError as exc:
        # Locally authored messages never include private subprocess output.
        print("Update refused: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError,
            KeyboardInterrupt) as exc:
        print("Update not started or completed (" + type(exc).__name__ +
              "); review local setup and Git status.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
