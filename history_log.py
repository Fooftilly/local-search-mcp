"""Private append-only audit history of local-search-mcp search and fetch calls.

Records metadata only, never API keys or fetched page content. Python stdlib only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import fcntl
import inspect
import json
import logging
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit, urlunsplit


HISTORY_ENV = "LOCAL_SEARCH_MCP_HISTORY_FILE"


def history_path() -> Path:
    override = os.environ.get(HISTORY_ENV)
    if override:
        return Path(override).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state").expanduser()
    return state_home / "local-search-mcp" / "history.jsonl"


def safe_url(value: object) -> str:
    """Avoid persisting URLs' query parameters, fragments, or userinfo."""
    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value)
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))[:2000]
    except ValueError:
        return "[invalid URL]"


def append_history(event: dict) -> None:
    """Bounded private JSONL history with serialized append and rotation."""
    target = history_path()
    directory = target.parent
    if directory.is_symlink() or target.is_symlink():
        raise OSError("Refusing symlink in history directory/file")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    max_bytes = max(64_000, min(int(os.getenv("LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES", "8388608")), 100_000_000))
    keep = 3
    line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    lock_path = directory / ".history.lock"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise OSError("History lock target is not a regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if target.is_symlink():
                raise OSError("Refusing symlinked history target")
            if target.exists() and target.stat().st_size + len(line) > max_bytes:
                for index in range(keep, 0, -1):
                    source = target.with_name(target.name + (f".{index-1}" if index > 1 else ""))
                    destination = target.with_name(target.name + f".{index}")
                    if source.is_symlink() or destination.is_symlink():
                        raise OSError("Refusing symlinked rotated history")
                    if source.exists():
                        os.replace(source, destination)
            fd = os.open(target, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("History target is not a regular file")
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "ab", closefd=False) as stream:
                    stream.write(line)
                    stream.flush()
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def logged_tool(kind: str):
    """Decorate a registered FastMCP tool while retaining its real signature."""
    if kind not in ("search", "fetch"):
        raise ValueError("Unrecognized history kind")

    def decorate(fn):
        sig = inspect.signature(fn)

        @wraps(fn)
        def wrapped(*args, **kwargs):
            bound = sig.bind_partial(*args, **kwargs)
            params = bound.arguments
            event: dict = {
                "time_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "kind": kind,
                "backend": "SearXNG" if kind == "search" else "Crawl4AI",
            }
            if kind == "search":
                event["query"] = str(params.get("query", ""))[:300]
                if "max_results" in params:
                    event["max_results"] = params["max_results"]
            else:
                event["url"] = safe_url(params.get("url"))
                try:
                    event["url_query_stripped"] = bool(urlsplit(str(params.get("url", ""))).query)
                except ValueError:
                    event["url_query_stripped"] = False
            started = time.monotonic()
            try:
                output = fn(*args, **kwargs)
                event["status"] = "success"
                if kind == "search" and isinstance(output, list):
                    event["result_count"] = len(output)
                    # Only retain a short summary; never save snippets or response bodies.
                    event["results"] = [
                        {"title": str(r.get("title", ""))[:180],
                         "url": safe_url(r.get("url")),
                         "engine": str(r.get("engine", ""))[:100]}
                        for r in output[:5] if isinstance(r, dict)
                    ]
                elif kind == "fetch" and isinstance(output, dict):
                    event["crawl_success"] = output.get("success")
                    event["status"] = {"approval_required":"pending", "denied":"denied", "blocked":"blocked"}.get(output.get("status"), "success" if output.get("success") is not False else "error")
                    event["truncated"] = bool(output.get("truncated", False))
                return output
            except Exception as exc:
                event["status"] = "error"
                event["error_type"] = type(exc).__name__
                response = getattr(exc, "response", None)
                code = getattr(response, "status_code", None)
                if isinstance(code, int):
                    event["http_status"] = code
                raise
            finally:
                event["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
                try:
                    append_history(event)
                except (OSError, TypeError, ValueError) as log_exc:
                    # History failures should not prevent normal search functionality.
                    logging.warning("Local-search history logging failed: %s", type(log_exc).__name__)

        return wrapped

    return decorate
