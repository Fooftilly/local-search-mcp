"""Opt-in, deterministic post-processing of SearXNG search results.

Default web_search ordering and result shape are unchanged. Deduplication uses
conservative URL equivalence only; no scores or cross-engine relevance claims.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalized_url(value: object) -> str | None:
    """Return a conservative HTTP(S) identity or None for unsafe/invalid URLs.

    Preserve path, query, scheme and non-default port. Ignore URL fragments,
    fold DNS host case, omit the default port and normalize an empty path to /.
    Deliberately do not decode percent escapes, reorder queries, remove
    tracking parameters or merge HTTP and HTTPS.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        host = parsed.hostname
        if ":" in host:
            # IPv6 addresses are case-insensitive, but their zone identifiers
            # are not necessarily so. urllib.parse.hostname preserves the
            # original zone spelling even though it lowercases the address.
            address, separator, zone = host.partition("%")
            host = f"[{address.lower()}{separator}{zone}]"
        else:
            host = host.lower()
        port = parsed.port
        default_port = 80 if parsed.scheme == "http" else 443
        if port is not None and port != default_port:
            host += f":{port}"
        canonical = urlunsplit((parsed.scheme.lower(), host, parsed.path or "/",
                                parsed.query, ""))
        # An empty '?' is origin-visible, unlike a fragment, and should not
        # silently match a URL without its query delimiter.
        if not parsed.query and value.split("#", 1)[0].endswith("?"):
            canonical += "?"
        return canonical
    except (ValueError, TypeError, UnicodeError):
        return None


def _engines(row: dict) -> list[str]:
    """Read all valid upstream engine names without an arbitrary count cap."""
    names = []
    seen = set()
    raw = row.get("engines")
    possible = [row.get("engine")]
    if isinstance(raw, list):
        possible.extend(raw)
    for name in possible:
        if (isinstance(name, str) and name and len(name) <= 100
                and name not in seen):
            names.append(name)
            seen.add(name)
    return names


def enrich_search_results(rows: list[dict], max_results: int, *,
                          deduplicate: bool = False,
                          ranking: str = "searxng") -> list[dict]:
    """Deduplicate before limiting results, retaining first-seen provenance.

    Optional per-item keys are source_engines, duplicate_count, source_matches
    and source_count. The 'source_count' ranking counts distinct source engines,
    breaking ties by original SearXNG order (stable Python sort).
    """
    if not deduplicate and ranking == "searxng":
        return [
            {"title": str(r.get("title", ""))[:240],
             "url": str(r.get("url", "")),
             "snippet": str(r.get("content", ""))[:1100],
             "engine": str(r.get("engine", ""))}
            for r in rows[:max_results]
        ]

    grouped: list[dict] = []
    by_url: dict[str, int] = {}
    for row in rows:
        normalized = normalized_url(row.get("url"))
        if normalized is not None and normalized in by_url:
            entry = grouped[by_url[normalized]]
        else:
            entry = {
                "title": str(row.get("title", ""))[:240],
                "url": str(row.get("url", "")),
                "snippet": str(row.get("content", ""))[:1100],
                "engine": str(row.get("engine", "")),
                "source_engines": [],
                "source_count": 0,
                "duplicate_count": 0,
                "source_matches": [],
            }
            if normalized is not None:
                by_url[normalized] = len(grouped)
            grouped.append(entry)
        entry["duplicate_count"] += 1
        for name in _engines(row):
            if name not in entry["source_engines"]:
                entry["source_engines"].append(name)
        entry["source_count"] = len(entry["source_engines"])
        # Retain an inspectable, bounded summary of merged result variants;
        # the main title/snippet remain the first matching SearXNG result.
        if len(entry["source_matches"]) < 8:
            entry["source_matches"].append({
                "engine": str(row.get("engine", ""))[:100],
                "title": str(row.get("title", ""))[:240],
                "snippet": str(row.get("content", ""))[:1100],
            })
    if ranking == "source_count":
        grouped.sort(key=lambda r: -r["source_count"])
    return grouped[:max_results]
