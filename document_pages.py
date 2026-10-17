"""Bounded, short-lived in-memory continuation pages for approved web crawls.

Opaque handles are bearer capabilities, scoped to this running MCP process.
Never store local files, credentials, or unapproved URLs. A restart, TTL
expiry, or memory-pressure eviction invalidates the handle explicitly.
"""
from __future__ import annotations

from collections import OrderedDict
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

PAGE_CHARS = 24_000
MAX_DOCUMENT_CHARS = 1_000_000
MAX_TOTAL_CHARS = 3_000_000
MAX_DOCUMENTS = 8
TTL_SECONDS = 900

# token -> (expires_at, approved_final_url, markdown, public_metadata)
_CACHE: OrderedDict[str, tuple[float, str, str, dict]] = OrderedDict()
_LOCK = threading.Lock()


def _purge_expired(now: float) -> None:
    """Called under _LOCK."""
    for key, item in list(_CACHE.items()):
        if item[0] <= now:
            del _CACHE[key]


def save(url: str, markdown: str, metadata: dict) -> tuple[str, int]:
    """Cache a bounded document and return an unpredictable read capability.

    Small documents need no capability; caller invokes this only for content
    longer than one page. No bytes are written to disk.
    """
    if not isinstance(markdown, str) or not PAGE_CHARS < len(markdown) <= MAX_DOCUMENT_CHARS:
        raise ValueError("Document is not eligible for temporary pagination")
    try:
        target = urlsplit(url) if isinstance(url, str) else None
        if (target is None or target.scheme not in ("http", "https")
                or not target.hostname or target.username is not None
                or target.password is not None):
            raise ValueError("Only approved public web URLs may be saved")
    except ValueError as exc:
        raise ValueError("Only approved public web URLs may be saved") from exc
    now = time.monotonic()
    with _LOCK:
        _purge_expired(now)
        while _CACHE and (
            len(_CACHE) >= MAX_DOCUMENTS
            or sum(len(doc[2]) for doc in _CACHE.values()) + len(markdown) > MAX_TOTAL_CHARS
        ):
            _CACHE.popitem(last=False)
        token = secrets.token_urlsafe(24)
        while token in _CACHE:
            token = secrets.token_urlsafe(24)
        _CACHE[token] = (now + TTL_SECONDS, url, markdown, dict(metadata))
    return token, (len(markdown) + PAGE_CHARS - 1) // PAGE_CHARS


def read(token: str, page: int) -> dict:
    """Read only a cached approved page; never fetch URLs or open files."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Invalid document handle")
    if isinstance(page, bool) or not isinstance(page, int) or page < 2:
        raise ValueError("Page must be an integer starting at 2")
    with _LOCK:
        _purge_expired(time.monotonic())
        item = _CACHE.get(token)
        if item is None:
            return {"success": False, "status": "expired_or_unknown",
                    "reason": "Temporary document not found or expired; fetch the approved URL again."}
        expires_at, url, markdown, metadata = item
        count = (len(markdown) + PAGE_CHARS - 1) // PAGE_CHARS
        if page > count:
            raise ValueError(f"Page must be between 2 and {count}")
        start = (page - 1) * PAGE_CHARS
        chunk = markdown[start:start + PAGE_CHARS]
        result = {"url": url, "success": True, "markdown": chunk,
                  "truncated": page < count, "document_id": token,
                  "page": page, "page_count": count,
                  "next_page": page + 1 if page < count else None}
        if metadata:
            result["metadata"] = dict(metadata)
        return result
