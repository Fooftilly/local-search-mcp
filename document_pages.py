"""Bounded, short-lived in-memory continuation pages for approved web crawls.

Opaque handles are bearer capabilities, scoped to this running MCP process.
Never store local files, credentials, or unapproved URLs. A restart, TTL
expiry, or memory-pressure eviction invalidates the handle explicitly.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

PAGE_CHARS = 24_000
MIN_FIRST_PAGE_CHARS = 2_048
MAX_DOCUMENT_CHARS = 1_000_000
MAX_TOTAL_CHARS = 3_000_000
MAX_DOCUMENTS = 8
TTL_SECONDS = 900

# token -> (expires_at, approved_final_url, markdown, public_metadata,
#           source_url, PRIVATE one-use proof or None, first_page_chars,
#           sanitized table payload or None)
_CACHE: OrderedDict[str, tuple] = OrderedDict()
_LOCK = threading.Lock()


def _entry_weight(entry: tuple) -> int:
    """Count Markdown and table rows against one conservative memory budget."""
    tables = entry[7] if len(entry) > 7 else None
    return len(entry[2]) + (tables["size_bytes"] if tables is not None else 0)


def _purge_expired(now: float) -> None:
    """Called under _LOCK."""
    for key, item in list(_CACHE.items()):
        if item[0] <= now:
            del _CACHE[key]


def _page_count(length: int, first_page_chars: int) -> int:
    return 1 + (max(0, length - first_page_chars) + PAGE_CHARS - 1) // PAGE_CHARS


def page_for_offset(offset: int, first_page_chars: int = PAGE_CHARS) -> int:
    """Page containing a zero-based Markdown character offset."""
    return (1 if offset < first_page_chars else
            2 + (offset - first_page_chars) // PAGE_CHARS)


def save(url: str, markdown: str, metadata: dict, *,
         source_url: str | None = None,
         once_proof: tuple[str, str] | None = None,
         first_page_chars: int = PAGE_CHARS) -> tuple[str, int]:
    """Cache a bounded document and return an unpredictable read capability.

    Ordinary short documents need no capability; the caller may also save a
    shorter, opt-in first page for safe continuation. Nothing is persisted.
    """
    if (type(first_page_chars) is not int
            or not MIN_FIRST_PAGE_CHARS <= first_page_chars <= PAGE_CHARS):
        raise ValueError("first_page_chars must be between 2048 and 24000")
    if (not isinstance(markdown, str)
            or not first_page_chars < len(markdown) <= MAX_DOCUMENT_CHARS):
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
            or sum(_entry_weight(doc) for doc in _CACHE.values()) + len(markdown) > MAX_TOTAL_CHARS
        ):
            _CACHE.popitem(last=False)
        token = secrets.token_urlsafe(24)
        while token in _CACHE:
            token = secrets.token_urlsafe(24)
        _CACHE[token] = (now + TTL_SECONDS, url, markdown, dict(metadata),
                         source_url, once_proof, first_page_chars, None)
    return token, _page_count(len(markdown), first_page_chars)


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
        expires_at, url, markdown, metadata, source_url, once_proof, first_page_chars, _tables = item
        count = _page_count(len(markdown), first_page_chars)
        if page > count:
            raise ValueError(f"Page must be between 2 and {count}")
        start = first_page_chars + (page - 2) * PAGE_CHARS
        chunk = markdown[start:start + PAGE_CHARS]
        result = {"url": url, "success": True, "markdown": chunk,
                  "truncated": page < count, "document_id": token,
                  "page": page, "page_count": count,
                  "next_page": page + 1 if page < count else None}
        if metadata:
            result["metadata"] = dict(metadata)
        return result, source_url, once_proof



MAX_TABLE_PAGE_ROWS = 10
MAX_TABLE_PAGE_COLUMNS = 8
MAX_TABLE_PAGE_BYTES = 20_000
MAX_RETAINED_TABLE_COLUMNS = 24


def save_tables(url: str, payload: dict, *, source_url: str,
                once_proof: tuple[str, str] | None = None) -> str:
    """Keep pre-normalized table rows in the existing document-capability cache."""
    size = payload.get("size_bytes") if isinstance(payload, dict) else None
    if (type(size) is not int or not 0 < size <= 800_000
            or not isinstance(payload.get("entries"), list)
            or not payload["entries"]):
        raise ValueError("Invalid bounded table payload")
    try:
        target, source = urlsplit(url), urlsplit(source_url)
        if any(part.scheme not in ("http", "https") or not part.hostname
               or part.username is not None or part.password is not None
               for part in (target, source)):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Only approved public URLs may be saved") from exc
    if once_proof is not None and (
            not isinstance(once_proof, tuple) or len(once_proof) != 2
            or not all(isinstance(v, str) for v in once_proof)
            or not re.fullmatch(r"[a-f0-9]{32}", once_proof[1])):
        raise ValueError("Invalid private authorization context")
    now = time.monotonic()
    with _LOCK:
        _purge_expired(now)
        while _CACHE and (len(_CACHE) >= MAX_DOCUMENTS or
                          sum(_entry_weight(doc) for doc in _CACHE.values())
                          + size > MAX_TOTAL_CHARS):
            _CACHE.popitem(last=False)
        token = secrets.token_urlsafe(24)
        while token in _CACHE:
            token = secrets.token_urlsafe(24)
        _CACHE[token] = (now + TTL_SECONDS, url, "", {}, source_url,
                         once_proof, PAGE_CHARS, payload)
    return token


def table_handle_exists(token: str) -> bool:
    """Check handle liveness without exposing table content or authorization."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        return False
    with _LOCK:
        _purge_expired(time.monotonic())
        item = _CACHE.get(token)
        return item is not None and item[7] is not None


def read_table_with_security_context(
        token: str, table_index: int, row_offset: int = 0,
        max_rows: int = MAX_TABLE_PAGE_ROWS, column_offset: int = 0,
        max_columns: int = MAX_TABLE_PAGE_COLUMNS
) -> tuple[dict, str | None, tuple[str, str] | None]:
    """Read retained rows only; bridge must reauthorize before content release."""
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Invalid table handle")
    if type(table_index) is not int or not 0 <= table_index < 16:
        raise ValueError("table_index must be between 0 and 15")
    if type(row_offset) is not int or not 0 <= row_offset <= 250:
        raise ValueError("row_offset must be between 0 and 250")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_TABLE_PAGE_ROWS:
        raise ValueError("max_rows must be between 1 and 10")
    if type(column_offset) is not int or not 0 <= column_offset <= MAX_RETAINED_TABLE_COLUMNS:
        raise ValueError("column_offset must be between 0 and 24")
    if type(max_columns) is not int or not 1 <= max_columns <= MAX_TABLE_PAGE_COLUMNS:
        raise ValueError("max_columns must be between 1 and 8")
    with _LOCK:
        _purge_expired(time.monotonic())
        item = _CACHE.get(token)
        if item is None or item[7] is None:
            return ({"success": False, "status": "expired_or_unknown",
                     "reason": "Temporary table not found or expired; fetch the approved URL again."},
                    None, None)
        _, url, _, _, source_url, once_proof, _, payload = item
        entry = next((table for table in payload["entries"]
                      if table["table_index"] == table_index), None)
        if entry is None:
            raise ValueError("Unknown table_index for this fetch")
        rows = entry["rows"]
        if row_offset > len(rows):
            raise ValueError("row_offset exceeds retained rows")
        selected = rows[row_offset:row_offset + max_rows]
        next_offset = row_offset + len(selected)
        more = next_offset < len(rows)
        retained_columns = max(len(entry["headers"]),
                               max((len(row) for row in rows), default=0))
        if column_offset > retained_columns:
            raise ValueError("column_offset exceeds retained columns")
        next_column = column_offset + max_columns
        more_columns = next_column < retained_columns
        response = {
            "success": True, "url": url, "table_id": token,
            "table_index": table_index, "row_offset": row_offset,
            "headers": entry["headers"][column_offset:next_column],
            "rows": [row[column_offset:next_column] for row in selected],
            "column_offset": column_offset, "retained_columns": retained_columns,
            "has_more_columns": more_columns,
            "next_column_offset": next_column if more_columns else None,
            "caption": entry["caption"], "summary": entry["summary"],
            "retained_rows": len(rows),
            "source_rows_observed": entry["source_rows_observed"],
            "retained_truncated": entry["retained_truncated"],
            "columns_truncated": entry["columns_truncated"],
            "cells_truncated": entry["cells_truncated"],
            "source_completeness": "unknown",
            "has_more": more, "next_offset": next_offset if more else None,
        }
        # Whole-response budget, including headers/captions/source URL. A
        # smaller row page is valid, but must advance by exactly its row count.
        while (response["rows"] and len(json.dumps(
                response, ensure_ascii=True).encode("utf-8")) > MAX_TABLE_PAGE_BYTES):
            response["rows"].pop()
            next_offset = row_offset + len(response["rows"])
            more = next_offset < len(rows)
            response["has_more"] = more
            response["next_offset"] = next_offset if more else None
        if len(json.dumps(response, ensure_ascii=True).encode("utf-8")) > MAX_TABLE_PAGE_BYTES:
            return ({"success": False, "status": "table_page_too_large",
                     "reason": "The retained table page cannot fit the response limit."},
                    source_url, once_proof)
        if row_offset < len(rows) and not response["rows"]:
            return ({"success": False, "status": "table_page_too_large",
                     "reason": "One row exceeds the response limit."},
                    source_url, once_proof)
        return response, source_url, once_proof


MAX_OUTLINE_HEADINGS = 40
MAX_OUTLINE_HEADING_CHARS = 160
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$")
_SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`+|~+)[ \t]*$")


def _outline_title(raw: str) -> str:
    """Bound and neutralize untrusted Markdown heading text."""
    import unicodedata
    clean = "".join(
        " " if unicodedata.category(c).startswith("C") else c
        for c in raw[:MAX_OUTLINE_HEADING_CHARS * 3])
    clean = re.sub(r"[ \t]+#+[ \t]*$", "", clean).strip()
    return clean[:MAX_OUTLINE_HEADING_CHARS]


def build_outline(markdown: str, *, first_page_chars: int = PAGE_CHARS) -> dict:
    """Summarize headings in already-approved Markdown, never a new crawl.

    Offsets count Python Unicode characters in the normalized extracted text
    and match web_find / web_fetch_page positions. Never stores a second index.
    """
    headings = []
    truncated = False
    fence_char = ""
    fence_length = 0
    previous = ""
    previous_offset = 0
    offset = 0
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        opened = _FENCE_OPEN.match(content)
        if fence_char:
            if opened and opened.group(1)[0] == fence_char:
                closing = _FENCE_CLOSE.match(content)
                if closing and len(closing.group(1)) >= fence_length:
                    fence_char = ""
                    fence_length = 0
            previous = ""
        elif opened:
            fence_char = opened.group(1)[0]
            fence_length = len(opened.group(1))
            previous = ""
        else:
            match = _ATX_HEADING.match(content)
            start = offset
            level = 0
            name = ""
            if match:
                level = len(match.group(1))
                name = _outline_title(match.group(2))
            elif _SETEXT_HEADING.fullmatch(content) and previous:
                marker = content.strip()[0]
                level = 1 if marker == "=" else 2
                name = _outline_title(previous)
                start = previous_offset
            if level and name:
                if len(headings) >= MAX_OUTLINE_HEADINGS:
                    truncated = True
                    break
                headings.append({"level": level, "heading": name,
                                 "offset": start, "page": page_for_offset(start, first_page_chars)})
            # Preserve a bounded prefix of long Setext title candidates,
            # rather than discarding their entire heading. Four-space or
            # tab-indented text is Markdown code, not a Setext title. A tab
            # after up to three leading spaces reaches a code indentation.
            eligible = (bool(content.strip())
                        and not content.lstrip().startswith(
                            ("#", ">", "-", "*", "+"))
                        and not re.match(r"^(?: {4}| {0,3}\t)", content))
            previous = content[:MAX_OUTLINE_HEADING_CHARS * 3] if eligible else ""
            previous_offset = offset
        offset += len(line)
    return {"outline": headings, "outline_truncated": truncated}


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
        if item is None or item[7] is not None:
            return ({"success": False, "status": "expired_or_unknown",
                     "reason": "Temporary document not found or expired; fetch the approved URL again."},
                    None, None)
        expires_at, url, markdown, metadata, source_url, once_proof, first_page_chars, _tables = item
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
            matches.append({"page": page_for_offset(start, first_page_chars),
                            "offset": start, "excerpt": excerpt})
            next_from = start + 1
        has_more = bool(matches and len(matches) >= max_matches
                        and matcher.search(markdown, next_from) is not None)
        count = _page_count(len(markdown), first_page_chars)
        result = {"success": True, "url": url, "document_id": token,
                  "page_count": count, "matches": matches,
                  "match_count": len(matches), "has_more": has_more,
                  "next_offset": next_from if has_more else None}
        return result, source_url, once_proof



MAX_SECTION_CHARS = 8_000
DEFAULT_SECTION_CHARS = 3_072


def section_with_security_context(
        token: str, start_offset: int, max_chars: int = DEFAULT_SECTION_CHARS
) -> tuple[dict, str | None, tuple[str, str] | None]:
    """Read an exact bounded Unicode span of an existing authorized document.

    Return the private source and one-use context to the same bridge release
    gate used for web_fetch_page/web_find. This never fetches or persists text.
    """
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ValueError("Invalid document handle")
    if type(start_offset) is not int or not 0 <= start_offset <= MAX_DOCUMENT_CHARS:
        raise ValueError("start_offset must be from 0 to 1000000")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_SECTION_CHARS:
        raise ValueError("max_chars must be from 1 to 8000")
    with _LOCK:
        _purge_expired(time.monotonic())
        item = _CACHE.get(token)
        if item is None or item[7] is not None:
            return ({"success": False, "status": "expired_or_unknown",
                     "reason": "Temporary document not found or expired; fetch the approved URL again."},
                    None, None)
        expires_at, url, markdown, metadata, source_url, once_proof, first_page_chars, _tables = item
        if start_offset > len(markdown):
            raise ValueError("start_offset exceeds the document length")
        end_offset = min(len(markdown), start_offset + max_chars)
        result = {
            "success": True, "url": url, "document_id": token,
            "text": markdown[start_offset:end_offset],
            "start_offset": start_offset, "end_offset": end_offset,
            "next_offset": end_offset if end_offset < len(markdown) else None,
            "page": page_for_offset(min(start_offset, max(0, len(markdown) - 1)),
                                    first_page_chars),
            "page_count": _page_count(len(markdown), first_page_chars),
            "total_chars": len(markdown),
        }
        return result, source_url, once_proof


def read_with_context(token: str, page: int) -> tuple[dict, str | None]:
    """Compatibility interface: private one-use proof is never returned."""
    result, source_url, _ = read_with_security_context(token, page)
    return result, source_url


def read(token: str, page: int) -> dict:
    """Read only cached approved content; bridge enforces live permission."""
    return read_with_context(token, page)[0]
