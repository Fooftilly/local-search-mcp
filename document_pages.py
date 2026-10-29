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

# token -> (expires_at, approved_final_url, markdown, public_metadata,
#           source_url, PRIVATE one-use proof or None)
_CACHE: OrderedDict[str, tuple[float, str, str, dict, str, tuple[str, str] | None]] = OrderedDict()
_LOCK = threading.Lock()


def _purge_expired(now: float) -> None:
    """Called under _LOCK."""
    for key, item in list(_CACHE.items()):
        if item[0] <= now:
            del _CACHE[key]


def save(url: str, markdown: str, metadata: dict, *,
         source_url: str | None = None,
         once_proof: tuple[str, str] | None = None) -> tuple[str, int]:
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
    # Keep the original approved host for cross-host redirects. Never expose
    # authorization metadata in the public continuation response.
    source_url = url if source_url is None else source_url
    try:
        source = urlsplit(source_url)
        if (source.scheme not in ("http", "https") or not source.hostname
                or source.username is not None or source.password is not None):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Only approved public source URLs may be saved") from exc
    if once_proof is not None and (
            not isinstance(once_proof, tuple) or len(once_proof) != 2
            or not all(isinstance(v, str) for v in once_proof)
            or not re.fullmatch(r"[a-f0-9]{32}", once_proof[1])):
        raise ValueError("Invalid private authorization context")
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
        _CACHE[token] = (now + TTL_SECONDS, url, markdown, dict(metadata),
                         source_url, once_proof)
    return token, (len(markdown) + PAGE_CHARS - 1) // PAGE_CHARS


def read_with_security_context(
        token: str, page: int
) -> tuple[dict, str | None, tuple[str, str] | None]:
    """Read a page and PRIVATE authorization proof, never returned to MCP."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Invalid document handle")
    if isinstance(page, bool) or not isinstance(page, int) or page < 2:
        raise ValueError("Page must be an integer starting at 2")
    with _LOCK:
        _purge_expired(time.monotonic())
        item = _CACHE.get(token)
        if item is None:
            return ({"success": False, "status": "expired_or_unknown",
                     "reason": "Temporary document not found or expired; fetch the approved URL again."}, None, None)
        expires_at, url, markdown, metadata, source_url, once_proof = item
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
        return result, source_url, once_proof


MAX_FIND_QUERY = 160
MAX_FIND_MATCHES = 20
FIND_SNIPPET_BEFORE = 90
FIND_SNIPPET_AFTER = 130


def find_with_security_context(
        token: str, query: str, max_matches: int = 10,
        start_offset: int = 0
) -> tuple[dict, str | None, tuple[str, str] | None]:
    """Literal case-insensitive matches in *existing* temporary document text.

    Return private source/one-use context to bridge for the exact same live
    approval and revocation checks as continuation pages. Never crawl, read
    local files, or retain an independent index.
    """
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Invalid document handle")
    if (not isinstance(query, str) or not 1 <= len(query) <= MAX_FIND_QUERY
            or not query.strip() or not query.isprintable()):
        raise ValueError("Query must be 1–160 printable characters")
    if type(max_matches) is not int or not 1 <= max_matches <= MAX_FIND_MATCHES:
        raise ValueError("max_matches must be between 1 and 20")
    if type(start_offset) is not int or not 0 <= start_offset <= MAX_DOCUMENT_CHARS:
        raise ValueError("start_offset must be a character position from 0 to 1000000")
    with _LOCK:
        _purge_expired(time.monotonic())
        item = _CACHE.get(token)
        if item is None:
            return ({"success": False, "status": "expired_or_unknown",
                     "reason": "Temporary document not found or expired; fetch the approved URL again."},
                    None, None)
        expires_at, url, markdown, metadata, source_url, once_proof = item
        if start_offset > len(markdown):
            raise ValueError("start_offset exceeds the document length")
        matcher = re.compile(re.escape(query), re.IGNORECASE)
        matches = []
        next_from = start_offset
        # Search from match.start()+1 to include overlapping literal hits.
        # At most 1 million cached characters, 20 returned matches.
        while len(matches) < max_matches:
            found = matcher.search(markdown, next_from)
            if found is None:
                break
            start, end = found.span()
            excerpt = markdown[max(0, start - FIND_SNIPPET_BEFORE):
                               min(len(markdown), end + FIND_SNIPPET_AFTER)]
            matches.append({"page": 1 + start // PAGE_CHARS,
                            "offset": start, "excerpt": excerpt})
            next_from = start + 1
        has_more = bool(matches and len(matches) >= max_matches
                        and matcher.search(markdown, next_from) is not None)
        count = (len(markdown) + PAGE_CHARS - 1) // PAGE_CHARS
        result = {"success": True, "url": url, "document_id": token,
                  "page_count": count, "matches": matches,
                  "match_count": len(matches), "has_more": has_more,
                  "next_offset": next_from if has_more else None}
        return result, source_url, once_proof


def read_with_context(token: str, page: int) -> tuple[dict, str | None]:
    """Compatibility interface: private one-use proof is never returned."""
    result, source_url, _ = read_with_security_context(token, page)
    return result, source_url


def read(token: str, page: int) -> dict:
    """Read only cached approved content; bridge enforces live permission."""
    return read_with_context(token, page)[0]
