"""Opt-in bounded JSON-LD extraction from the HTML of ONE approved Crawl4AI result.

Untrusted publisher-supplied hints only: no scripts run, external URLs opened,
network requests, raw HTML returned, deep traversal or durable storage.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re

MAX_HTML_SCAN = 524_288
MAX_SCRIPT_CHARS = 32_768
MAX_SCRIPTS = 8
MAX_JSON_NODES = 24
MAX_RECORDS = 6
MAX_OUTPUT_BYTES = 12_000

_ALLOWED_TYPES = frozenset({
    "Article", "JobPosting", "Event", "Product", "Offer", "FAQPage",
})
# Deliberately exclude nested URLs, identifiers, arbitrary properties and HTML.
_FIELDS = {
    "Article": ("headline", "name", "description", "datePublished",
                "dateModified", "author"),
    "JobPosting": ("title", "description", "datePosted", "validThrough",
                   "employmentType", "hiringOrganization"),
    "Event": ("name", "description", "startDate", "endDate",
              "location", "organizer"),
    "Product": ("name", "description", "sku", "brand", "offers"),
    "Offer": ("price", "priceCurrency", "availability", "priceValidUntil"),
}
_FIELD_LIMITS = {"description": 420, "text": 420}
_NAMED_OBJECTS = frozenset({"author", "hiringOrganization", "location", "organizer", "brand"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_SPACE = re.compile(r"\s+")


def _plain(value: object, limit: int = 180) -> str:
    # Never display HTML/JS markup, URLs, objects or unbounded source strings.
    if type(value) in (int, float) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value or len(value) > limit * 12:
        return ""
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    if (not cleaned or "<" in cleaned or ">" in cleaned
            or any(s in cleaned.casefold() for s in ("javascript:", "vbscript:", "data:", "file:"))):
        return ""
    return cleaned[:limit].encode("utf-8", "replace").decode("utf-8")


def _name(value: object) -> str:
    if isinstance(value, dict):
        return _plain(value.get("name"))
    if isinstance(value, list):
        return next((candidate for item in value[:3]
                     if (candidate := _name(item))), "")
    return _plain(value)


def _offer_fields(value: object) -> dict:
    if isinstance(value, list):
        value = next((v for v in value[:3] if isinstance(v, dict)), None)
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("price", "priceCurrency", "availability"):
        raw = value.get(key)
        if key == "availability" and isinstance(raw, str):
            # Drop URL; retain only recognized schema.org status token.
            raw = raw.rsplit("/", 1)[-1] if raw.startswith(("https://schema.org/", "http://schema.org/")) else raw
            if raw not in ("InStock", "OutOfStock", "PreOrder", "BackOrder",
                           "LimitedAvailability", "Discontinued", "SoldOut"):
                continue
        field = _plain(raw, 60)
        if field:
            result[key] = field
    return result


def _types(raw: object) -> list[str]:
    items = raw[:4] if isinstance(raw, list) else [raw]
    names = []
    for item in items:
        if not isinstance(item, str) or len(item) > 90:
            continue
        value = item.removeprefix("https://schema.org/").removeprefix("http://schema.org/")
        if value in _ALLOWED_TYPES and value not in names:
            names.append(value)
    return names



def _has_schema_type(raw: object, expected: str) -> bool:
    values = raw[:4] if isinstance(raw, list) else [raw]
    return any(isinstance(item, str)
               and item in (expected, f"https://schema.org/{expected}",
                            f"http://schema.org/{expected}") for item in values)


def _trusted_schema_context(value: object) -> bool:
    if isinstance(value, str):
        return value.rstrip("/") in ("https://schema.org", "http://schema.org")
    if isinstance(value, list):
        return any(_trusted_schema_context(x) for x in value[:3])
    return False


def _record(node: dict, context_ok: bool) -> dict | None:
    kinds = _types(node.get("@type"))
    if not kinds:
        return None
    absolute_types = (node.get("@type") if isinstance(node.get("@type"), list)
                      else [node.get("@type")])
    if not context_ok and not any(
            isinstance(v, str) and v.startswith(("https://schema.org/", "http://schema.org/"))
            for v in absolute_types[:4]):
        return None
    kind = kinds[0]
    values = {}
    if kind == "FAQPage":
        questions = node.get("mainEntity")
        if not isinstance(questions, list):
            questions = [questions] if isinstance(questions, dict) else []
        answers = []
        for q in questions[:6]:
            if not isinstance(q, dict) or not _has_schema_type(q.get("@type"), "Question"):
                continue
            name = _plain(q.get("name"), 180)
            accepted = q.get("acceptedAnswer")
            if not isinstance(accepted, dict) or not _has_schema_type(accepted.get("@type"), "Answer"):
                continue
            answer = _plain(accepted.get("text"), _FIELD_LIMITS["text"])
            if name and answer:
                answers.append({"question": name, "answer": answer})
            if len(answers) >= 3:
                break
        if answers:
            values["questions"] = answers
    else:
        for key in _FIELDS[kind]:
            raw = node.get(key)
            if key == "offers":
                val = _offer_fields(raw)
            elif key in _NAMED_OBJECTS:
                val = _name(raw)
            elif key == "availability":
                val = _offer_fields({"availability": raw}).get("availability", "")
            else:
                val = _plain(raw, _FIELD_LIMITS.get(key, 180))
            if val:
                values[key] = val
    if not values:
        return None
    return {"type": kind, "fields": values,
            "source": "page_jsonld_unverified", "publisher_claims_verified": False}


class _ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.observed = 0
        self.exceeded = False
        self._active = False
        self._buf: list[str] = []
        self._length = 0

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        mime = next((v for k, v in attrs if k.lower() == "type"), None)
        if not isinstance(mime, str) or mime.strip().casefold().split(";", 1)[0].strip() != "application/ld+json":
            return
        self.observed = min(self.observed + 1, MAX_SCRIPTS + 1)
        if self.observed > MAX_SCRIPTS:
            self.exceeded = True
            return
        self._active = True
        self._buf = []
        self._length = 0

    def handle_data(self, data):
        if self._active:
            self._length += len(data)
            if self._length > MAX_SCRIPT_CHARS:
                self.exceeded = True
                self._active = False
                self._buf = []
            else:
                self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._active:
            self._active = False
            self.scripts.append("".join(self._buf))
            self._buf = []


def extract(page: dict) -> dict:
    """Bounded parser over CrawlResult.html; never returns raw HTML/JSON."""
    html = page.get("html")
    if not isinstance(html, str):
        return {"jsonld": [], "jsonld_info": {
            "status": "unavailable", "reason": "Raw HTML absent from crawler result",
            "source": "crawlresult.html", "source_completeness": "unknown"}}
    limited = html[:MAX_HTML_SCAN]
    scan_truncated = len(html) > MAX_HTML_SCAN
    parser = _ScriptCollector()
    try:
        parser.feed(limited)
        parser.close()
    except (ValueError, RecursionError, UnicodeError):
        return {"jsonld": [], "jsonld_info": {
            "status": "invalid_html", "source": "crawlresult.html",
            "source_completeness": "unknown"}}
    records = []
    examined = 0
    invalid_scripts = 0
    capped = scan_truncated or parser.exceeded
    for script in parser.scripts:
        try:
            data = json.loads(script, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON constant")))
        except (ValueError, RecursionError, UnicodeError):
            invalid_scripts += 1
            continue
        roots = data[:MAX_JSON_NODES] if isinstance(data, list) else [data]
        for root in roots:
            if not isinstance(root, dict):
                continue
            context_ok = _trusted_schema_context(root.get("@context"))
            graphs = root.get("@graph")
            nodes = graphs[:MAX_JSON_NODES] if isinstance(graphs, list) else [root]
            if isinstance(graphs, list) and len(graphs) > MAX_JSON_NODES:
                capped = True
            for node in nodes:
                if examined >= MAX_JSON_NODES or len(records) >= MAX_RECORDS:
                    capped = True
                    break
                examined += 1
                if not isinstance(node, dict):
                    continue
                row = _record(node, context_ok or _trusted_schema_context(node.get("@context")))
                if row is None:
                    continue
                # Include only bounded records that fit the overall output.
                candidate = records + [row]
                if len(json.dumps(candidate, ensure_ascii=True).encode("utf-8")) > MAX_OUTPUT_BYTES:
                    capped = True
                    continue
                records.append(row)
            if examined >= MAX_JSON_NODES or len(records) >= MAX_RECORDS:
                capped = True
                break
        if examined >= MAX_JSON_NODES or len(records) >= MAX_RECORDS:
            break
    return {"jsonld": records, "jsonld_info": {
        "status": "available" if records else "none_found",
        "source": "crawlresult.html",
        "source_completeness": "unknown",
        "source_html_scan_truncated": scan_truncated,
        "candidate_scripts_observed": parser.observed,
        "invalid_scripts": invalid_scripts,
        "limits_applied": capped,
        "records_returned": len(records),
        "publisher_claims_verified": False,
    }}
