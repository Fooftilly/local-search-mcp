"""Offline, explicitly opt-in operator credential management for remote MCP.

No secrets, files, listeners or tunnels are created when imported. Preview and
list commands are read-only. Mutating commands require --apply and the same
private directory ownership rules as the gateway's per-request loader.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
from typing import Callable

from remote_agent_gateway import ClientGrant, MAX_GRANTS, synthetic_grant
from remote_agent_prerequisites import (
    _check_parent_chain, CredentialStoreError, MAX_STORE_BYTES,
    SCHEMA, load_client_grants,
)

MAX_TTL_HOURS = 720
_LOCK_SUFFIX = ".lock"


class ProvisioningError(ValueError):
    """Operator-safe error without token or path content."""


def _check_path(path: Path) -> None:
    try:
        _check_parent_chain(path)
        if path.name in ("", ".", "..") or path.name.endswith(_LOCK_SUFFIX):
            raise ProvisioningError("Unsafe credential path")
    except (OSError, CredentialStoreError, ValueError):
        raise ProvisioningError("Unsafe credential path") from None


def _existing_grants(path: Path, *, allow_missing: bool) -> tuple[ClientGrant, ...]:
    # lstat distinguishes nonexistent files from dangling symlinks.
    try:
        path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return ()
        raise ProvisioningError("Credential store does not exist") from None
    try:
        return load_client_grants(path)
    except CredentialStoreError:
        raise ProvisioningError("Credential store is not safe") from None


def plan_change(path: str | Path, action: str, client_id: str) -> str:
    """Read-only dry run, intentionally does not generate any bearer token."""
    target = Path(path)
    _check_path(target)
    _validate_action(action, client_id)
    grants = _existing_grants(target, allow_missing=action == "create")
    known = {g.client_id for g in grants}
    if action == "create" and client_id in known:
        raise ProvisioningError("Client already exists; use rotate")
    if action == "create" and len(grants) >= MAX_GRANTS:
        raise ProvisioningError("Maximum client grants reached")
    if action != "create" and client_id not in known:
        raise ProvisioningError("Client does not exist")
    return f"Preview: {action} client {client_id}; {len(grants)} current grant(s). No changes made."


def _validate_action(action: str, client_id: str) -> None:
    if action not in ("create", "rotate", "revoke"):
        raise ProvisioningError("Unsupported operation")
    try:
        ClientGrant(client_id, b"0" * 32, 1)
    except (TypeError, ValueError):
        raise ProvisioningError("Invalid client ID") from None


def _validate_ttl(ttl_hours: int) -> None:
    if type(ttl_hours) is not int or not 1 <= ttl_hours <= MAX_TTL_HOURS:
        raise ProvisioningError("Token TTL must be 1 to 720 hours")


def _open_lock(path: Path) -> int:
    # The private parent directory has already passed ownership checks.
    # A persistent lock file serializes all compliant operator CLI writers.
    lockfile = path.with_name(path.name + _LOCK_SUFFIX)
    try:
        fd = os.open(
            lockfile, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600)
        details = os.fstat(fd)
        if (not stat.S_ISREG(details.st_mode) or details.st_nlink != 1
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o600):
            raise ProvisioningError("Unsafe credential lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, ProvisioningError):
        if "fd" in locals():
            os.close(fd)
        raise ProvisioningError("Credential update is unavailable or already running") from None


def _atomic_replace(path: Path, grants: tuple[ClientGrant, ...]) -> None:
    rows = [
        {"client_id": g.client_id,
         "token_sha256": g.token_sha256.hex(),
         "expires_at": g.expires_at}
        for g in sorted(grants, key=lambda x: x.client_id)
    ]
    serialized = (json.dumps(
        {"schema": SCHEMA, "grants": rows},
        sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    if len(serialized) > MAX_STORE_BYTES:
        raise ProvisioningError("Credential store exceeds size limit")
    descriptor = None
    temp_path = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".grant-update-", dir=path.parent)
        temp_path = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as f:
            descriptor = None
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
        # Do not replace a file which became unsafe, including a link,
        # between reading the old store and publishing the new store.
        _check_path(path)
        _existing_grants(path, allow_missing=True)
        os.replace(temp_path, path)
        temp_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, CredentialStoreError, ValueError) as exc:
        raise ProvisioningError("Credential update could not be committed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def apply_change(
    path: str | Path,
    action: str,
    client_id: str,
    *,
    ttl_hours: int = 24,
    reveal_and_confirm: Callable[[str, str], bool] | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """Explicit mutation. For issuance, require a one-time secret confirmation.

    The callback must use a trusted local TTY, not logs/stdout/arguments.
    If confirmation fails, the token digest is not committed. This library
    function never returns a raw token, even on success.
    """
    target = Path(path)
    _check_path(target)
    _validate_action(action, client_id)
    if action != "revoke":
        _validate_ttl(ttl_hours)
        if not callable(reveal_and_confirm):
            raise ProvisioningError("Secure local token confirmation required")
    fd = _open_lock(target)
    try:
        grants = list(_existing_grants(target, allow_missing=action == "create"))
        found = next((i for i, g in enumerate(grants)
                      if g.client_id == client_id), None)
        if action == "create":
            if found is not None:
                raise ProvisioningError("Client already exists; use rotate")
            if len(grants) >= MAX_GRANTS:
                raise ProvisioningError("Maximum client grants reached")
        elif found is None:
            raise ProvisioningError("Client does not exist")

        if action == "revoke":
            del grants[found]
        else:
            token = secrets.token_urlsafe(48)
            expires = float(now() + ttl_hours * 3600)
            grant = synthetic_grant(token, client_id, expires)
            if not reveal_and_confirm(client_id, token):
                raise ProvisioningError("Credential issuance not confirmed")
            if action == "rotate":
                grants[found] = grant
            else:
                grants.append(grant)
        _atomic_replace(target, tuple(grants))
        return f"{action.capitalize()} completed for {client_id}."
    finally:
        os.close(fd)


def _trusted_tty_confirmation(client_id: str, token: str) -> bool:
    # Deliberately never prints the token to stdout, stderr, shell command
    # history, GitHub, or a URL. The owner must have an interactive /dev/tty.
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            if not tty.isatty():
                raise ProvisioningError("An interactive terminal is required")
            tty.write(
                "\nNew remote MCP credential for " + client_id +
                "\nCopy the following secret directly to a password manager.\n" +
                "This secret is not stored in the verifier file.\n" +
                "Token: " + token +
                "\nType COMMIT to activate this token, anything else cancels: ")
            tty.flush()
            response = tty.readline()
        return response.strip() == "COMMIT"
    except OSError:
        raise ProvisioningError("Secure local terminal not available") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True,
                        help="absolute private verifier-file path")
    parser.add_argument("--apply", action="store_true",
                        help="explicitly commit the mutation")
    parser.add_argument("--ttl-hours", type=int, default=24)
    command = parser.add_subparsers(dest="action", required=True)
    command.add_parser("list", help="list IDs and expirations, never tokens")
    for action in ("create", "rotate", "revoke"):
        item = command.add_parser(action)
        item.add_argument("client_id")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise ProvisioningError("Run as the unprivileged service owner")
        _check_path(args.store)
        if args.action == "list":
            if args.apply:
                raise ProvisioningError("--apply is not valid for list")
            for g in _existing_grants(args.store, allow_missing=False):
                print(f"{g.client_id}: expires at {int(g.expires_at)} UTC epoch")
        elif args.apply:
            print(apply_change(
                args.store, args.action, args.client_id,
                ttl_hours=args.ttl_hours,
                reveal_and_confirm=_trusted_tty_confirmation))
        else:
            if args.action != "revoke":
                _validate_ttl(args.ttl_hours)
            print(plan_change(args.store, args.action, args.client_id))
        return 0
    except ProvisioningError as exc:
        print(f"Credential action refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
