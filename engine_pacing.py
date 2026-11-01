"""Inert-by-default, cross-process admission primitive for isolated SearXNG engines.

The search router must opt in explicitly: this module does not change existing
searches, silently select engines, manage SearXNG YAML, or create a dashboard
configuration interface. An exclusive file lock spans the admitted network
operation; the spacing reservation is committed before that operation starts.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

SCHEMA = "local-search-mcp/engine-pacing/v1"
FILENAME = "engine-pacing.json"
MAX_POLICY_BYTES = 4096
MAX_STATE_BYTES = 256
MAX_SPACING_SECONDS = 3600
MAX_ENGINES = 64
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_ENGINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}\Z", re.ASCII)


class PaceUnavailable(ValueError):
    """Fail-closed admission error with a nonsecret, bounded retry hint."""

    def __init__(self, reason: str, retry_after_seconds: int = 0):
        super().__init__(reason)
        self.retry_after_seconds = max(0, min(3600, retry_after_seconds))


def _private_dir(*, create: bool = False) -> int | None:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = root / "local-search-mcp"
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    except FileNotFoundError:
        if not create:
            return None
        raise
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise PaceUnavailable("Unsafe private pacing settings directory")
    return fd


def _read_regular(directory: int, name: str, limit: int) -> bytes | None:
    try:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > limit):
            raise PaceUnavailable("Unsafe private pacing file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise PaceUnavailable("Oversized private pacing file")
        return data
    finally:
        os.close(fd)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise PaceUnavailable("Duplicate pacing setting")
        result[name] = value
    return result


def _parse(raw: bytes, *, preserve_case: bool = False) -> dict[str, int] | None:
    """Return None when disabled; otherwise validated per-engine minimum gaps.

    No arbitrary provider names, wildcard provider groups, defaults, or
    fallback selections are supported. An enabled policy with zero engines
    is rejected rather than implicitly allowing all engines.
    """
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, ValueError) as exc:
        raise PaceUnavailable("Invalid private pacing policy") from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "enabled", "engines"}
            or data["schema"] != SCHEMA or type(data["enabled"]) is not bool
            or not isinstance(data["engines"], dict)
            or len(data["engines"]) > MAX_ENGINES):
        raise PaceUnavailable("Invalid private pacing schema")
    engines: dict[str, int] = {}
    seen: set[str] = set()
    for name, settings in data["engines"].items():
        if (not isinstance(name, str) or not _ENGINE.fullmatch(name)
                or name != name.strip() or name.casefold().startswith("google")
                or name.casefold() in seen
                or not isinstance(settings, dict)
                or set(settings) != {"min_spacing_seconds"}):
            raise PaceUnavailable("Invalid private pacing engine entry")
        seconds = settings["min_spacing_seconds"]
        if type(seconds) is not int or not 0 <= seconds <= MAX_SPACING_SECONDS:
            raise PaceUnavailable("Invalid private pacing interval")
        seen.add(name.casefold())
        engines[name if preserve_case else name.casefold()] = seconds
    if data["enabled"] and not engines:
        raise PaceUnavailable("Enabled pacing requires explicit engine entries")
    return engines if data["enabled"] else None


def policy(*, preserve_case: bool = False) -> dict[str, int] | None:
    """Read only owner-controlled settings; missing policy is safely off.

    Never treat a malformed or insecure policy as disabled when the caller
    explicitly enabled engine pacing. The router can therefore fail closed.
    preserve_case=True keeps exact configured engine labels for matching
    SearXNG /config; the default retains canonical casefold keys for readers.
    """
    directory = _private_dir()
    if directory is None:
        return None
    try:
        raw = _read_regular(directory, FILENAME, MAX_POLICY_BYTES)
        return _parse(raw, preserve_case=preserve_case) if raw is not None else None
    finally:
        os.close(directory)


def _locked_fd(directory: int, name: str) -> int:
    fd = os.open(name, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600,
                 dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise PaceUnavailable("Unsafe engine pacing lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PaceUnavailable("Selected engine already has an active search", 1) from exc
        return fd
    except BaseException:
        os.close(fd)
        raise



def _boot_id() -> str:
    """Identify a Linux boot so monotonic deadlines survive process restarts."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise PaceUnavailable("System boot identity unavailable for safe pacing") from exc
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise PaceUnavailable("Invalid system boot identity")
    return value

def _save_next(directory: int, name: str, *, epoch: float, mono: float, boot: str, spacing: int) -> None:
    tmp = ".engine-pacing-" + secrets.token_hex(12)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps({"next_allowed_epoch": epoch, "next_allowed_mono": mono,
                                     "boot_id": boot, "spacing_seconds": spacing}) + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
        tmp = ""
        os.fsync(directory)
    finally:
        if tmp:
            try:
                os.unlink(tmp, dir_fd=directory)
            except FileNotFoundError:
                pass


@contextmanager
def reserve(engine: str, spacing_seconds: int):
    """Nonblocking per-engine, cross-process admission and restart-safe gap.

    The caller MUST already have selected/verified exactly this engine and
    must hold this context around the real /search network request. This is
    not a selector: combined searches, categories and !bang queries must be
    checked by the router separately before using this primitive.
    """
    if (not isinstance(engine, str) or not _ENGINE.fullmatch(engine)
            or engine != engine.strip() or engine.casefold().startswith("google")
            or type(spacing_seconds) is not int
            or not 0 <= spacing_seconds <= MAX_SPACING_SECONDS):
        raise PaceUnavailable("Invalid isolated engine pacing request")
    directory = _private_dir(create=True)
    try:
        suffix = hashlib.sha256(engine.casefold().encode("ascii")).hexdigest()[:32]
        lock = _locked_fd(directory, ".engine-pacing-" + suffix + ".lock")
        try:
            state_name = ".engine-pacing-" + suffix + ".json"
            raw = _read_regular(directory, state_name, MAX_STATE_BYTES)
            now = time.time()
            now_mono = time.monotonic()
            boot = _boot_id()
            if (not math.isfinite(now) or now < 0
                    or not math.isfinite(now_mono) or now_mono < 0):
                raise PaceUnavailable("Invalid system clock for pacing")
            if raw is not None:
                try:
                    record = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_pairs)
                    if (not isinstance(record, dict)
                            or set(record) != {"next_allowed_epoch", "next_allowed_mono",
                                               "boot_id", "spacing_seconds"}
                            or not isinstance(record["boot_id"], str)
                            or not re.fullmatch(
                                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                                record["boot_id"])
                            or type(record["spacing_seconds"]) is not int
                            or not 0 <= record["spacing_seconds"] <= MAX_SPACING_SECONDS
                            or any(type(record[key]) not in (int, float)
                                   or not math.isfinite(record[key]) or record[key] < 0
                                   for key in ("next_allowed_epoch", "next_allowed_mono"))):
                        raise ValueError("invalid pacing state")
                except (ValueError, UnicodeError) as exc:
                    raise PaceUnavailable("Invalid private pacing state") from exc
                if record["boot_id"] != boot:
                    # After reboot elapsed monotonic time cannot be reconstructed.
                    # Reestablish the full positive gap before admitting traffic.
                    # A zero-gap engine has no cooldown to reestablish.
                    wait = max(spacing_seconds, record["spacing_seconds"])
                    if wait:
                        _save_next(directory, state_name, epoch=now + wait,
                                   mono=now_mono + wait, boot=boot, spacing=wait)
                        raise PaceUnavailable("Engine cooling down after system reboot", wait)
                else:
                    remaining = record["next_allowed_mono"] - now_mono
                    if remaining > MAX_SPACING_SECONDS:
                        raise PaceUnavailable("Pacing monotonic clock skew requires inspection")
                    if remaining > 0:
                        raise PaceUnavailable("Selected engine cooling down",
                                              math.ceil(remaining))
            _save_next(directory, state_name, epoch=now + spacing_seconds,
                       mono=now_mono + spacing_seconds, boot=boot,
                       spacing=spacing_seconds)
            yield
        finally:
            os.close(lock)
    finally:
        os.close(directory)
