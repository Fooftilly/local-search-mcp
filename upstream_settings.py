"""Private, live-applied upstream request timeouts; never stores secrets.

Separate from history-settings.json so preexisting history settings and
rollback remain compatible. Stored values are only bounded integers.
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
from typing import NamedTuple

import safe_settings

SCHEMA = "local-search-mcp/upstream-timeouts/v1"
FILENAME = "upstream-timeouts.json"
DEFAULTS = {"searxng_seconds": 25, "crawl_seconds": 75}
BOUNDS = {"searxng_seconds": (5, 120), "crawl_seconds": (15, 300)}
OVERRIDES = {
    "searxng_seconds": "LOCAL_SEARCH_MCP_SEARXNG_TIMEOUT",
    "crawl_seconds": "LOCAL_SEARCH_MCP_CRAWL_TIMEOUT",
}
MAX_FILE_BYTES = 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class SettingsProfile(NamedTuple):
    schema: str
    filename: str
    defaults: dict[str, int]
    bounds: dict[str, tuple[int, int]]
    overrides: dict[str, str]
    label: str
    lock_name: str
    temporary_prefix: str
    max_digits: int


UPSTREAM_PROFILE = SettingsProfile(
    SCHEMA, FILENAME, DEFAULTS, BOUNDS, OVERRIDES, "upstream settings",
    ".upstream-timeouts.lock", ".upstream-timeouts-", 3,
)


def _unique_json_members(pairs: list[tuple[str, object]]) -> dict:
    """Reject JSON objects with duplicate keys before accepting private data."""
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Duplicate settings JSON member")
        values[key] = value
    return values


def _read_bytes(directory_fd: int | None, profile: SettingsProfile = UPSTREAM_PROFILE) -> bytes:
    if directory_fd is None:
        return b""
    try:
        fd = os.open(profile.filename, os.O_RDONLY | _NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return b""
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_FILE_BYTES):
            raise ValueError(f"Unsafe or oversized {profile.label} file")
        data = os.read(fd, MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"Oversized {profile.label} file")
        return data
    finally:
        os.close(fd)


def _parse(raw: bytes, profile: SettingsProfile = UPSTREAM_PROFILE) -> dict[str, int]:
    if not raw:
        return dict(profile.defaults)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_members)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid {profile.label} JSON") from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "settings"}
            or data["schema"] != profile.schema):
        raise ValueError(f"Unsupported {profile.label} schema")
    values = data["settings"]
    if not isinstance(values, dict) or set(values) != set(profile.defaults):
        raise ValueError(f"Unexpected {profile.label} fields")
    for key, (low, high) in profile.bounds.items():
        value = values[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid {key} setting")
    return dict(values)


def _snapshot(profile: SettingsProfile = UPSTREAM_PROFILE) -> tuple[dict[str, int], str]:
    fd = safe_settings._open_private_directory()
    try:
        raw = _read_bytes(fd, profile)
        return _parse(raw, profile), hashlib.sha256(raw).hexdigest()
    finally:
        if fd is not None:
            os.close(fd)


def dashboard_settings(profile: SettingsProfile = UPSTREAM_PROFILE) -> dict:
    values, state = _snapshot(profile)
    overrides = {key: name in os.environ for key, name in profile.overrides.items()}
    return {"values": values, "state": state,
            "editable": not any(overrides.values()),
            "environment_override": overrides}


def effective_timeout(key: str, *, profile: SettingsProfile = UPSTREAM_PROFILE) -> int:
    """Read each operation's setting; malformed storage cannot disable tools."""
    if key not in profile.defaults:
        raise ValueError(f"Unknown {profile.label} setting")
    try:
        override = profile.overrides[key]
        if override in os.environ:
            value = int(os.environ[override])
            low, high = profile.bounds[key]
            if not low <= value <= high:
                raise ValueError(f"Invalid {profile.label} environment override")
            return value
        return _snapshot(profile)[0][key]
    except (OSError, ValueError) as exc:
        logging.warning("%s setting unavailable (%s); using default",
                        profile.label.capitalize(), type(exc).__name__)
        return profile.defaults[key]


def update_settings(fields: dict[str, str], expected_state: str, *,
                    profile: SettingsProfile = UPSTREAM_PROFILE) -> None:
    """Validate all fields then atomically CAS a strictly allowlisted file."""
    if any(name in os.environ for name in profile.overrides.values()):
        raise ValueError("Environment overrides are active; unset and restart before editing")
    if not isinstance(expected_state, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_state):
        raise ValueError("Missing or invalid settings version; reload the page")
    if not isinstance(fields, dict) or set(fields) != set(profile.defaults):
        raise ValueError(f"Unexpected {profile.label} fields")
    values = {}
    for key, (low, high) in profile.bounds.items():
        raw = fields[key]
        if not isinstance(raw, str) or not re.fullmatch(rf"[0-9]{{1,{profile.max_digits}}}", raw):
            raise ValueError(f"Invalid {key} setting")
        value = int(raw)
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        values[key] = value
    payload = (json.dumps({"schema": profile.schema, "settings": values},
                          separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    fd = safe_settings._open_private_directory(create=True)
    tmp_name = ""
    try:
        lock_fd = os.open(profile.lock_name, os.O_RDWR | os.O_CREAT | _NOFOLLOW,
                          0o600, dir_fd=fd)
        try:
            info = os.fstat(lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ValueError(f"Unsafe {profile.label} lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            raw = _read_bytes(fd, profile)
            _parse(raw, profile)  # Do not overwrite invalid files, even if CAS matches.
            if hashlib.sha256(raw).hexdigest() != expected_state:
                raise ValueError("Settings changed; refresh and review before saving")
            tmp_name = profile.temporary_prefix + secrets.token_hex(12)
            temp_fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                              0o600, dir_fd=fd)
            with os.fdopen(temp_fd, "wb") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, profile.filename, src_dir_fd=fd, dst_dir_fd=fd)
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
