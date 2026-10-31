#!/usr/bin/env python3
"""Operator-only Cloudflare named-tunnel MCP route editor (#246).

Only a *locally managed* YAML tunnel with a terminal 404 rule is supported.
Never touches DNS, Cloudflare APIs, tunnels, services, firewall or MCP clients.
Each operator provides their own hostname and private file outside the repo.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from remote_agent_credentials import default_store_path
from remote_agent_server import StartupRefused, preflight_local
GATEWAY = "http://127.0.0.1:18790"
BEGIN = "  # BEGIN local-search-mcp managed authenticated route\n"
END = "  # END local-search-mcp managed authenticated route\n"
MAX_BYTES = 128 * 1024
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
INGRESS = re.compile(r"(?m)^ingress:\s*$")
CATCHALL = re.compile(r"(?m)^  - service: http_status:404[ \t]*$")
HOST_RULE = re.compile(r"(?m)^[ \t]+(?:-\s*)?hostname:\s*['\"]?([^'\"\s#]+)['\"]?[ \t]*(?:#.*)?$")
SAFE_TAIL = re.compile(r"\A(?:[ \t]*\n|[ \t]*#[^\n]*\n?)*\Z")


class RouteError(RuntimeError):
    """Sanitized failure: never expose hostname, filesystem content or tokens."""


def _hostname(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 253
            or value != value.lower() or not HOSTNAME.fullmatch(value)
            or any(len(part) > 63 for part in value.split("."))):
        raise RouteError("Provide one lowercase DNS hostname (no wildcard, scheme or path)")
    return value


def _private_config(path: Path) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute() or ".." in path.parts:
        raise RouteError("Cloudflare configuration must use a private absolute path")
    if path.is_relative_to(ROOT):
        raise RouteError("Tunnel configuration must stay outside the Git checkout")
    # Reject symlink aliases at every level; no accidental edits to another file.
    try:
        current = path
        while current != current.parent:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise RouteError("Symlink in private tunnel path")
            current = current.parent
        info = path.stat()
        directory = path.parent.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_size > MAX_BYTES
                or stat.S_IMODE(info.st_mode) & 0o022
                or not stat.S_ISDIR(directory.st_mode)
                or directory.st_uid != os.geteuid()
                or stat.S_IMODE(directory.st_mode) & 0o022):
            raise RouteError("Unsafe local tunnel configuration ownership or mode")
        raw = path.read_bytes()
    except (OSError, ValueError):
        raise RouteError("Cannot read private local Cloudflare configuration") from None
    if len(raw) > MAX_BYTES or b"\x00" in raw:
        raise RouteError("Invalid or oversized local Cloudflare configuration")
    return raw, info


def _render(raw: bytes, hostname: str, *, remove: bool = False) -> bytes:
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        raise RouteError("Tunnel configuration is not UTF-8") from None
    if content.count(BEGIN) != content.count(END):
        raise RouteError("Incomplete managed route markers; no change")
    if not content.endswith("\n") or len(list(INGRESS.finditer(content))) != 1:
        raise RouteError("Unsupported ambiguous tunnel YAML layout")
    if (len(re.findall(r"(?m)^tunnel:[^\n]+$", content)) != 1
            or len(re.findall(r"(?m)^credentials-file:[^\n]+$", content)) != 1):
        raise RouteError("Expected one locally managed named tunnel and credential-file")
    if remove:
        if content.count(BEGIN) != 1 or content.count(END) != 1:
            raise RouteError("No unique managed route available to remove")
        first = content.index(BEGIN)
        end = content.index(END, first) + len(END)
        expected = _block(hostname)
        if content[first:end] != expected:
            raise RouteError("Existing managed route differs; refuse removal")
        return (content[:first] + content[end:]).encode()
    if BEGIN in content or END in content:
        raise RouteError("A managed MCP route already exists; refuse overwrite")
    if any(m.group(1).lower() == hostname for m in HOST_RULE.finditer(content)):
        raise RouteError("Hostname already has a route; refuse overwrite")
    matches = list(CATCHALL.finditer(content))
    if len(matches) != 1 or not SAFE_TAIL.fullmatch(content[matches[0].end():]):
        raise RouteError("Requires one terminal 404 catch-all in a locally managed tunnel")
    if matches[0].start() < next(INGRESS.finditer(content)).end():
        raise RouteError("Invalid ingress structure")
    return (content[:matches[0].start()] + _block(hostname)
            + content[matches[0].start():]).encode()


def _block(hostname: str) -> str:
    # Hostname is already restricted to lowercase ASCII DNS labels.
    return (BEGIN + "  - hostname: " + hostname + "\n"
            + '    path: "^/mcp$"\n'
            + "    service: " + GATEWAY + "\n"
            + "    originRequest:\n"
            + "      httpHostHeader: 127.0.0.1:18790\n"
            + END)


def _cloudflared(binary: str, candidate: Path, hostname: str, *,
                 remove: bool = False) -> None:
    # All probes use a fixed binary and exact commands. Output is never logged.
    def run(*args: str) -> str:
        try:
            proc = subprocess.run(
                [binary, "tunnel", "--config", str(candidate), "ingress", *args],
                capture_output=True, text=True, check=False, timeout=12)
        except (OSError, subprocess.TimeoutExpired):
            raise RouteError("Local cloudflared validation unavailable") from None
        if proc.returncode:
            raise RouteError("Local cloudflared ingress validation failed") 
        return proc.stdout
    run("validate")
    for path, expected in (
        ("/mcp", "http_status:404" if remove else GATEWAY),
        ("/dashboard", "http_status:404"),
        ("/mcp/", "http_status:404"),
    ):
        out = run("rule", f"https://{hostname}{path}")
        if not re.search(r"(?m)^\s*service:\s*" + re.escape(expected) + r"\s*$", out):
            raise RouteError("Cloudflared routing rules do not isolate the MCP path")
    other = run("rule", "https://other-invalid-host.example/mcp")
    if not re.search(r"(?m)^\s*service:\s*http_status:404\s*$", other):
        raise RouteError("Cloudflared catch-all does not reject unknown hosts")


def _check_not_running() -> None:
    # Do not rewrite a live shared tunnel or restart any other tunnel/app.
    try:
        found = subprocess.run(
            ["pgrep", "-x", "cloudflared"], capture_output=True, check=False,
            timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        raise RouteError("Cannot verify cloudflared is stopped") from None
    if found.returncode == 0:
        raise RouteError("cloudflared is running; stop it explicitly before editing the local config")
    if found.returncode != 1:
        raise RouteError("Cannot verify cloudflared process state")


def _atomic_update(path: Path, before: bytes, info: os.stat_result,
                   after: bytes) -> None:
    """Back up unchanged original once, then replace with a private atomic file."""
    new = None
    try:
        current, current_info = _private_config(path)
        if (current != before or current_info.st_ino != info.st_ino
                or current_info.st_dev != info.st_dev
                or current_info.st_mtime_ns != info.st_mtime_ns):
            raise RouteError("Cloudflare config changed during validation; retry")
        original_hash = hashlib.sha256(before).hexdigest()[:20]
        backup = path.with_name(path.name + ".before-mcp-" + original_hash + ".bak")
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            raise RouteError("Private backup exists or cannot be created; no change") from None
        with os.fdopen(fd, "wb") as handle:
            handle.write(before)
            handle.flush()
            os.fsync(handle.fileno())
        fd, filename = tempfile.mkstemp(prefix=".cloudflared-mcp-", dir=path.parent)
        new = Path(filename)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(info.st_mode))
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(new, path)
        new = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise RouteError("Private route write may be incomplete; inspect local backup") from None
    finally:
        if new is not None:
            try:
                new.unlink(missing_ok=True)
            except OSError:
                pass


def configure(config: Path, hostname: str, *, action: str = "preview",
              cloudflared_binary: str | None = None,
              verify_auth=None) -> None:
    if os.geteuid() == 0:
        raise RouteError("Run Cloudflare route setup as an unprivileged user")
    hostname = _hostname(hostname)
    if action not in ("preview", "apply", "remove"):
        raise RouteError("Unsupported operation")
    raw, info = _private_config(config)
    candidate = _render(raw, hostname, remove=action == "remove")
    if len(candidate) > MAX_BYTES:
        raise RouteError("Tunnel config exceeds limit")
    binary = cloudflared_binary or shutil.which("cloudflared")
    if not binary or not Path(binary).is_absolute():
        raise RouteError("cloudflared must be installed and discoverable")
    # Use ephemeral private scratch for *local* cloudflared ingress checks.
    with tempfile.TemporaryDirectory(prefix="lsm-cloudflare-") as directory:
        test = Path(directory) / "config.yml"
        test.write_bytes(candidate)
        test.chmod(0o600)
        _cloudflared(binary, test, hostname, remove=action == "remove")
    if action == "preview":
        return
    _check_not_running()
    if action == "apply":
        # Validate authorizing credentials, read-only root guard and localhost
        # MCP tools before committing ANY new live-config route.
        try:
            if verify_auth is None:
                asyncio.run(preflight_local(default_store_path()))
            else:
                verify_auth()
        except (StartupRefused, OSError, ValueError, RuntimeError):
            raise RouteError("Authenticated gateway preflight failed; no route changed") from None
    _atomic_update(config, raw, info, candidate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preview", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--remove", action="store_true")
    parser.add_argument("--config", type=Path, required=True,
                        help="absolute path to locally managed private tunnel YAML")
    parser.add_argument("--hostname", required=True, help="own lower-case FQDN; no wildcard")
    parser.add_argument("--confirm", choices=("APPLY", "REMOVE"))
    args = parser.parse_args(argv)
    if args.apply and args.confirm != "APPLY":
        parser.error("Applying the tunnel route requires --confirm APPLY")
    if args.remove and args.confirm != "REMOVE":
        parser.error("Removing the tunnel route requires --confirm REMOVE")
    if not (args.apply or args.remove) and args.confirm:
        parser.error("Preview must not include a confirmation")
    action = "apply" if args.apply else "remove" if args.remove else "preview"
    try:
        configure(args.config, args.hostname, action=action)
    except RouteError as exc:
        print("Cloudflare MCP route refused: " + str(exc), file=sys.stderr)
        return 1
    if action == "preview":
        print("Validated a private draft MCP route and negative path rules. No persistent changes.")
    else:
        print("Local Cloudflare config " + ("updated" if action == "apply" else "route removed") +
              " with a private original backup; NO DNS, tunnel restart, service or public activation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
