"""Pure, bounded sitemap XML classification for an already-authorized source.

This module NEVER downloads, follows, approves, caches, or publishes URLs.
The caller must supply original, schema-preserving XML bytes obtained via
an independently guarded fetch; Crawl4AI Markdown is NOT suitable input.
Do not expose results to MCP callers without checking the live source/final
permission epoch and one-use authorization proof at content release.
"""
from __future__ import annotations

from io import BytesIO
import re
from urllib.parse import urlsplit, urlunsplit
from xml.etree.ElementTree import ParseError

from defusedxml.ElementTree import iterparse
from defusedxml.common import DefusedXmlException

# The standard sitemap XML namespace is a URI identifier, not an HTTP request.
SITEMAP_NS = urlunsplit(("http", "www.sitemaps.org", "/schemas/sitemap/0.9", "", ""))
MAX_DOCUMENT_BYTES = 1024 * 1024  # Two documents may use 2 MiB total.
MAX_RETURNED_URLS = 100
MAX_XML_ELEMENTS = 25_000
MAX_XML_DEPTH = 12
MAX_SITEMAP_ENTRIES = 6_000
MAX_URL_LENGTH = 2048
_BAD_ESCAPED_PATH = re.compile(r"%(?:2e|2f|5c|25|0[0-9a-f]|1[0-9a-f]|7f|23|3f)", re.IGNORECASE)
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")



class SitemapXMLRejected(ValueError):
    """XML input did not satisfy the bounded, fail-closed parser contract."""


def _tag_name(tag: object) -> str | None:
    """Accept only standard sitemap XML namespace or no namespace."""
    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        namespace, sep, name = tag[1:].partition("}")
        return name if sep and namespace == SITEMAP_NS else None
    return tag


def _scope_host(host: str) -> str:
    if not isinstance(host, str) or not 1 <= len(host) <= 253:
        raise ValueError("Invalid caller-provided scope host")
    if (host != host.lower() or host.endswith(".") or not host.isascii()
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or any(not label or len(label) > 63 or label.startswith("-")
                   or label.endswith("-") for label in host.split("."))):
        raise ValueError("Invalid caller-provided scope host")
    return host


def _path_safe(path: str) -> bool:
    if (not path.startswith("/") or "\\" in path or "//" in path or "?" in path or "#" in path
            or _BAD_ESCAPED_PATH.search(path) or _BAD_PERCENT.search(path)):
        return False
    return not any(part in (".", "..") for part in path.split("/"))


def _source_url(url: str, host: str) -> None:
    if _candidate(url, host, "/") is None:
        raise ValueError("Invalid or cross-host sitemap source URL")


def _candidate(value: str, host: str, path_prefix: str) -> str | None:
    """Filter discovered strings, not permissions; never perform DNS or fetch."""
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_URL_LENGTH
            or value != value.strip() or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or url.username is not None
                or url.password is not None or url.port not in (None, 80, 443)
                or url.hostname != host or url.fragment):
            return None
    except ValueError:
        return None
    path = url.path or "/"
    if not _path_safe(path):
        return None
    prefix = path_prefix.rstrip("/") or "/"
    if prefix != "/" and path != prefix and not path.startswith(prefix + "/"):
        return None
    return urlunsplit((url.scheme, url.netloc.lower(), path, url.query, ""))


def parse_sitemap_xml(data: bytes, *, source_url: str,
                      scope_host: str, path_prefix: str = "/",
                      max_urls: int = MAX_RETURNED_URLS) -> dict:
    """Classify a single XML sitemap/index without I/O or authorization side effects.

    Returned index URLs are CHILD SITEMAP REFERENCES, never page URLs.
    No child is fetched automatically. The caller must separately guard any
    child-sitemap request and independently verify effective permissions.

    Raises SitemapXMLRejected on malformed/DTD/entity/oversized/unsupported
    XML. Returns only conservative, same-host/path-matching candidates.
    """
    host = _scope_host(scope_host)
    if (not isinstance(path_prefix, str) or not _path_safe(path_prefix)
            or len(path_prefix) > 2048):
        raise ValueError("Invalid caller-provided scope path")
    if type(max_urls) is not int or not 1 <= max_urls <= MAX_RETURNED_URLS:
        raise ValueError("Invalid output cap")
    _source_url(source_url, host)
    if not isinstance(data, bytes) or not data or len(data) > MAX_DOCUMENT_BYTES:
        raise SitemapXMLRejected("XML bytes unavailable or exceed per-document cap")
    if data.startswith((b"\x1f\x8b", b"PK\x03\x04")):
        raise SitemapXMLRejected("Compressed XML requires an independently guarded decoder")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise SitemapXMLRejected("Only UTF-8 XML is supported")
    # Read at most 256 bytes of an XML declaration with linear byte operations.
    # This avoids regex backtracking on adversarial text and rejects mismatched encodings.
    head = data[:256].removeprefix(b"\xef\xbb\xbf")
    if head.startswith(b"<?xml"):
        end = head.find(b"?>")
        if end < 0:
            raise SitemapXMLRejected("Malformed XML declaration")
        header = head[:end].lower()
        _, found, tail = header.partition(b"encoding")
        if found:
            tail = tail.lstrip()
            if not tail.startswith(b"="):
                raise SitemapXMLRejected("Malformed XML encoding declaration")
            tail = tail[1:].lstrip()
            quote = tail[:1]
            if quote not in (b"'", b'"'):
                raise SitemapXMLRejected("Malformed XML encoding declaration")
            encoding, closing, _ = tail[1:].partition(quote)
            if not closing or encoding not in (b"utf-8", b"utf8"):
                raise SitemapXMLRejected("Only UTF-8 XML is supported")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise SitemapXMLRejected("Only valid UTF-8 XML is supported") from exc

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    scanned = 0
    elements = 0
    rejected = 0
    duplicates = 0
    root_kind: str | None = None
    root_ns_prefix = ""
    record_kind: str | None = None
    location: str | None = None
    saw_location = False
    depth = 0
    try:
        # Stream in-memory bounded bytes with defusedxml, forbidding XML
        # DTDs, internal entities and external references explicitly.
        for event, element in iterparse(BytesIO(data), events=("start", "end"),
                                        forbid_dtd=True, forbid_entities=True,
                                        forbid_external=True):
            tag = _tag_name(element.tag)
            if event == "start":
                elements += 1
                depth += 1
                if depth > MAX_XML_DEPTH or elements > MAX_XML_ELEMENTS:
                    raise SitemapXMLRejected("XML structural limits exceeded")
                if depth == 1:
                    root_kind = tag
                    root_ns_prefix = ("{" + SITEMAP_NS + "}"
                                      if element.tag.startswith("{") else "")
                    if root_kind not in ("urlset", "sitemapindex"):
                        raise SitemapXMLRejected("Not a sitemap XML root")
                    record_kind = "url" if root_kind == "urlset" else "sitemap"
                elif depth == 2:
                    if element.tag != root_ns_prefix + record_kind:
                        raise SitemapXMLRejected("Unsupported sitemap root child")
                    scanned += 1
                    if scanned > MAX_SITEMAP_ENTRIES:
                        raise SitemapXMLRejected("Sitemap entry scan limit exceeded")
                    location = None
                    saw_location = False
            else:
                if depth == 3 and element.tag == root_ns_prefix + "loc":
                    # Only one direct child location; ignore any nested
                    # extension locs or non-text location bodies.
                    if saw_location or len(element) != 0 or element.text is None:
                        location = None
                        saw_location = True
                    else:
                        location = element.text
                        saw_location = True
                elif depth == 2:
                    candidate = (_candidate(location, host, path_prefix)
                                 if saw_location and location is not None else None)
                    if candidate is None:
                        rejected += 1
                    elif candidate in seen:
                        duplicates += 1
                    else:
                        seen.add(candidate)
                        if len(entries) < max_urls:
                            entries.append({"url": candidate,
                                            "source_method": "sitemap_index" if root_kind == "sitemapindex" else "sitemap_urlset"})
                    # Keep memory independent of the number of source entries.
                    element.clear()
                depth -= 1
        if depth != 0 or root_kind is None:
            raise SitemapXMLRejected("Incomplete sitemap")
    except (DefusedXmlException, ParseError, UnicodeError) as exc:
        raise SitemapXMLRejected("Unsafe or malformed XML") from exc

    return {
        "success": True,
        "source_url": source_url,
        "source_type": "sitemap_index" if root_kind == "sitemapindex" else "urlset",
        "entry_type": "sitemap_reference" if root_kind == "sitemapindex" else "page_url",
        "urls": entries,
        "discovered_in_scope": len(seen),
        "entries_examined": scanned,
        "rejected_entries": rejected,
        "duplicate_entries": duplicates,
        "truncated": len(seen) > len(entries),
        "all_supplied_xml_parsed": True,
        "source_completeness": "unknown",
        "fetch_permissions_verified": False,
    }
