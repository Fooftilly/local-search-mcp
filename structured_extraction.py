"""Bounded, opt-in structured Crawl4AI page data; no new network or approvals.

Links are discovered, not visited or authorized. Actual fetches must still pass
the bridge's full DNS, operator-approval and guarded-crawler checks.
"""
from __future__ import annotations

import json
import re
import sys
from urllib.parse import urljoin, urlsplit, urlunsplit

from domain_policy import normalize_domain

MAX_LINKS = 24
MAX_ALTERNATIVES = 3
MAX_TABLES = 4
MAX_ROWS = 10
MAX_COLUMNS = 8
MAX_OUTPUT_BYTES = 20_000


def _text(value: object, limit: int) -> str:
    if type(value) in (int, float, bool):
        value = str(value)
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value[: limit * 4]).strip()
    return cleaned[:limit].encode("utf-8", "replace").decode("utf-8")


def _url(value: object, base: str) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return None
    if (value != value.strip()
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.IGNORECASE)):
        return None
    try:
        value.encode("utf-8", "strict")
        # urljoin accepts relative links, but schemes other than http(s)
        # remain forbidden even after resolution.
        # An explicit empty query delimiter is origin-visible. urllib's
        # urljoin("?") wrongly inherits the base query, and urlunsplit drops
        # the trailing '?' even for absolute hrefs. Retain it intentionally.
        raw_no_fragment = value.split("#", 1)[0]
        # A single trailing '?' marks an empty query. If a query already
        # contains '?', the trailing character is part of its value.
        empty_query = raw_no_fragment.endswith("?") and raw_no_fragment.count("?") == 1
        if raw_no_fragment == "?":
            origin = urlsplit(base)
            absolute = urlunsplit((
                origin.scheme, origin.netloc, origin.path or "/", "", ""))
        else:
            absolute = urljoin(base, value)
        parsed = urlsplit(absolute)
        if (parsed.scheme not in ("http", "https")
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 80, 443)):
            return None
        host = normalize_domain(parsed.hostname or "")
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        url = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
        if empty_query:
            url += "?"
        if len(url.encode("utf-8")) > 2048:
            return None
        return url
    except (ValueError, TypeError, UnicodeError):
        return None


def _size(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


_NAV_LABELS = frozenset({
    "home", "about", "about us", "contact", "contact us", "menu", "login",
    "log in", "sign in", "register", "privacy", "privacy policy", "terms",
    "terms of use", "cookie policy", "search", "sitemap", "skip to content",
})
_NAV_SEGMENTS = frozenset({
    "login", "signin", "register", "contact", "about", "privacy",
    "terms", "cookies", "sitemap", "account",
})
_CONTENT_SEGMENTS = frozenset({
    "article", "articles", "news", "research", "report", "reports",
    "publication", "publications", "documents", "documentation",
    "docs", "notice", "notices", "guides", "manual", "manuals",
})


def _link_score(item: dict) -> int:
    """A conservative deterministic link-selection heuristic, not relevance."""
    segments = [segment.casefold() for segment in
                urlsplit(item["url"]).path.split("/") if segment]
    label = (item["text"] or item["title"]).casefold().strip()
    if label in _NAV_LABELS or any(s in _NAV_SEGMENTS for s in segments):
        return 0
    score = 1
    if any(s in _CONTENT_SEGMENTS for s in segments):
        score += 2
    if len(label) >= 14:
        score += 1
    if len(segments) >= 2 and len(label) >= 5:
        score += 1
    return score


def _select_links(raw: dict, base: str, *, budget: int = MAX_OUTPUT_BYTES) -> tuple[list[dict], int]:
    """Deduplicate bounded crawl links before selecting a useful 24-item mix.

    Preserve original order for <=24 unique URLs. On overflow, sort by an
    explainable content-link heuristic, preserve source-order ties and reserve
    at most two external candidates. A listing is never an approval or fetch.
    """
    candidates: list[dict] = []
    seen: dict[str, int] = {}
    for kind in ("internal", "external"):
        entries = raw.get(kind)
        if not isinstance(entries, list):
            continue
        for row in entries[:100]:
            if not isinstance(row, dict):
                continue
            target = _url(row.get("href"), base)
            if target is None:
                continue
            item = {"url": target, "kind": kind,
                    "text": _text(row.get("text"), 160),
                    "title": _text(row.get("title"), 160)}
            previous = seen.get(target)
            if previous is not None:
                # The same URL may appear in a generic header and later as a
                # descriptive article link. Prefer bounded informative labels,
                # but never change the first-seen kind or URL provenance.
                original = candidates[previous]
                for key in ("text", "title"):
                    if len(item[key]) > len(original[key]):
                        original[key] = item[key]
                continue
            seen[target] = len(candidates)
            candidates.append(item)

    indexed = list(enumerate(candidates))
    overflowing = len(candidates) > MAX_LINKS
    if overflowing:
        ranked = sorted(indexed, key=lambda pair: (-_link_score(pair[1]), pair[0]))
        external = [pair for pair in ranked if pair[1]["kind"] == "external"][:2]
        reserved = {index for index, _ in external}
        # Reserve a chance for diverse external links, then consider *all*
        # remaining bounded candidates. A too-large selected link must not
        # hide later short URLs that would fit in the shared response budget.
        ordered = external + [pair for pair in ranked if pair[0] not in reserved]
    else:
        ordered = indexed
    chosen = []
    budget = min(budget, MAX_OUTPUT_BYTES)
    for index, item in ordered:
        if len(chosen) >= MAX_LINKS:
            break
        size = _size(item)
        if size <= budget:
            chosen.append((index, item))
            budget -= size
    if overflowing:
        # Restore ranked/source order after the external reservation phase.
        chosen.sort(key=lambda pair: (-_link_score(pair[1]), pair[0]))
    return [row for _, row in chosen], len(candidates)



# The RFC Editor info page exposes plain text, HTML and PDF under one
# explicit "View as:" group in Crawl4AI Markdown. Selected Crawl4AI links
# can omit the group entirely under their 24-link cap, so inspect only
# this narrowly evidenced source-advertised pattern; never guess URLs.
_ALT_MARKER = re.compile(r"(?im)^[ \t]{0,3}View as:[ \t]*$")
_ALT_LINK = re.compile(r"\[([^\]\n]{1,40})\]\(([^)\s]{1,2048})\)")
_ALT_LABELS = {
    "plain text": "txt", "text": "txt",
    "compact html": "html", "html": "html", "pdf": "pdf",
}
_ALT_FENCE_OPEN = re.compile(r"^ {0,3}(\x60{3,}|~{3,})")
_ALT_FENCE_CLOSE = re.compile(r"^ {0,3}(\x60+|~+)[ \t]*$")


def _without_fenced_examples(markdown: str) -> str:
    """Exclude Markdown code fences, including examples of format links."""
    out = []
    fence_char = ""
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        opened = _ALT_FENCE_OPEN.match(content)
        if fence_char:
            if opened and opened.group(1)[0] == fence_char:
                closed = _ALT_FENCE_CLOSE.match(content)
                if closed and len(closed.group(1)) >= fence_length:
                    fence_char = ""
                    fence_length = 0
            out.append("\n")
        elif opened:
            fence_char = opened.group(1)[0]
            fence_length = len(opened.group(1))
            out.append("\n")
        else:
            out.append(line)
    return "".join(out)


def _advertised_alternatives(markdown: str, base: str, *, budget: int) -> list[dict]:
    """Extract explicitly grouped format links, without fetching a link.

    Each candidate must be an advertised, safely normalized URL with a
    matching filename stem. This is a site *claim* of alternative formats,
    not content-type verification or permission to fetch.
    """
    if not isinstance(markdown, str):
        return []
    markdown = _without_fenced_examples(markdown)
    for match_index, marker in enumerate(_ALT_MARKER.finditer(markdown)):
        if match_index >= 8:
            break
        # A single adjacent group only: never match unrelated later nav links.
        group = markdown[marker.end():marker.end() + 900].split("\n\n", 1)[0]
        candidates: dict[str, tuple[str, str]] = {}
        for link in _ALT_LINK.finditer(group):
            fmt = _ALT_LABELS.get(link.group(1).strip().casefold())
            if fmt is None or fmt in candidates:
                continue
            url = _url(link.group(2), base)
            if url is None:
                continue
            path = urlsplit(url).path
            suffix = "." + fmt
            if not path.lower().endswith(suffix):
                continue
            stem = path[:-len(suffix)]
            if not stem or stem.endswith("/"):
                continue
            candidates[fmt] = (stem, url)
        # Do not infer equivalence from labels alone: the source must expose
        # a PDF and a text/HTML candidate for the same literal URL stem.
        if "pdf" not in candidates:
            continue
        stem = candidates["pdf"][0]
        if not any(fmt in candidates and candidates[fmt][0] == stem
                   for fmt in ("txt", "html")):
            continue
        results: list[dict] = []
        for fmt in ("txt", "html", "pdf"):
            candidate = candidates.get(fmt)
            if candidate is None or candidate[0] != stem:
                continue
            item = {"format": fmt, "url": candidate[1],
                    "evidence": "source_labeled_view_as"}
            if _size(item) <= budget and len(results) < MAX_ALTERNATIVES:
                results.append(item)
                budget -= _size(item)
        return results
    return []

# Only already-extracted table cells are eligible for bounded continuation.
MAX_RETAINED_TABLES = 16
MAX_RETAINED_ROWS = 250
MAX_RETAINED_COLUMNS = 24
MAX_RETAINED_BYTES = 800_000
MAX_ROW_BYTES = 1_500


def _bounded_cells(values: list) -> tuple[list[str], bool]:
    cells = [_text(value, 200) for value in values[:MAX_RETAINED_COLUMNS]]
    clipped = len(values) > MAX_RETAINED_COLUMNS or any(
        isinstance(value, str) and len(value.strip()) > 200
        for value in values[:MAX_RETAINED_COLUMNS])
    # ensure_ascii JSON can expand non-ASCII and escaped characters.
    while _size({"cells": cells}) > MAX_ROW_BYTES:
        index = max(range(len(cells)), key=lambda i: len(cells[i]))
        cells[index] = cells[index][:len(cells[index]) // 2]
        clipped = True
    return cells, clipped


def _resident_cells_bytes(cells: list[str]) -> int:
    """Conservative Python object allocation, not just serialized text size."""
    return sys.getsizeof(cells) + sum(sys.getsizeof(value) for value in cells) + 64


def prepare_table_continuation(page: dict) -> dict | None:
    """Keep a bounded normalized subset of this crawl, never raw HTML."""
    raw = page.get("tables")
    if not isinstance(raw, list) or not raw:
        return None
    entries: list[dict] = []
    total_bytes = 512
    truncated = len(raw) > MAX_RETAINED_TABLES
    for index, original in enumerate(raw[:MAX_RETAINED_TABLES]):
        if not isinstance(original, dict):
            continue
        original_headers = original.get("headers")
        original_rows = original.get("rows")
        headers, header_clipped = (_bounded_cells(original_headers)
                                  if isinstance(original_headers, list) else ([], False))
        source_rows = original_rows if isinstance(original_rows, list) else []
        if not headers and not source_rows:
            continue
        fixed = {
            "table_index": index,
            "headers": headers,
            "caption": _text(original.get("caption"), 200),
            "summary": _text(original.get("summary"), 240),
            "source_rows_observed": len(source_rows),
        }
        fixed_size = max(_size(fixed) + 150,
                         sys.getsizeof(fixed) + _resident_cells_bytes(headers)
                         + sys.getsizeof(fixed["caption"])
                         + sys.getsizeof(fixed["summary"]) + 256)
        if total_bytes + fixed_size > MAX_RETAINED_BYTES:
            truncated = True
            break
        total_bytes += fixed_size
        rows: list[list[str]] = []
        cells_truncated = header_clipped
        columns_truncated = (isinstance(original_headers, list)
                             and len(original_headers) > MAX_RETAINED_COLUMNS)
        budget_exhausted = False
        for record in source_rows[:MAX_RETAINED_ROWS]:
            if not isinstance(record, list):
                # Match preview semantics: malformed entries are not rows.
                continue
            columns_truncated = columns_truncated or len(record) > MAX_RETAINED_COLUMNS
            cells, clipped = _bounded_cells(record)
            cost = max(_size({"cells": cells}), _resident_cells_bytes(cells))
            if total_bytes + cost > MAX_RETAINED_BYTES:
                truncated = True
                budget_exhausted = True
                break
            rows.append(cells)
            cells_truncated = cells_truncated or clipped
            total_bytes += cost
        entries.append({
            **fixed,
            "rows": rows,
            "cells_truncated": cells_truncated,
            "retained_truncated": len(rows) < len(source_rows),
            "columns_truncated": columns_truncated,
        })
        # Malformed records are intentionally skipped, not evidence that
        # retention ran out of memory. Continue to later source tables unless
        # the actual aggregate byte budget stopped the current table.
        if budget_exhausted:
            break
    if not entries:
        return None
    return {
        "entries": entries,
        "size_bytes": total_bytes,
        "source_tables_observed": len(raw),
        "retention_truncated": truncated or len(entries) < len(raw) or any(
            entry["retained_truncated"] or entry["columns_truncated"]
            or entry["cells_truncated"]
            for entry in entries),
    }


def extract(page: dict, base: str, *, include_links: bool, include_tables: bool,
            include_alternatives: bool = False, markdown: str = "") -> dict:
    """Return only requested bounded fields from a successfully crawled page.

    Crawl4AI v0.9.5: links={internal:[{href,text,title}],external:[...]},
    tables=[{headers:[...],rows:[[...]],caption,summary}].
    """
    output = {}
    # Reserve a fixed bounded allowance for structural metadata. Counts are
    # observations of the Crawl4AI response, not exhaustive website totals.
    remaining = MAX_OUTPUT_BYTES - 1200
    info = {}
    if include_alternatives:
        alternatives = _advertised_alternatives(markdown, base, budget=remaining)
        output["alternatives"] = alternatives
        remaining -= sum(_size(item) for item in alternatives)
        info["alternatives"] = {
            "returned": len(alternatives),
            "evidence": "source_labeled_view_as" if alternatives else "none_observed",
            "format_verified": False,
        }
    if include_links:
        links = []
        raw = page.get("links")
        observed = 0
        if isinstance(raw, dict):
            selected, observed = _select_links(raw, base, budget=remaining)
            for item in selected:
                size = _size(item)
                if size <= remaining:
                    links.append(item)
                    remaining -= size
        output["links"] = links
        info["links"] = {
            "upstream_field_present": isinstance(raw, dict),
            "unique_safe_candidates_examined": observed,
            "returned": len(links),
            "input_scan_capped": bool(isinstance(raw, dict) and any(
                isinstance(raw.get(kind), list) and len(raw[kind]) > 100
                for kind in ("internal", "external"))),
            "truncated": observed > len(links) or bool(
                isinstance(raw, dict) and any(
                    isinstance(raw.get(kind), list) and len(raw[kind]) > 100
                    for kind in ("internal", "external"))),
        }
    if include_tables:
        tables = []
        limits = []
        returned_indices = []
        raw = page.get("tables")
        if isinstance(raw, list):
            for source_index, row in enumerate(raw[:10]):
                if len(tables) >= MAX_TABLES or remaining <= 0:
                    break
                if not isinstance(row, dict):
                    continue
                original_headers = row.get("headers")
                headers = ([_text(x, 200) for x in original_headers[:MAX_COLUMNS]]
                           if isinstance(original_headers, list) else [])
                source_rows = row.get("rows")
                cells = []
                if isinstance(source_rows, list):
                    for record in source_rows[:MAX_ROWS]:
                        if isinstance(record, list):
                            cells.append([_text(x, 200) for x in record[:MAX_COLUMNS]])
                item = {"headers": headers, "rows": cells,
                        "caption": _text(row.get("caption"), 200),
                        "summary": _text(row.get("summary"), 240)}
                if not headers and not cells:
                    continue
                size = _size(item)
                if size <= remaining:
                    tables.append(item)
                    remaining -= size
                    returned_indices.append(source_index)
                    limits.append({
                        "rows_observed": len(source_rows) if isinstance(source_rows, list) else 0,
                        "rows_truncated": (isinstance(source_rows, list) and (
                            len(source_rows) > MAX_ROWS or
                            len(cells) < min(len(source_rows), MAX_ROWS))),
                        "columns_truncated": (isinstance(original_headers, list) and len(original_headers) > MAX_COLUMNS)
                        or (isinstance(source_rows, list) and any(
                            isinstance(record, list) and len(record) > MAX_COLUMNS
                            for record in source_rows[:MAX_ROWS])),
                    })
        output["tables"] = tables
        info["tables"] = {
            "upstream_field_present": isinstance(raw, list),
            "candidates_observed": len(raw) if isinstance(raw, list) else 0,
            "returned": len(tables),
            "input_scan_capped": isinstance(raw, list) and len(raw) > 10,
            "truncated": isinstance(raw, list) and len(raw) > len(tables),
            "table_limits": limits,
            "returned_source_indices": returned_indices,
        }
    if include_links or include_tables or include_alternatives:
        # This is only present on opt-in structured fetches. Do not imply
        # completeness of the original web page: upstream may omit content.
        output["structured_info"] = {
            "source_completeness": "unknown",
            **info,
        }
    return output
