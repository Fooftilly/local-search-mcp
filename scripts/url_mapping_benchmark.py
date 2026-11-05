"""Offline URL-mapping comparison using already-authorized MCP JSON captures.

No network requests, local approval reads/writes, or services are started.
The output contains aggregate, heuristic counts, never input URLs or handles.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

MAX_FILES = 45
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 1_500_000
MAX_SITEMAP_CHARS = 1_000_000
URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)


def _hostname(url: object) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("Invalid source URL")
    try:
        p = urlsplit(url)
        if (p.scheme not in ("http", "https") or not p.hostname
                or p.username is not None or p.password is not None
                or p.port not in (None, 80, 443)):
            raise ValueError("Invalid source URL")
        return p.hostname.lower()
    except ValueError as exc:
        raise ValueError("Invalid source URL") from exc


def _candidate(url: object, host: str) -> str | None:
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        p = urlsplit(url.rstrip(".,;)"))
        if (p.scheme not in ("http", "https") or p.hostname != host
                or p.username is not None or p.password is not None
                or p.port not in (None, 80, 443)):
            return None
        return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))
    except ValueError:
        return None


def analyze(home: dict, sitemap_pages: list[dict],
            link_pages: list[dict] | None = None) -> dict:
    """Summarize a snapshot; flattened Markdown is NOT authenticated XML locs."""
    if not isinstance(home, dict) or home.get("success") is not True:
        raise ValueError("Homepage response must be successful")
    host = _hostname(home.get("url"))
    if not sitemap_pages or len(sitemap_pages) > 42:
        raise ValueError("Invalid number of sitemap pages")
    if any(not isinstance(p, dict) for p in sitemap_pages):
        raise ValueError("Malformed sitemap page")
    source = sitemap_pages[0].get("url")
    if _hostname(source) != host or not urlsplit(source).path:
        raise ValueError("Sitemap origin must match homepage host")
    count = sitemap_pages[0].get("page_count", 1)
    if type(count) is not int or not 1 <= count <= 42 or len(sitemap_pages) > count:
        raise ValueError("Invalid sitemap page count")
    document_id = sitemap_pages[0].get("document_id")
    if count > 1 and (not isinstance(document_id, str) or not document_id):
        raise ValueError("Missing sitemap continuation handle")
    chunks = []
    for n, p in enumerate(sitemap_pages, 1):
        if (p.get("success") is not True or p.get("url") != source
                or type(p.get("page", 1)) is not int or p.get("page", 1) != n
                or p.get("page_count", 1) != count
                or (n > 1 and p.get("document_id") != document_id)
                or not isinstance(p.get("markdown"), str)):
            raise ValueError("Sitemap pages must be sequential with matching provenance")
        chunks.append(p["markdown"])
    text = "".join(chunks)
    if len(text) > MAX_SITEMAP_CHARS:
        raise ValueError("Sitemap exceeds the bounded extracted-text limit")
    all_pages = len(chunks) == count
    # Markdown pages divide strings at character offsets, even inside URLs.
    # Avoid treating a final incomplete token as a valid location.
    if text and (not all_pages or not text[-1].isspace()):
        text = text[:text.rfind(" ") + 1] if " " in text else ""
    sitemap_urls = {x for m in URL_RE.finditer(text)
                    if (x := _candidate(m.group(), host)) is not None}

    rows = home.get("links", [])
    if not isinstance(rows, list):
        raise ValueError("Malformed homepage link list")
    rows = list(rows)
    cont = home.get("link_continuation")
    if cont is not None and not isinstance(cont, dict):
        raise ValueError("Invalid link continuation metadata")
    retained = cont.get("retained_links", 0) if cont else 0
    if type(retained) is not int or not 0 <= retained <= 250:
        raise ValueError("Invalid retained link count")
    if link_pages and (not cont or not isinstance(cont.get("link_id"), str)):
        raise ValueError("Unexpected link continuation page")
    expected_offset = 0
    for p in link_pages or []:
        if (not isinstance(p, dict) or p.get("success") is not True
                or p.get("url") != home["url"]
                or p.get("link_id") != cont["link_id"]
                or type(p.get("offset")) is not int
                or p.get("offset") != expected_offset
                or p.get("retained_links") != retained):
            raise ValueError("Link continuation provenance/offset mismatch")
        extra = p.get("links")
        if not isinstance(extra, list) or not 1 <= len(extra) <= 10:
            raise ValueError("Invalid link continuation entries")
        rows.extend(extra)
        expected_offset += len(extra)
    if len(rows) > 250 or expected_offset > retained:
        raise ValueError("Homepage link list exceeds retention limit")
    home_urls = {x for r in rows if isinstance(r, dict)
                 if (x := _candidate(r.get("url"), host)) is not None}
    info = home.get("structured_info")
    info = info if isinstance(info, dict) else {}
    links_info = info.get("links")
    links_info = links_info if isinstance(links_info, dict) else {}
    links_missing = bool(cont and expected_offset < retained)
    return {
        "homepage": {
            "observed_unique_in_scope_links": len(home_urls),
            "reported_unique_safe_candidates_examined": links_info.get(
                "unique_safe_candidates_examined"),
            "initial_links_truncated": bool(links_info.get("truncated")),
            "link_continuation_pages_supplied": len(link_pages or []),
            "link_continuation_incomplete": links_missing,
            "source_completeness": "unknown",
        },
        "sitemap": {
            "reported_pages": count,
            "pages_supplied": len(sitemap_pages),
            "all_extracted_pages_supplied": all_pages,
            "flattened_markdown_characters_supplied": sum(map(len, chunks)),
            "heuristic_unique_same_host_url_candidates": len(sitemap_urls),
            "heuristic_candidates_not_in_observed_homepage_links": len(
                sitemap_urls - home_urls),
            "xml_structure_verified": False,
            "source_completeness": "unknown",
        },
        "comparison_validity": "exploratory_only_not_xml_or_complete_site_inventory",
    }


def _read(files: list[Path]) -> list[dict]:
    if len(files) > MAX_FILES:
        raise ValueError("Too many input files")
    total = 0
    results = []
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Input must be a regular file")
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("Input exceeds benchmark resource caps")
        # Bound actual read too, even if input changes between stat and open.
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Input exceeds file limit")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        results.append(value)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze already-captured guarded MCP JSON offline; no URLs fetched")
    parser.add_argument("--homepage", type=Path, required=True)
    parser.add_argument("--sitemap-page", type=Path, action="append", required=True,
                        help="JSON from web_fetch then each web_fetch_page, in order")
    parser.add_argument("--link-page", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        data = _read([args.homepage, *args.sitemap_page, *args.link_page])
        boundary = 1 + len(args.sitemap_page)
        print(json.dumps(analyze(data[0], data[1:boundary], data[boundary:]),
                         indent=2, ensure_ascii=False))
        return 0
    except (OSError, UnicodeError, ValueError, TypeError):
        # Never echo contents, private file paths, bearer IDs, or URL details.
        print(json.dumps({"success": False, "status": "invalid_benchmark_input"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
