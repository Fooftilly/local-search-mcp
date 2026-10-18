"""Private, indexed SQLite view over the existing rotating JSONL audit trail.

JSONL remains the source of truth and the old CLI/export formats stay intact.
The index is a rebuildable sidecar, without search queries, paths, page bodies
or credentials. Import coordinates with append_history's existing file lock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import urlsplit

from history_log import history_path

MAX_HISTORY_ROWS = 100_000
MAX_EVENT_BYTES = 65_536
MAX_QUERY_RESULTS = 100


def index_path() -> Path:
    target = history_path()
    return target.with_name(target.name + ".sqlite3")


def _retention_days() -> int:
    try:
        value = int(os.environ.get("LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS", "30"))
    except ValueError as exc:
        raise ValueError("History retention days must be an integer") from exc
    if not 1 <= value <= 365:
        raise ValueError("History retention days must be between 1 and 365")
    return value


def _time_ms(text: object) -> int | None:
    if not isinstance(text, str) or len(text) > 48:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return None
        return int(moment.timestamp() * 1000)
    except (OverflowError, ValueError):
        return None


def _origin_domain(event: dict) -> str:
    raw = event.get("url")
    if not isinstance(raw, str):
        return ""
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            return ""
        ipaddress.ip_address(host)
    except ValueError:
        # A normal DNS hostname raises ValueError in ipaddress.ip_address.
        return (host or "")[:253] if "host" in locals() else ""
    return ""


def _safe_event(event: object) -> tuple | None:
    if not isinstance(event, dict):
        return None
    at_ms = _time_ms(event.get("time_utc"))
    kind = event.get("kind")
    status = event.get("status")
    if at_ms is None or kind not in ("search", "fetch", "page"):
        return None
    if (not isinstance(status, str) or not 1 <= len(status) <= 48
            or any(ord(char) < 32 or ord(char) == 127 for char in status)):
        return None
    duration = event.get("duration_ms")
    duration = (min(duration, 3600_000) if type(duration) in (int, float)
                and 0 <= duration < float("inf") else None)
    backend = event.get("backend")
    backend = backend if backend in ("SearXNG", "Crawl4AI") else ""
    return (at_ms, kind, status, _origin_domain(event), duration, backend)


def _files(target: Path) -> list[Path]:
    return [p for p in (
        target.with_name(target.name + ".3"),
        target.with_name(target.name + ".2"),
        target.with_name(target.name + ".1"), target,
    ) if p.exists() or p.is_symlink()]


def sync_history() -> None:
    """Idempotently import complete lines, rotate safely, purge expired rows."""
    target = history_path()
    folder = target.parent
    index = index_path()
    lock_file = folder / ".history.lock"
    if folder.is_symlink() or index.is_symlink() or lock_file.is_symlink():
        raise OSError("Refusing symlinked history index or lock")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    fd = os.open(lock_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("History lock is not a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # Reject source symlinks before making a new index file.
            source_files = _files(target)
            if any(p.is_symlink() for p in source_files):
                raise OSError("Refusing symlinked history input")
            if index.is_symlink():
                raise OSError("Refusing symlinked SQLite history index")
            with closing(sqlite3.connect(index, timeout=10)) as conn:
                os.chmod(index, 0o600)
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("BEGIN IMMEDIATE")
                # The index is derived from JSONL: an early pre-release schema
                # without a source generation ID can be rebuilt safely.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(history)")}
                if columns and "generation_id" not in columns:
                    conn.execute("DROP TABLE IF EXISTS history")
                    conn.execute("DROP TABLE IF EXISTS sources")
                conn.execute("""CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS history (
                    generation_id INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL, at_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL,
                    domain TEXT NOT NULL, duration_ms REAL,
                    backend TEXT NOT NULL,
                    PRIMARY KEY(generation_id,byte_offset))""")
                conn.execute("""CREATE TABLE IF NOT EXISTS sources (
                    dev INTEGER NOT NULL, ino INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, generation_id INTEGER NOT NULL,
                    next_offset INTEGER NOT NULL,
                    checkpoint_offset INTEGER NOT NULL DEFAULT -1,
                    checkpoint_hash TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(dev,ino))""")
                conn.execute("CREATE INDEX IF NOT EXISTS hist_at ON history(at_ms DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS hist_kind_status_at "
                             "ON history(kind,status,at_ms DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS hist_domain_at "
                             "ON history(domain,at_ms DESC)")
                seen: set[tuple[int, int]] = set()
                cutoff = int((datetime.now(timezone.utc) -
                              timedelta(days=_retention_days())).timestamp() * 1000)
                for p in source_files:
                    if p.is_symlink():
                        raise OSError("Refusing symlinked rotated history")
                    with p.open("rb") as stream:
                        meta = os.fstat(stream.fileno())
                        if not stat.S_ISREG(meta.st_mode):
                            raise OSError("History input must be a regular file")
                        key = (meta.st_dev, meta.st_ino)
                        seen.add(key)
                        # Linux may immediately reuse a discarded .3 inode for
                        # a new active JSONL. Hash its first bounded line so
                        # a reused inode cannot silently inherit old offsets.
                        first_line = stream.readline(MAX_EVENT_BYTES + 1)
                        fingerprint = hashlib.sha256(first_line).hexdigest()
                        previous = conn.execute(
                            """SELECT fingerprint,generation_id,next_offset,
                                      checkpoint_offset,checkpoint_hash
                               FROM sources WHERE dev=? AND ino=?""", key
                        ).fetchone()
                        same_source = bool(previous and previous[0] == fingerprint
                                           and previous[2] <= meta.st_size)
                        if same_source and previous[3] >= 0:
                            # Also check the last imported line. This detects
                            # reuse even if a new file begins with identical
                            # content and happens to grow beyond the old size.
                            stream.seek(previous[3])
                            checkpoint = stream.readline(MAX_EVENT_BYTES + 1)
                            same_source = (
                                hashlib.sha256(checkpoint).hexdigest() == previous[4])
                        if same_source:
                            generation = previous[1]
                            offset = previous[2]
                            check_offset, check_hash = previous[3], previous[4]
                        else:
                            generation = conn.execute(
                                "INSERT INTO generations DEFAULT VALUES").lastrowid
                            offset = 0
                            check_offset, check_hash = -1, ""
                        stream.seek(offset)
                        while True:
                            pos = stream.tell()
                            line = stream.readline(MAX_EVENT_BYTES + 1)
                            if not line:
                                break
                            if not line.endswith(b"\n"):
                                if len(line) <= MAX_EVENT_BYTES:
                                    break  # short, incomplete tail: retry later
                                # Skip a complete oversized record in bounded
                                # pieces, without buffering arbitrary content.
                                complete = False
                                while True:
                                    chunk = stream.readline(MAX_EVENT_BYTES + 1)
                                    if not chunk:
                                        break
                                    if chunk.endswith(b"\n"):
                                        complete = True
                                        break
                                if not complete:
                                    break  # incomplete oversized tail
                                offset = stream.tell()
                                check_offset = pos
                                check_hash = hashlib.sha256(line).hexdigest()
                                continue
                            offset = stream.tell()
                            check_offset = pos
                            check_hash = hashlib.sha256(line).hexdigest()
                            if len(line) > MAX_EVENT_BYTES:
                                continue
                            try:
                                data = _safe_event(json.loads(line))
                            except (UnicodeError, json.JSONDecodeError):
                                continue
                            if data and data[0] >= cutoff:
                                conn.execute("""INSERT OR IGNORE INTO history
                                    (generation_id,byte_offset,at_ms,
                                     kind,status,domain,duration_ms,backend)
                                    VALUES (?,?,?,?,?,?,?,?)""",
                                    (generation, pos, *data))
                        conn.execute("""INSERT INTO sources
                            (dev,ino,fingerprint,generation_id,next_offset,
                             checkpoint_offset,checkpoint_hash)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(dev,ino)
                            DO UPDATE SET fingerprint=excluded.fingerprint,
                              generation_id=excluded.generation_id,
                              next_offset=excluded.next_offset,
                              checkpoint_offset=excluded.checkpoint_offset,
                              checkpoint_hash=excluded.checkpoint_hash""",
                            (*key, fingerprint, generation, offset,
                             check_offset, check_hash))
                # Inode/offset markers survive a rename during rotation, but
                # disappear when a source is no longer among retained files.
                for dev, ino in conn.execute("SELECT dev,ino FROM sources").fetchall():
                    if (dev, ino) not in seen:
                        conn.execute("DELETE FROM sources WHERE dev=? AND ino=?", (dev, ino))
                conn.execute("DELETE FROM history WHERE at_ms < ?", (cutoff,))
                conn.execute("""DELETE FROM history WHERE rowid IN (
                    SELECT rowid FROM history ORDER BY at_ms DESC, rowid DESC
                    LIMIT -1 OFFSET ?)""", (MAX_HISTORY_ROWS,))
                conn.commit()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


HISTORY_WINDOWS = (0, 1, 24, 168)


def _history_filter(kind: str, status: str, domain: str,
                    hours: int) -> tuple[str, list]:
    """Build one bounded, parameterized filter for rows and summary bars."""
    if kind not in ("", "search", "fetch", "page"):
        raise ValueError("Invalid history kind")
    if type(hours) is not int or hours not in HISTORY_WINDOWS:
        raise ValueError("Invalid history time window")
    if (not isinstance(status, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{0,48}", status)
            or not isinstance(domain, str) or len(domain) > 253
            or not re.fullmatch(r"[a-zA-Z0-9.-]*", domain)):
        raise ValueError("Invalid history filter")
    where = []
    args = []
    for col, term in (("kind", kind), ("status", status), ("domain", domain.lower())):
        if term:
            where.append(col + "=?")
            args.append(term)
    if hours:
        where.append("at_ms>=?")
        args.append(int((datetime.now(timezone.utc) -
                         timedelta(hours=hours)).timestamp() * 1000))
    return (" WHERE " + " AND ".join(where)) if where else "", args


def search_history(*, limit: int = 25, kind: str = "", status: str = "",
                   domain: str = "", hours: int = 0) -> list[dict]:
    """Return bounded newest-first metadata, with optional rolling time filter."""
    if type(limit) is not int or not 1 <= limit <= MAX_QUERY_RESULTS:
        raise ValueError("Invalid history page size")
    suffix, args = _history_filter(kind, status, domain, hours)
    sync_history()
    with closing(sqlite3.connect(index_path(), timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT at_ms,kind,status,domain,duration_ms,backend FROM history" +
            suffix + " ORDER BY at_ms DESC,rowid DESC LIMIT ?", (*args, limit))]


def summarize_history(*, kind: str = "", status: str = "",
                      domain: str = "", hours: int = 0) -> list[dict]:
    """Grouped operational counts for the same filters as the history table."""
    suffix, args = _history_filter(kind, status, domain, hours)
    sync_history()
    with closing(sqlite3.connect(index_path(), timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT kind,status,count(*) AS count,
               round(avg(duration_ms),1) AS avg_duration_ms
               FROM history""" + suffix +
            " GROUP BY kind,status ORDER BY count DESC,kind,status", args)]


def operational_metrics(hours: int = 24) -> dict:
    """Aggregate retained events only; never expose domains or private text.

    Metrics cover events that still exist in the rotating JSONL source and
    its retention-bounded SQLite index. Runtime cache counters are separate.
    """
    if type(hours) is not int or hours not in (1, 24, 168):
        raise ValueError("Metrics window must be 1, 24, or 168 hours")
    sync_history()
    cutoff_ms = int((datetime.now(timezone.utc) -
                     timedelta(hours=hours)).timestamp() * 1000)
    with closing(sqlite3.connect(index_path(), timeout=10)) as conn:
        rows = conn.execute(
            """SELECT kind,
                      CASE WHEN status IN (
                        'success', 'pending', 'denied', 'blocked', 'error',
                        'expired_or_unknown', 'approval_queue_full',
                        'upstream_unavailable', 'invalid_response',
                        'unavailable_extraction', 'continuation_evicted',
                        'crawl_failed', 'fetch_error', 'cancelled'
                      ) THEN status ELSE 'other' END AS safe_status,
                      count(*) AS n, round(avg(duration_ms),1) AS avg_ms
               FROM history WHERE at_ms>=?
               GROUP BY kind,safe_status ORDER BY kind,safe_status""",
            (cutoff_ms,)
        ).fetchall()
    # Keep the output schema fixed: no hostnames, URLs or search terms.
    outcomes = [
        {"tool": kind, "status": status, "count": count,
         "avg_duration_ms": average}
        for kind, status, count, average in rows
    ]
    return {"window_hours": hours, "total_events": sum(x["count"] for x in outcomes),
            "outcomes": outcomes}
