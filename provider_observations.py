"""Read-only bounded provider observations from private JSONL history.

No raw queries, result titles, URLs, snippets, error payloads, or credentials
leave this report. No database writes or searches are performed here.
Result source attribution and measured isolated local SearXNG operations are
separate measurements; neither proves remote provider uptime/latency.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
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
    reported_failures: dict[str, int] = defaultdict(int)
    isolated_attempts: dict[str, list[dict]] = defaultdict(list)
    display_labels: dict[str, str] = {}
    reported_diagnostic_calls = partial_diagnostic_calls = unknown_diagnostic_calls = 0
    no_failures_reported_calls = omitted_diagnostic_names = 0
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
            # Actual isolated local /search operations, never inferred from
            # fanout hits, and absent on cache hits or preflight rejection.
            raw_attempts = item.get("isolated_engine_operations")
            if isinstance(raw_attempts, list):
                for item_attempt in raw_attempts[:3]:
                    if not isinstance(item_attempt, dict):
                        continue
                    label = _label(item_attempt.get("engine"))
                    outcome = item_attempt.get("outcome")
                    source = item_attempt.get("source")
                    duration = item_attempt.get("roundtrip_ms")
                    if (not label or outcome not in (
                            "results", "empty", "reported_unresponsive",
                            "http_error", "transport_error", "invalid_response",
                            "unverified_attribution")
                            or source not in ("explicit", "isolated_recovery", "paced_default", "paced_category")
                            or type(duration) not in (int, float)
                            or not math.isfinite(duration)
                            or not 0 <= duration <= 3_600_000):
                        continue
                    isolated_attempts[label].append({
                        "outcome": outcome, "source": source,
                        "roundtrip_ms": duration,
                    })
            # Old/non-opted-in events have no provider-failure evidence.
            # These are upstream-reported labels, never a provider uptime
            # denominator or measured provider-specific response time.
            diag = item.get("provider_diagnostics")
            if item.get("provider_diagnostics_opt_in") is True and isinstance(diag, dict):
                coverage = diag.get("coverage")
                if coverage in ("partial", "unverifiable", "no_failures_reported"):
                    reported_diagnostic_calls += 1
                    if coverage == "partial":
                        partial_diagnostic_calls += 1
                    elif coverage == "unverifiable":
                        unknown_diagnostic_calls += 1
                    else:
                        no_failures_reported_calls += 1
                    names = diag.get("unresponsive_engines")
                    distinct = set()
                    suppressed = diag.get("suppressed_or_unparsed_names")
                    if type(suppressed) is int and 0 <= suppressed <= 144:
                        omitted_diagnostic_names += suppressed
                    if coverage == "partial" and isinstance(names, list):
                        for name in names[:16]:
                            safe = _label(name)
                            if not safe:
                                omitted_diagnostic_names += 1
                            elif safe.casefold() not in distinct:
                                key = safe.casefold()
                                distinct.add(key)
                                display_labels.setdefault(key, safe)
                                reported_failures[key] += 1
                    if diag.get("truncated") is True:
                        limited = True
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
        "isolated_engine_operation_total": sum(
            len(items) for items in isolated_attempts.values()),
        "isolated_engine_operations": [
            {
                "name": name,
                "local_operations": len(items),
                "reported_results": sum(x["outcome"] == "results" for x in items),
                "reported_empty": sum(x["outcome"] == "empty" for x in items),
                "reported_unresponsive": sum(
                    x["outcome"] == "reported_unresponsive" for x in items),
                "other_outcomes": sum(
                    x["outcome"] not in ("results", "empty", "reported_unresponsive")
                    for x in items),
                "recovery_operations": sum(
                    x["source"] == "isolated_recovery" for x in items),
                "mean_local_search_ms": round(
                    sum(x["roundtrip_ms"] for x in items) / len(items), 1),
            }
            for name, items in sorted(
                isolated_attempts.items(),
                key=lambda x: (-len(x[1]), x[0]))[:MAX_GROUPS]
        ],
        "isolated_engine_groups_truncated": len(isolated_attempts) > MAX_GROUPS,
        "requested_categories": _rollup(category_samples),
        "opted_in_diagnostic_calls": reported_diagnostic_calls,
        "reported_partial_provider_calls": partial_diagnostic_calls,
        "reported_unknown_provider_calls": unknown_diagnostic_calls,
        "reported_no_failures_calls": no_failures_reported_calls,
        "provider_labels_suppressed": omitted_diagnostic_names,
        "reported_unresponsive_engines": [
            {"name": display_labels[name], "reported_unresponsive_calls": count}
            for name, count in sorted(
                reported_failures.items(), key=lambda row: (-row[1], row[0])
            )[:MAX_GROUPS]
        ],
        "provider_failure_groups_truncated": len(reported_failures) > MAX_GROUPS,
        "scope": "retained private audit; first five result rows per search",
        "latency_scope": "total local search tool call time, not engine latency",
        "provider_failure_coverage": ("per_call_opt_in_incomplete"
                                      if reported_diagnostic_calls else "no_opt_in_reports_retained"),
        "provider_uptime": "not_measured",
        "isolated_latency_scope": "local SearXNG /search roundtrip including retries, not remote engine execution",
        "per_engine_category_link": "not_retained",
        "cache_hit_coverage": "not_retained",
    }
