"""Validated, local-only nonsecret history settings, separate from credentials.

Two history settings are effective on their next history operation without
restarting services. Environment overrides remain authoritative. This module
never loads or modifies bootstrap's credential-bearing config.json.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

SCHEMA = "local-search-mcp/nonsecret-history/v1"
FILENAME = "history-settings.json"
DEFAULTS = {"retention_days": 30, "max_bytes": 8_388_608}
BOUNDS = {"retention_days": (1, 365), "max_bytes": (64_000, 100_000_000)}
OVERRIDES = {
    "retention_days": "LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS",
    "max_bytes": "LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES",
}
MAX_FILE_BYTES = 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _directory() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or
                (Path.home() / ".config")).expanduser()
    return root / "local-search-mcp"


def _open_private_directory(*, create: bool = False) -> int | None:
    directory = _directory()
    if create and not directory.exists():
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    except FileNotFoundError:
        if not create:
            return None
        raise
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError("Private settings directory must be owner-only (0700)")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_bytes(fd: int | None) -> bytes:
    if fd is None:
        return b""
    try:
        file_fd = os.open(FILENAME, os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return b""
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_FILE_BYTES):
            raise ValueError("Unsafe or oversized nonsecret settings file")
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Oversized nonsecret settings file")
        return data
    finally:
        os.close(file_fd)


def _validate(data: dict) -> dict[str, int]:
    if not isinstance(data, dict) or set(data) != {"schema", "settings"}:
        raise ValueError("Unsupported nonsecret settings schema")
    if data["schema"] != SCHEMA:
        raise ValueError("Unsupported nonsecret settings version")
    vals = data["settings"]
    if not isinstance(vals, dict) or set(vals) != set(DEFAULTS):
        raise ValueError("Unexpected nonsecret settings fields")
    result = {}
    for key, (low, high) in BOUNDS.items():
        value = vals[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid {key} setting")
        result[key] = value
    return result


def _parse(raw: bytes) -> dict[str, int]:
    if not raw:
        return dict(DEFAULTS)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid nonsecret settings JSON") from exc
    return _validate(document)


def _snapshot() -> tuple[dict[str, int], str]:
    fd = _open_private_directory()
    try:
        raw = _read_bytes(fd)
        return _parse(raw), hashlib.sha256(raw).hexdigest()
    finally:
        if fd is not None:
            os.close(fd)


def effective_value(key: str) -> int:
    if key not in DEFAULTS:
        raise ValueError("Unknown nonsecret setting")
    override = OVERRIDES[key]
    if override in os.environ:
        try:
            value = int(os.environ[override])
        except ValueError as exc:
            raise ValueError(f"Invalid environment override for {key}") from exc
        low, high = BOUNDS[key]
        if key == "max_bytes":
            # Preserve the previous bounded history rotation behavior.
            return max(low, min(value, high))
        if not low <= value <= high:
            raise ValueError(f"Invalid environment override for {key}")
        return value
    return _snapshot()[0][key]


def dashboard_settings() -> dict:
    """Nonsecret values and source labels only; never include config paths."""
    values, fingerprint = _snapshot()
    env_active = {key: OVERRIDES[key] in os.environ for key in DEFAULTS}
    return {"values": values, "state": fingerprint, "editable": not any(env_active.values()),
            "environment_override": env_active}


def update_settings(fields: dict[str, str], expected_state: str) -> None:
    """Atomic compare-and-swap of an allowlisted settings file, never secrets."""
    if any(key in os.environ for key in OVERRIDES.values()):
        raise ValueError("Environment overrides are active; unset and restart before editing")
    if not isinstance(expected_state, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_state):
        raise ValueError("Missing or invalid settings version; reload the page")
    if not isinstance(fields, dict) or set(fields) != set(DEFAULTS):
        raise ValueError("Unexpected settings fields")
    values = {}
    for key, (low, high) in BOUNDS.items():
        raw = fields[key]
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,9}", raw):
            raise ValueError(f"Invalid {key} setting")
        val = int(raw)
        if not low <= val <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        values[key] = val
    payload = (json.dumps({"schema": SCHEMA, "settings": values},
                          separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError("Settings document too large")
    directory_fd = _open_private_directory(create=True)
    tmp_name = ""
    try:
        lock_fd = os.open(".history-settings.lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW,
                          0o600, dir_fd=directory_fd)
        try:
            lock_info = os.fstat(lock_fd)
            if (not stat.S_ISREG(lock_info.st_mode) or
                    lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077):
                raise ValueError("Unsafe settings lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            raw = _read_bytes(directory_fd)
            _parse(raw)  # Never overwrite malformed or unsupported data.
            if hashlib.sha256(raw).hexdigest() != expected_state:
                raise ValueError("Settings changed; refresh and review before saving")
            tmp_name = ".history-settings-" + secrets.token_hex(12)
            tmp_fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                             0o600, dir_fd=directory_fd)
            with os.fdopen(tmp_fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp_name, FILENAME, src_dir_fd=directory_fd,
                       dst_dir_fd=directory_fd)
            tmp_name = ""
            os.fsync(directory_fd)
        finally:
            os.close(lock_fd)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
