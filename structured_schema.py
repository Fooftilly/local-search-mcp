"""Pure, bounded declarative CSS/XPath schemas and offline result validation (#300).

This module NEVER performs a crawl, DNS query, permission lookup, or MCP
content release. A validated selector is not a domain authorization. An
upstream extracted_content response MUST remain private until the existing
bridge revalidates its source/final URL, permission epoch and one-use grant.
"""
from __future__ import annotations

import json
import re

MAX_FIELDS = 8
MAX_SELECTOR_CHARS = 96
MAX_SELECTOR_STEPS = 4
MAX_INPUT_BYTES = 64 * 1024
MAX_ROWS = 20
MAX_CELL_CHARS = 240
MAX_OUTPUT_BYTES = 12 * 1024

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z", re.ASCII)
_CSS_STEP = re.compile(
    r"(?:[a-z][a-z0-9-]{0,23})?(?:[.#][a-z_][a-z0-9_-]{0,29}){0,2}\Z",
    re.ASCII | re.IGNORECASE,
)
_XPATH_STEP = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z", re.ASCII | re.IGNORECASE)
_ATTRIBUTES = frozenset({"title", "alt", "aria-label", "datetime"})
_CLASSES = {"css": "JsonCssExtractionStrategy", "xpath": "JsonXPathExtractionStrategy"}
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class StructuredSchemaRejected(ValueError):
    """The caller's specification or returned content is not safe to accept."""


def _safe_selector(value: object, strategy: str) -> bool:
    if (type(value) is not str or not 1 <= len(value) <= MAX_SELECTOR_CHARS
            or not value.isascii() or value != value.strip()):
        return False
    if strategy == "css":
        # Conservative compounds and space-separated descendant combinators.
        # Reject pseudo selectors, operators, grouping, escapes, wildcard,
        # attribute selectors and any arbitrary browser or script expressions.
        steps = value.split(" ")
        return (1 <= len(steps) <= MAX_SELECTOR_STEPS
                and all(step and _CSS_STEP.fullmatch(step)
                        and any(ch.isalnum() for ch in step)
                        for step in steps))
    if strategy == "xpath":
        # No predicates, XPath functions, union, axes, namespaces, recursion
        # after the initial scope marker, wildcards or string expressions.
        prefix = next((p for p in (".//", "//", "./")
                       if value.startswith(p)), None)
        if prefix is None:
            return False
        steps = value[len(prefix):].split("/")
        return (1 <= len(steps) <= MAX_SELECTOR_STEPS
                and all(_XPATH_STEP.fullmatch(s) for s in steps))
    return False


def validate_spec(spec: object) -> dict:
    """Create a canonical, independently bounded extraction specification.

    A spec contains only strategy, base_selector and a nonempty fields list.
    Each field contains name, selector and type='text', or type='attribute'
    plus one of four benign literal attribute names. No user regex,
    transformations, custom JS, nested/list fields, or arbitrary Crawl4AI
    configuration is ever forwarded.
    """
    if type(spec) is not dict or set(spec) != {"strategy", "base_selector", "fields"}:
        raise StructuredSchemaRejected("Invalid extraction specification keys")
    strategy = spec["strategy"]
    if type(strategy) is not str or strategy not in _CLASSES:
        raise StructuredSchemaRejected("Unsupported extraction strategy")
    if not _safe_selector(spec["base_selector"], strategy):
        raise StructuredSchemaRejected("Unsafe base selector")
    fields = spec["fields"]
    if type(fields) is not list or not 1 <= len(fields) <= MAX_FIELDS:
        raise StructuredSchemaRejected("Invalid field count")
    normalized = []
    seen: set[str] = set()
    for field in fields:
        if type(field) is not dict or field.get("type") not in ("text", "attribute"):
            raise StructuredSchemaRejected("Invalid field")
        name, kind = field.get("name"), field["type"]
        allowed = ({"name", "selector", "type"} if kind == "text"
                   else {"name", "selector", "type", "attribute"})
        if set(field) != allowed:
            raise StructuredSchemaRejected("Unexpected field property")
        if (type(name) is not str or not _FIELD_NAME.fullmatch(name)
                or name in seen):
            raise StructuredSchemaRejected("Invalid or duplicate field name")
        if not _safe_selector(field.get("selector"), strategy):
            raise StructuredSchemaRejected("Unsafe field selector")
        item = {"name": name, "selector": field["selector"], "type": kind}
        if kind == "attribute":
            if type(field["attribute"]) is not str or field["attribute"] not in _ATTRIBUTES:
                raise StructuredSchemaRejected("Unsupported attribute")
            item["attribute"] = field["attribute"]
        seen.add(name)
        normalized.append(item)
    return {"strategy": strategy, "base_selector": spec["base_selector"],
            "fields": normalized}


def to_crawl4ai_config(validated_spec: dict) -> dict:
    """Build only the reviewed non-LLM upstream typed configuration.

    Validate again at the composition boundary; no arbitrary caller JSON
    is passed through. Not a function for sending this to an upstream server.
    """
    canonical = validate_spec(validated_spec)
    schema = {"name": "bounded-structured-extraction",
              "baseSelector": canonical["base_selector"],
              "fields": canonical["fields"]}
    return {"type": "CrawlerRunConfig", "params": {
        "extraction_strategy": {"type": _CLASSES[canonical["strategy"]],
                                "params": {"schema": schema}}}}


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict:
    output = {}
    for key, value in pairs:
        if key in output:
            raise StructuredSchemaRejected("Duplicate JSON property")
        output[key] = value
    return output


def _reject_constant(_: str) -> None:
    raise StructuredSchemaRejected("Nonfinite JSON value")


def normalize_extracted_content(raw: object, validated_spec: dict) -> dict:
    """Normalize bounded extracted_content JSON, without authorizing release.

    Reject oversized/malformed JSON or unknown/nested data. Truncate row count,
    cell length, and JSON output with explicit flags rather than silently
    claiming a complete source. Caller must recheck authorization at release.
    """
    canonical = validate_spec(validated_spec)
    if type(raw) is not str:
        raise StructuredSchemaRejected("Expected upstream JSON text")
    try:
        total_input_bytes = len(raw.encode("utf-8", "strict"))
    except UnicodeError as exc:
        raise StructuredSchemaRejected("Invalid input Unicode") from exc
    if not 1 <= total_input_bytes <= MAX_INPUT_BYTES:
        raise StructuredSchemaRejected("Extracted content exceeds input budget")
    try:
        rows = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs,
                          parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise StructuredSchemaRejected("Invalid extracted JSON") from exc
    if type(rows) is not list:
        raise StructuredSchemaRejected("Extracted JSON is not a row list")
    expected = {field["name"] for field in canonical["fields"]}
    normalized: list[dict[str, str | None]] = []
    clipped = False
    for row in rows[:MAX_ROWS]:
        if type(row) is not dict or not set(row).issubset(expected):
            raise StructuredSchemaRejected("Unexpected extracted row fields")
        item: dict[str, str | None] = {}
        for field in canonical["fields"]:
            name = field["name"]
            value = row.get(name)
            if value is not None and type(value) is not str:
                raise StructuredSchemaRejected("Extracted value is not text")
            if value is not None:
                clean = _CONTROLS.sub(" ", value)
                if len(clean) > MAX_CELL_CHARS:
                    clipped = True
                value = clean[:MAX_CELL_CHARS]
            item[name] = value
        if len(json.dumps({"records": normalized + [item]}, ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8")) > MAX_OUTPUT_BYTES:
            clipped = True
            break
        normalized.append(item)
    return {
        "records": normalized,
        "rows_observed_in_response": len(rows),
        "rows_returned": len(normalized),
        "truncated": clipped or len(rows) > len(normalized),
        "extraction_method": "crawl4ai_json_" + canonical["strategy"],
        "site_completeness": "unknown",
        "authorization_verified": False,
    }
