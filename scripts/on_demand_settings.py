"""Private, opt-in on-demand backend preferences. No Docker or service effects."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from scripts.on_demand_eligibility import validate_options

SCHEMA = "local-search-mcp/on-demand/v1"
NAME = "on-demand.json"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class SettingsError(ValueError):
    """An unsafe or invalid on-demand preference must fail closed."""


def directory() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or
                str(Path.home() / ".config")).expanduser()
    return base / "local-search-mcp"


def _private_directory(folder: Path, *, create: bool) -> bool:
    if create and not folder.exists() and not folder.is_symlink():
        folder.mkdir(parents=True, mode=0o700)
    try:
        item = folder.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid()
            or item.st_mode & 0o077):
        raise SettingsError("On-demand settings directory is not owner-only")
    return True


def _private_file(path: Path) -> None:
    item = path.lstat()
    if (not stat.S_ISREG(item.st_mode) or item.st_uid != os.getuid()
            or stat.S_IMODE(item.st_mode) != 0o600 or item.st_size > 2048):
        raise SettingsError("On-demand settings file has unsafe type, owner, mode or size")


def _no_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SettingsError("Duplicate settings JSON keys are unsafe")
        result[key] = value
    return result


def read(*, folder: Path | None = None) -> dict:
    """Read strict versioned settings, default OFF only when no file exists."""
    folder = directory() if folder is None else folder
    defaults = validate_options()
    if not _private_directory(folder, create=False):
        return defaults
    path = folder / NAME
    try:
        _private_file(path)
    except FileNotFoundError:
        return defaults
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            raw = source.read(2049)
        if len(raw) > 2048:
            raise SettingsError("On-demand settings exceed size limit")
        document = json.loads(raw, object_pairs_hook=_no_duplicate_json_keys)
        if type(document) is not dict or set(document) != {"schema", "settings"}:
            raise SettingsError("Unexpected on-demand settings schema")
        if document["schema"] != SCHEMA or type(document["settings"]) is not dict:
            raise SettingsError("Unsupported on-demand settings schema")
        saved = document["settings"]
        if set(saved) != {"enabled", "idle_minutes"}:
            raise SettingsError("Unexpected on-demand setting")
        return validate_options(**saved)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SettingsError("On-demand settings unreadable or unsafe") from exc


def save(*, enabled: bool, idle_minutes: int = 30,
         folder: Path | None = None) -> dict:
    """Atomically save validated preferences; activation is a separate gate."""
    settings = validate_options(enabled=enabled, idle_minutes=idle_minutes)
    folder = directory() if folder is None else folder
    _private_directory(folder, create=True)
    path = folder / NAME
    try:
        _private_file(path)
    except FileNotFoundError:
        pass
    content = (json.dumps({"schema": SCHEMA, "settings": settings},
                          sort_keys=True, separators=(",", ":")) + "\n").encode()
    tempname = None
    try:
        fd, tempname = tempfile.mkstemp(prefix=".on-demand-", dir=folder)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        _private_directory(folder, create=False)
        try:
            _private_file(path)
        except FileNotFoundError:
            pass
        os.replace(tempname, path)
        tempname = None
        dirfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if tempname is not None:
            os.unlink(tempname)
    return settings
