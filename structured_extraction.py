"""Bounded, opt-in structured Crawl4AI page data; no new network or approvals.

Links are discovered, not visited or authorized. Actual fetches must still pass
the bridge's full DNS, operator-approval and guarded-crawler checks.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from domain_policy import normalize_domain

MAX_LINKS = 24
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


def _select_links(raw: dict, base: str) -> list[dict]:
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

    if len(candidates) <= MAX_LINKS:
        return candidates
    indexed = list(enumerate(candidates))
    ranked = sorted(indexed, key=lambda pair: (-_link_score(pair[1]), pair[0]))
    external = [pair for pair in ranked if pair[1]["kind"] == "external"][:2]
    reserved = {index for index, _ in external}
    chosen = external + [
        pair for pair in ranked if pair[0] not in reserved
    ][:MAX_LINKS - len(external)]
    chosen.sort(key=lambda pair: (-_link_score(pair[1]), pair[0]))
    return [row for _, row in chosen]


def extract(page: dict, base: str, *, include_links: bool, include_tables: bool) -> dict:
    """Return only requested bounded fields from a successfully crawled page.

    Crawl4AI v0.9.5: links={internal:[{href,text,title}],external:[...]},
    tables=[{headers:[...],rows:[[...]],caption,summary}].
    """
    output = {}
    remaining = MAX_OUTPUT_BYTES
    if include_links:
        links = []
        raw = page.get("links")
        if isinstance(raw, dict):
            for item in _select_links(raw, base):
                size = _size(item)
                if size <= remaining:
                    links.append(item)
                    remaining -= size
        output["links"] = links
    if include_tables:
        tables = []
        raw = page.get("tables")
        if isinstance(raw, list):
            for row in raw[:10]:
                if len(tables) >= MAX_TABLES or remaining <= 0:
                    break
                if not isinstance(row, dict):
                    continue
                headers = row.get("headers")
                headers = [_text(x, 200) for x in headers[:MAX_COLUMNS]] if isinstance(headers, list) else []
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
        output["tables"] = tables
    return output
