"""Bounded comparison of two already-authorized normalized Markdown snapshots.

Pure computation: no network, cache writes, filesystem, or durable page history.
The caller must enforce both live capability/permission checks before AND after.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import json

MAX_CHARS = 64_000
MAX_LINES = 1_024
MAX_CHANGES = 12
MAX_CONTEXT_CHARS = 120
MAX_OUTPUT_BYTES = 6_000


def _offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def compare(left: str, right: str, *, max_changes: int = 12,
            context_chars: int = 120) -> dict:
    if type(max_changes) is not int or not 1 <= max_changes <= MAX_CHANGES:
        raise ValueError("max_changes must be between 1 and 12")
    if type(context_chars) is not int or not 1 <= context_chars <= MAX_CONTEXT_CHARS:
        raise ValueError("context_chars must be between 1 and 120")
    if (not isinstance(left, str) or not isinstance(right, str)
            or len(left) > MAX_CHARS or len(right) > MAX_CHARS):
        raise ValueError("Comparison text limit exceeded")
    a = left.splitlines(keepends=True)
    b = right.splitlines(keepends=True)
    if len(a) > MAX_LINES or len(b) > MAX_LINES:
        raise ValueError("Comparison line-count limit exceeded")
    offsets_a, offsets_b = _offsets(a), _offsets(b)
    # Only bounded, in-memory normalized lines. autojunk avoids large costs
    # on repeated navigation elements; worst-case cost is capped by 1024 lines.
    opcodes = SequenceMatcher(None, a, b, autojunk=True).get_opcodes()
    all_edits = sum(tag != "equal" for tag, *_ in opcodes)
    # "text_equal" refers only to these already captured Markdown snapshots.
    # It says nothing about page completeness, dates or publisher changes.
    result = {
        "success": True,
        "text_equal": left == right,
        "changes": [],
        "total_changed_regions": all_edits,
        "changes_truncated": False,
        "left_chars": len(left),
        "right_chars": len(right),
        "offset_units": "unicode_code_points_in_normalized_markdown",
        "source_completeness": "unknown",
        "publisher_revision_verified": False,
        "chronological_order_verified": False,
        "comparison_scope": "temporary_captured_markdown_only",
    }
    for tag, start_a, end_a, start_b, end_b in opcodes:
        if tag == "equal":
            continue
        if len(result["changes"]) >= max_changes:
            result["changes_truncated"] = True
            break
        span_a = left[offsets_a[start_a]:offsets_a[end_a]]
        span_b = right[offsets_b[start_b]:offsets_b[end_b]]
        candidate = {
            "kind": tag,
            "left_start_offset": offsets_a[start_a],
            "left_end_offset": offsets_a[end_a],
            "right_start_offset": offsets_b[start_b],
            "right_end_offset": offsets_b[end_b],
            "left_excerpt": span_a[:context_chars],
            "right_excerpt": span_b[:context_chars],
            "left_excerpt_truncated": len(span_a) > context_chars,
            "right_excerpt_truncated": len(span_b) > context_chars,
        }
        result["changes"].append(candidate)
        if len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_OUTPUT_BYTES:
            result["changes"].pop()
            result["changes_truncated"] = True
            break
    if len(result["changes"]) < all_edits:
        result["changes_truncated"] = True
    return result
