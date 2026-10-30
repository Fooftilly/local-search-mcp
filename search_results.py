"""Opt-in, deterministic post-processing of SearXNG search results.

Default web_search ordering and result shape are unchanged. Deduplication uses
conservative URL equivalence only; no scores or cross-engine relevance claims.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from urllib.parse import urlsplit, urlunsplit

from domain_policy import normalize_domain


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


def _text(value: object, limit: int) -> str:
    """Keep untrusted search text bounded and encodable by MCP transports."""
    return str(value)[:limit].encode("utf-8", "replace").decode("utf-8")


def _engine_text(value: object) -> str:
    """Use one bounded, readable representation for all source names."""
    return str(value).replace("\\", "\\\\").encode(
        "utf-8", "backslashreplace").decode("utf-8")[:100]


def _engines(row: dict) -> list[str]:
    """Read all valid upstream engine names without an arbitrary count cap."""
    names = []
    seen = set()
    raw = row.get("engines")
    possible = [row.get("engine")]
    if isinstance(raw, list):
        possible.extend(raw)
    for raw_name in possible:
        if isinstance(raw_name, str) and raw_name and len(raw_name) <= 100:
            # Only count a source when its escaped name remains within
            # the established 100-character bound. An overlong malformed
            # source cannot provide reliable, distinguishable attribution.
            name = raw_name.replace("\\", "\\\\").encode(
                "utf-8", "backslashreplace").decode("utf-8")
            if len(name) <= 100 and name not in seen:
                names.append(name)
                seen.add(name)
    return names


def _published_date(row: dict) -> str | None:
    """Extract the source-reported calendar date, never infer a timezone.

    The real SearXNG JSON output uses publishedDate=None or ISO-like text,
    including timestamps without an offset. Do not promote deprecated
    pubdate, scraped snippet text, or the crawl time as publication evidence.
    """
    value = row.get("publishedDate")
    if (not isinstance(value, str) or len(value) > 40 or
            re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?)?", value
            ) is None):
        return None
    try:
        parsed = (datetime.fromisoformat(value) if "T" in value
                  else date.fromisoformat(value))
        # Preserve the source's calendar day. Turning a naive timestamp
        # into UTC would invent a timezone; date-only output avoids that.
        return (parsed.date() if isinstance(parsed, datetime) else parsed).isoformat()
    except ValueError:
        return None


def _public_image_link(value: object) -> str | None:
    """Validate *metadata only*, without DNS, downloading or granting access.

    Image URLs are untrusted search-provider strings, not approved fetch URLs.
    Only unambiguous ASCII HTTPS links to public-looking DNS hostnames are
    exposed. This deliberately omits IDNs and unusual URL encodings.
    """
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or not value.startswith("https://")
            or any(not 0x21 <= ord(char) <= 0x7e for char in value)
            or "\\" in value
            or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|5c|7f)", value,
                         flags=re.IGNORECASE)):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme != "https" or not host
                or "@" in parts.netloc or parts.port not in (None, 443)
                or parts.netloc.endswith(":") or parts.netloc.endswith(".")
                or any(label.startswith("xn--") for label in host.split("."))):
            return None
        # Uses only the bundled public suffix rules. No DNS or policy write.
        if normalize_domain(host) != host:
            return None
    except (UnicodeError, ValueError, RuntimeError):
        return None
    return value


def _image_dimensions(value: object) -> tuple[int, int] | None:
    """Only retain measured pixel dimensions from an exact WxH string."""
    if not isinstance(value, str) or len(value) > 24:
        return None
    match = re.fullmatch(r"([1-9][0-9]{0,4})[xX]([1-9][0-9]{0,4})", value)
    if match is None:
        return None
    width, height = map(int, match.groups())
    return (width, height) if width <= 20000 and height <= 20000 else None


def _with_optional_metadata(entry: dict, row: dict, publication: bool,
                            images: bool) -> dict:
    if publication:
        published = _published_date(row)
        if published is not None:
            entry["published_date"] = published
            entry["date_source"] = "searxng.publishedDate"
    # Do not infer image attribution merely from the selected search filter.
    # A normal web hit with a provider-supplied img_src is not an image result.
    if images and row.get("category") == "images":
        image = _public_image_link(row.get("img_src"))
        thumbnail = _public_image_link(row.get("thumbnail_src"))
        if image is not None or thumbnail is not None:
            if image is not None:
                entry["image_url"] = image
            if thumbnail is not None:
                entry["thumbnail_url"] = thumbnail
            source = _public_image_link(row.get("url"))
            if source is not None:
                entry["source_page_url"] = source
            dimensions = _image_dimensions(row.get("resolution"))
            if dimensions is not None:
                entry["image_width"] = dimensions[0]
                entry["image_height"] = dimensions[1]
    return entry


def enrich_search_results(rows: list[dict], max_results: int, *,
                          deduplicate: bool = False,
                          ranking: str = "searxng",
                          include_published_date: bool = False,
                          include_image_metadata: bool = False) -> list[dict]:
    """Deduplicate before limiting results, retaining first-seen provenance.

    Optional per-item keys are source_engines, duplicate_count, source_matches
    and source_count; independently, include_published_date exposes only valid
    upstream publishedDate as a date with explicit field provenance.
    Image metadata is opt-in, validated and copied only from the primary
    source row; never attach another engine's picture to a grouped URL.
    The 'source_count' ranking counts distinct source engines, breaking ties
    by original SearXNG order (stable Python sort).
    """
    if not deduplicate and ranking == "searxng":
        return [
            _with_optional_metadata({
                "title": _text(r.get("title", ""), 240),
                "url": str(r.get("url", "")),
                "snippet": _text(r.get("content", ""), 1100),
                "engine": _engine_text(r.get("engine", "")),
            }, r, include_published_date, include_image_metadata)
            for r in rows[:max_results]
        ]

    grouped: list[dict] = []
    by_url: dict[str, int] = {}
    for row in rows:
        normalized = normalized_url(row.get("url"))
        if normalized is not None and normalized in by_url:
            entry = grouped[by_url[normalized]]
        else:
            entry = _with_optional_metadata({
                "title": _text(row.get("title", ""), 240),
                "url": str(row.get("url", "")),
                "snippet": _text(row.get("content", ""), 1100),
                "engine": _engine_text(row.get("engine", "")),
                "source_engines": [],
                "source_count": 0,
                "duplicate_count": 0,
                "source_matches": [],
            }, row, include_published_date, include_image_metadata)
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
        if entry["duplicate_count"] > 1 and len(entry["source_matches"]) < 8:
            # The first occurrence is already represented by the primary
            # engine/title/snippet. Only retain actual alternative matches.
            entry["source_matches"].append({
                "engine": _engine_text(row.get("engine", "")),
                "title": _text(row.get("title", ""), 240),
                "snippet": _text(row.get("content", ""), 1100),
            })
    if ranking == "source_count":
        grouped.sort(key=lambda r: -r["source_count"])
    return grouped[:max_results]
