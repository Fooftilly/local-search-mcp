"""Strict, read-only production prerequisite adapters for remote MCP (#230).

No listener, file writer, token generator, tunnel or privileged command runs
at import time. Operators must explicitly bind these adapters to a future
locally bound gateway runner; their presence does NOT provision remote access.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess

from remote_agent_gateway import ClientGrant, MAX_GRANTS

SCHEMA = "local-search-mcp/agent-grants/v1"
MAX_STORE_BYTES = 8192
# This exact command is already allowed by the existing operator-installed
# narrow sudoers rule. Never run user-editable checkout code with sudo.
ROOT_GUARD_COMMAND = (
    "/usr/bin/sudo", "-n", "/usr/bin/python3",
    "/usr/local/libexec/local-search-mcp/crawler_egress_guard.py",
    "--container", "crawl4ai", "--tunnel-gate",
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class CredentialStoreError(ValueError):
    """Non-disclosing credential store validation failure."""


def _unique_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise CredentialStoreError("Invalid credential store")
        obj[key] = value
    return obj


def _check_parent_chain(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise CredentialStoreError("Invalid credential store")
    parent = path.parent
    if parent == parent.parent:
        raise CredentialStoreError("Invalid credential store")
    owner = os.geteuid()
    first = True
    for directory in (parent, *parent.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise CredentialStoreError("Invalid credential store")
        mode = stat.S_IMODE(info.st_mode)
        if first:
            # The leaf directory is private to this service user.
            if info.st_uid != owner or (mode & 0o077):
                raise CredentialStoreError("Invalid credential store")
        elif (mode & 0o022) and not (
                info.st_uid == 0 and mode & stat.S_ISVTX):
            # /tmp's root-owned sticky directory is safe only if its child
            # is a mode-0700 directory owned by our user.
            raise CredentialStoreError("Invalid credential store")
        first = False


def load_client_grants(path: str | Path) -> tuple[ClientGrant, ...]:
    """Read and validate an operator-owned 0600 verifier file each request.

    Fails closed on absent files, links, unsafe parents, changed ownership,
    truncated JSON, duplicate keys/IDs/digests or any raw token field.
    No file contents are retained in a module-level cache.
    """
    try:
        target = Path(path)
        _check_parent_chain(target)
        fd = os.open(
            target, os.O_RDONLY | os.O_NOFOLLOW |
            os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_uid != os.geteuid() or
                    stat.S_IMODE(info.st_mode) != 0o600 or
                    info.st_size > MAX_STORE_BYTES):
                raise CredentialStoreError("Invalid credential store")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_STORE_BYTES + 1)
            if len(raw) > MAX_STORE_BYTES:
                raise CredentialStoreError("Invalid credential store")
        finally:
            os.close(fd)
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_keys,
            parse_constant=lambda _: (_ for _ in ()).throw(
                CredentialStoreError("Invalid credential store")))
        if not isinstance(data, dict) or set(data) != {"schema", "grants"}:
            raise CredentialStoreError("Invalid credential store")
        if data["schema"] != SCHEMA:
            raise CredentialStoreError("Invalid credential store")
        items = data["grants"]
        if not isinstance(items, list) or len(items) > MAX_GRANTS:
            raise CredentialStoreError("Invalid credential store")
        grants = []
        ids, digests = set(), set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                    "client_id", "token_sha256", "expires_at"}:
                raise CredentialStoreError("Invalid credential store")
            digest_hex = item["token_sha256"]
            expires_at = item["expires_at"]
            if (not isinstance(digest_hex, str) or
                    not _HEX_SHA256.fullmatch(digest_hex) or
                    type(expires_at) not in (int, float) or
                    not math.isfinite(expires_at) or expires_at <= 0):
                raise CredentialStoreError("Invalid credential store")
            grant = ClientGrant(
                client_id=item["client_id"],
                token_sha256=bytes.fromhex(digest_hex),
                expires_at=float(expires_at),
            )
            if grant.client_id in ids or grant.token_sha256 in digests:
                raise CredentialStoreError("Invalid credential store")
            ids.add(grant.client_id)
            digests.add(grant.token_sha256)
            grants.append(grant)
        return tuple(grants)
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
        raise CredentialStoreError("Invalid credential store") from None


def fixed_root_tunnel_gate() -> bool:
    """Require the preinstalled, root-owned live/stopped nftables guard.

    This checks the existing Docker/bridge containment. It does not install
    nftables, start or stop containers, grant sudo rights, or publish a tunnel.
    Failure or missing sudo authorization always returns False.
    """
    try:
        result = subprocess.run(
            ROOT_GUARD_COMMAND,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            cwd="/",
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return result.returncode == 0 and result.stdout.startswith("PASS:")
