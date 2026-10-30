#!/usr/bin/env python3
"""Synthetic-only document comparison cost survey. No MCP, URLs or file I/O.

This design-gate harness measures difflib on bounded, generated Markdown lines.
It prints only aggregate counts/timing/memory, never page or private text.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import json
import platform
import time
import tracemalloc

MAX_CHARS_PER_SIDE = 64_000
MAX_LINES_PER_SIDE = 1_024


def _compare(left: str, right: str) -> dict:
    if (not isinstance(left, str) or not isinstance(right, str)
            or len(left) > MAX_CHARS_PER_SIDE or len(right) > MAX_CHARS_PER_SIDE):
        raise ValueError("Document comparison design limit exceeded")
    a, b = left.splitlines(keepends=True), right.splitlines(keepends=True)
    if len(a) > MAX_LINES_PER_SIDE or len(b) > MAX_LINES_PER_SIDE:
        raise ValueError("Document line-count limit exceeded")
    # This intentionally evaluates only a candidate algorithm, not a
    # production-safe API or resource-limited worker. autojunk=True uses
    # difflib's popular-element heuristic; benchmark repetitive inputs.
    opcodes = SequenceMatcher(None, a, b, autojunk=True).get_opcodes()
    changes = sum(tag != "equal" for tag, *_ in opcodes)
    return {"changes": changes, "equal": changes == 0}


def _cases() -> dict[str, tuple[str, str]]:
    base = [f"### Section {i}\n" + f"Public fact {i} in Nikšić and Podgorica.\n"
            for i in range(400)]
    original = "".join(base)
    added = "".join(base[:170] + ["### New section\nPublic new text.\n"] +
                    base[170:])
    changed = "".join(base[:90] + ["### Replacement\nUpdated facts.\n"] +
                      base[94:])
    reordered = "".join(base[200:] + base[:200])
    repetitive = ("Menu\n" * 500) + ("Article content.\n" * 80)
    repetitive_changed = ("Menu\n" * 500) + ("Updated article content.\n" * 80)
    unicode_left = ("🧭 Život, Šćepan, đakon\n" * 300)
    unicode_right = unicode_left.replace("đakon", "dokaz", 5)
    replacement = "".join(f"Old {i}\n" for i in range(900))
    replacement_new = "".join(f"New {i}\n" for i in range(900))
    return {
        "unchanged": (original, original),
        "insert": (original, added),
        "replace": (original, changed),
        "reordered": (original, reordered),
        "repeated_navigation": (repetitive, repetitive_changed),
        "unicode": (unicode_left, unicode_right),
        "full_replacement": (replacement, replacement_new),
    }


def main() -> None:
    rows = []
    for name, (left, right) in _cases().items():
        tracemalloc.start()
        started = time.perf_counter()
        outcome = _compare(left, right)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rows.append({
            "case": name, "left_chars": len(left), "right_chars": len(right),
            "changes": outcome["changes"], "equal": outcome["equal"],
            "elapsed_ms": elapsed_ms, "peak_traced_bytes": peak,
        })
    assert rows[0]["equal"]
    assert all(not row["equal"] for row in rows[1:])
    print(json.dumps({
        "python": platform.python_version(), "note": "offline synthetic only",
        "limits": {"characters_per_side": MAX_CHARS_PER_SIDE,
                   "lines_per_side": MAX_LINES_PER_SIDE},
        "results": rows,
    }, indent=2))


if __name__ == "__main__":
    main()
