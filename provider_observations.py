"""Read-only bounded provider observations from existing private JSONL audit.

No raw queries, result titles, URLs, snippets, error payloads, or credentials
leave this module. No database writes, new provider tracking, or searches.
These are *observed result sources*, not per-provider uptime/latency metrics.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat

from history_log import history_path

WINDOWS = (1, 24, 168)
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 65_536
MAX_EVENTS = 20_000
MAX_GROUPS = 24
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 () ._-]{0,39}\Z")
_SECRET_WORDS = re.compile(r"(?i)(?:token|password|secret|bearer|cookie|apikey|api_key)")
_CATEGORIES = frozenset(("general", "web", "images", "news", "videos", "music",
                         "files", "it", "science", "social media", "books",
                         "maps", "other"))


def _label(value: object) -> str:
    if (not isinstance(value, str) or not _NAME.fullmatch(value)
            or _SECRET_WORDS.search(value)):
        return ""
    return value


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not 15 <= len(value) <= 40:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(timezone.utc) if result.tzinfo is not None else None


def _read_tail(path: Path) -> tuple[list[bytes], bool]:
    """Fixed-size read; reject links/special files without modifying the host."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("History input is not a regular file")
        start = max(0, info.st_size - MAX_FILE_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, min(info.st_size, MAX_FILE_BYTES))
        incomplete = start > 0 or start + len(data) < info.st_size
        if start:
            # First line could start midway through a UTF-8 or JSON value.
            i = data.find(b"\n")
            data = data[i + 1:] if i >= 0 else b""
        # An in-flight JSONL append may leave an incomplete last line.
        if not data.endswith(b"\n"):
            incomplete = True
        return data.split(b"\n")[:-1], incomplete
    finally:
        os.close(fd)



def _source_snapshot(files: list[Path]) -> tuple[list[tuple[list[bytes], bool]], bool]:
    """Read all source tails under the writer's EXISTING history lock.

    Do not create or chmod the lock, history files, a database, or a sidecar.
    If older/manual history has no lock file, return a best-effort sample
    marked as non-atomic rather than falsely certifying consistency.
    """
    lock = files[0].parent / ".history.lock"
    fd = None
    locked = False
    try:
        try:
            fd = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        if fd is not None:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("Invalid private history lock")
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            locked = True
        snapshot = []
        for source in files:
            try:
                snapshot.append(_read_tail(source))
            except FileNotFoundError:
                continue
        return snapshot, fd is not None
    finally:
        if fd is not None:
            try:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

def _rollup(values: dict[str, list[float | None]]) -> list[dict]:
    top = sorted(values.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    result = []
    for name, samples in top[:MAX_GROUPS]:
        measured = [ms for ms in samples if ms is not None]
        result.append({"name": name, "observed_searches": len(samples),
                       "timed_searches": len(measured),
                       "mean_search_call_ms": round(sum(measured) / len(measured), 1)
                       if measured else None})
    return result


def observations(hours: int = 24, *, now: datetime | None = None) -> dict:
    """Summarize private retained metadata only, with explicit missingness."""
    if type(hours) is not int or hours not in WINDOWS:
        raise ValueError("hours must be 1, 24 or 168")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    path = history_path()
    if path.parent.is_symlink():
        raise OSError("History directory is a symlink")
    engine_samples: dict[str, list[float | None]] = defaultdict(list)
    category_samples: dict[str, list[float | None]] = defaultdict(list)
    calls = errors = seen_sources = unattributed = 0
    malformed = scanned = scanned_files = 0
    limited = False
    files = [path] + [path.with_name(path.name + f".{i}") for i in (1, 2, 3)]
    batches, snapshot_locked = _source_snapshot(files)
    existing = len(batches)
    scanned_files = existing
    if existing and not snapshot_locked:
        limited = True
    for rows, truncated in batches:
        limited |= truncated
        for raw in reversed(rows):
            if scanned >= MAX_EVENTS:
                limited = True
                break
            scanned += 1
            if not raw or len(raw) > MAX_EVENT_BYTES:
                malformed += 1
                continue
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            if not isinstance(item, dict) or item.get("kind") != "search":
                continue
            when = _time(item.get("time_utc"))
            if when is None or when < cutoff or when > now + timedelta(minutes=5):
                continue
            calls += 1
            if item.get("status") != "success":
                errors += 1
                continue
            duration = item.get("duration_ms")
            if (type(duration) not in (int, float) or not 0 <= duration <= 3_600_000):
                duration = None
            categories = item.get("categories")
            group_names = set()
            if isinstance(categories, str) and len(categories) <= 320:
                group_names.update(n for value in categories.split(",")[:8]
                                   if (n := _label(value.strip().lower())) in _CATEGORIES)
            if not group_names:
                group_names = {"unspecified"}
            for category in group_names:
                category_samples[category].append(duration)
            result_rows = item.get("results")
            engines = set()
            if isinstance(result_rows, list):
                for row in result_rows[:5]:
                    if isinstance(row, dict):
                        name = _label(row.get("engine"))
                        if name:
                            engines.add(name)
            if engines:
                seen_sources += 1
                for name in engines:
                    engine_samples[name].append(duration)
            else:
                unattributed += 1
        if scanned >= MAX_EVENTS:
            break
    limited |= malformed > 0
    return {
        "available": existing > 0,
        "source_snapshot_lock_acquired": snapshot_locked,
        "hours": hours,
        "sampled_search_calls": calls,
        "non_success_search_calls": errors,
        "search_calls_with_observed_sources": seen_sources,
        "search_calls_without_observed_sources": unattributed,
        "source_files_scanned": scanned_files,
        "records_examined": scanned,
        "malformed_records_ignored": malformed,
        "retention_or_scan_limited": limited,
        "engine_groups_truncated": len(engine_samples) > MAX_GROUPS,
        "category_groups_truncated": len(category_samples) > MAX_GROUPS,
        "observed_result_engines": _rollup(engine_samples),
        "requested_categories": _rollup(category_samples),
        "scope": "retained private audit; first five result rows per search",
        "latency_scope": "total local search tool call time, not engine latency",
        "provider_failure_coverage": "unavailable_from_existing_history",
        "provider_uptime": "not_measured",
        "per_engine_category_link": "not_retained",
        "cache_hit_coverage": "not_retained",
    }
