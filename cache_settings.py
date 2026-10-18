"""Validated, live-applied nonsecret cache lifetimes.

Existing entries keep their original expiration; query data stays in memory.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import stat

import safe_settings

SCHEMA = "local-search-mcp/cache-ttl/v1"
FILENAME = "cache-ttl.json"
DEFAULTS = {"search_seconds": 45, "fetch_seconds": 120}
BOUNDS = {"search_seconds": (5, 900), "fetch_seconds": (5, 1800)}
OVERRIDES = {
    "search_seconds": "LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL",
    "fetch_seconds": "LOCAL_SEARCH_MCP_CACHE_FETCH_TTL",
}
MAX_FILE_BYTES = 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _read_bytes(directory_fd: int | None) -> bytes:
    if directory_fd is None:
        return b""
    try:
        fd = os.open(FILENAME, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return b""
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_FILE_BYTES):
            raise ValueError("Unsafe or oversized cache TTL settings file")
        data = os.read(fd, MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Oversized cache TTL settings file")
        return data
    finally:
        os.close(fd)


def _parse(raw: bytes) -> dict[str, int]:
    if not raw:
        return dict(DEFAULTS)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid cache TTL settings JSON") from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "settings"}
            or data["schema"] != SCHEMA):
        raise ValueError("Unsupported cache TTL settings schema")
    values = data["settings"]
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Unexpected cache TTL settings fields")
    for key, (low, high) in BOUNDS.items():
        value = values[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid {key} setting")
    return dict(values)


def _snapshot() -> tuple[dict[str, int], str]:
    fd = safe_settings._open_private_directory()
    try:
        raw = _read_bytes(fd)
        return _parse(raw), hashlib.sha256(raw).hexdigest()
    finally:
        if fd is not None:
            os.close(fd)


def dashboard_settings() -> dict:
    values, state = _snapshot()
    overrides = {key: name in os.environ for key, name in OVERRIDES.items()}
    return {"values": values, "state": state,
            "editable": not any(overrides.values()),
            "environment_override": overrides}


def effective_ttl(key: str) -> int:
    """Read each operation's setting; malformed storage cannot disable tools."""
    if key not in DEFAULTS:
        raise ValueError("Unknown cache TTL setting")
    try:
        override = OVERRIDES[key]
        if override in os.environ:
            value = int(os.environ[override])
            low, high = BOUNDS[key]
            if not low <= value <= high:
                raise ValueError("Invalid cache TTL environment override")
            return value
        return _snapshot()[0][key]
    except (OSError, ValueError) as exc:
        logging.warning("Cache TTL setting unavailable (%s); using default",
                        type(exc).__name__)
        return DEFAULTS[key]


def update_settings(fields: dict[str, str], expected_state: str) -> None:
    """Validate all fields then atomically CAS a strictly allowlisted file."""
    if any(name in os.environ for name in OVERRIDES.values()):
        raise ValueError("Environment overrides are active; unset and restart before editing")
    if not isinstance(expected_state, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_state):
        raise ValueError("Missing or invalid settings version; reload the page")
    if not isinstance(fields, dict) or set(fields) != set(DEFAULTS):
        raise ValueError("Unexpected cache TTL settings fields")
    values = {}
    for key, (low, high) in BOUNDS.items():
        raw = fields[key]
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,3}", raw):
            raise ValueError(f"Invalid {key} setting")
        value = int(raw)
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        values[key] = value
    payload = (json.dumps({"schema": SCHEMA, "settings": values},
                          separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    fd = safe_settings._open_private_directory(create=True)
    tmp_name = ""
    try:
        lock_fd = os.open(".cache-ttl.lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW,
                          0o600, dir_fd=fd)
        try:
            info = os.fstat(lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ValueError("Unsafe cache TTL settings lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            raw = _read_bytes(fd)
            _parse(raw)  # Do not overwrite invalid files, even if CAS matches.
            if hashlib.sha256(raw).hexdigest() != expected_state:
                raise ValueError("Settings changed; refresh and review before saving")
            tmp_name = ".cache-ttl-" + secrets.token_hex(12)
            temp_fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                              0o600, dir_fd=fd)
            with os.fdopen(temp_fd, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, FILENAME, src_dir_fd=fd, dst_dir_fd=fd)
            tmp_name = ""
            os.fsync(fd)
        finally:
            os.close(lock_fd)
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name, dir_fd=fd)
            except FileNotFoundError:
                pass
        os.close(fd)
