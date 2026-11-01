#!/usr/bin/env python3
"""Root-owned, fixed-address lifecycle for the optional private MCP endpoint.

Install a reviewed copy under /usr/local/libexec/local-search-mcp. Never run
this file as root directly from the owner-writable Git working tree.
"""
from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path
import subprocess
import sys

PRIVATE_IP = "10.253.91.1"
CIDR = PRIVATE_IP + "/32"
TABLE = "lsm_cf_private_endpoint"
MARKER = Path("/run/local-search-mcp-cloudflare-private.owned")
INVOCATION_PATTERN = re.compile(r"[a-fA-F0-9]{32}\Z")


def invocation_id() -> str:
    value = os.environ.get("INVOCATION_ID", "")
    if not INVOCATION_PATTERN.fullmatch(value):
        raise RuntimeError("Managed systemd invocation ID required")
    return value.lower()
RULES = """add table inet lsm_cf_private_endpoint
add chain inet lsm_cf_private_endpoint input { type filter hook input priority -150; policy accept; }
add rule inet lsm_cf_private_endpoint input ip daddr 10.253.91.1 iifname != "lo" drop
"""


def command(*args: str, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input, text=True, capture_output=True,
                          check=False, timeout=10)


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Only root may manage the private ingress boundary")
    # Root-owned file alone is insufficient: a writable ancestor permits
    # replacement of the helper before its next privileged execution.
    path = Path(__file__).resolve()
    for component in (path, *path.parents):
        info = component.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Privileged endpoint helper ancestry is unsafe")


def exists_ip() -> bool:
    result = command("/usr/bin/ip", "-4", "-o", "addr", "show", "dev", "lo")
    if result.returncode:
        raise RuntimeError("Cannot inspect loopback address")
    # A differently masked alias is still a conflicting private address.
    # Never create or remove our /32 while the same IP has foreign ownership.
    return any(
        field.split("/", 1)[0] == PRIVATE_IP
        for line in result.stdout.splitlines()
        for field in line.split()
        if "/" in field
    )


def exists_table() -> bool:
    result = command("/usr/bin/nft", "list", "table", "inet", TABLE)
    if result.returncode == 0:
        return True
    if "No such file or directory" not in result.stderr:
        raise RuntimeError("Cannot inspect dedicated nftables table")
    return False


def prepare() -> None:
    invocation = invocation_id()
    # Never modify a pre-existing alias or nft table; another owner or a
    # stale deployment requires explicit inspection and cleanup first.
    if MARKER.exists() or exists_ip() or exists_table():
        raise RuntimeError("Private ingress resources already exist: inspect before retry")
    # Install the non-loopback DROP before adding the address or listener.
    result = command("/usr/bin/nft", "-f", "-", input=RULES)
    if result.returncode:
        raise RuntimeError("Cannot install private ingress guard")
    marker_created = False
    try:
        result = command("/usr/bin/ip", "-4", "addr", "add", CIDR, "dev", "lo")
        if result.returncode:
            raise RuntimeError("Cannot install loopback-only private address")
        # Marker is root-owned; no user-writable run-directory is trusted.
        fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        marker_created = True
        with os.fdopen(fd, "w", encoding="ascii") as marker:
            marker.write(invocation + "\n")
    except Exception:
        # The ingress guard MUST remain when address cleanup fails.
        if exists_ip():
            deletion = command("/usr/bin/ip", "-4", "addr", "del", CIDR, "dev", "lo")
            if deletion.returncode or exists_ip():
                raise RuntimeError("Alias cleanup failed; ingress guard preserved")
        cleanup = command("/usr/bin/nft", "delete", "table", "inet", TABLE)
        if cleanup.returncode:
            raise RuntimeError("Ingress guard cleanup failed; inspect before retry")
        # Only clean the marker created by this activation. An incomplete
        # marker after ENOSPC must not block all subsequent activations.
        if marker_created:
            MARKER.unlink()
        raise


def remove() -> None:
    if not MARKER.exists():
        return
    info = MARKER.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise RuntimeError("Unexpected ownership of private ingress marker")
    # ExecStopPost also runs after an unsuccessful ExecStartPre. A different
    # activation/instance must not remove another service's active ingress.
    if MARKER.read_text(encoding="ascii") != invocation_id() + "\n":
        return
    # Fail closed if deleting the address fails: retain nftables protection.
    if exists_ip():
        result = command("/usr/bin/ip", "-4", "addr", "del", CIDR, "dev", "lo")
        if result.returncode or exists_ip():
            raise RuntimeError("Cannot remove private address; guard preserved")
    if exists_table():
        result = command("/usr/bin/nft", "delete", "table", "inet", TABLE)
        if result.returncode:
            raise RuntimeError("Cannot remove ingress guard")
    MARKER.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "remove"))
    args = parser.parse_args(argv)
    try:
        require_root()
        if args.action == "prepare":
            prepare()
        else:
            remove()
        return 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        print("Private MCP endpoint unavailable; inspect system journal and guard",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
