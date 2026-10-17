#!/usr/bin/env python3
"""Read/filter/export the local search MCP JSONL history (no extra dependencies)."""
from __future__ import annotations

import argparse
import csv
from datetime import date
from collections import Counter, deque
import json
from pathlib import Path
import sys

from history_log import history_path


def events(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for row in stream:
            try:
                event = json.loads(row)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def main() -> int:
    parser = argparse.ArgumentParser(description="Show/export web_search and web_fetch history")
    parser.add_argument("--last", type=int, default=30, help="Number of newest entries (default: 30)")
    parser.add_argument("--kind", choices=["search", "fetch", "page"], help="Only one kind of call")
    parser.add_argument("--status", choices=["success", "error", "pending", "denied", "blocked", "expired_or_unknown"], help="Filter by outcome")
    parser.add_argument("--since", type=date.fromisoformat, help="Include entries on or after YYYY-MM-DD (UTC)")
    parser.add_argument("--json", action="store_true", help="Print raw JSON lines")
    parser.add_argument("--csv", action="store_true", help="Print filtered events as CSV")
    parser.add_argument("--stats", action="store_true", help="Print event counts instead of records")
    args = parser.parse_args()
    if args.last < 1 or args.last > 100_000:
        parser.error("--last must be between 1 and 100000")
    path = history_path()
    if not path.exists():
        print(f"No history yet: {path}", file=sys.stderr)
        return 0

    def matches(event):
        return ((not args.kind or event.get("kind") == args.kind)
                and (not args.status or event.get("status") == args.status)
                and (not args.since or event.get("time_utc", "")[:10] >= args.since.isoformat()))

    selected = (e for e in events(path) if matches(e))
    if args.stats:
        counter = Counter((e.get("kind"), e.get("status")) for e in selected)
        print(f"History file: {path}")
        for (kind, status), total in sorted(counter.items()):
            print(f"{kind or '?':6} {status or '?':7} {total}")
        return 0
    recent = list(deque(selected, maxlen=args.last))
    if args.csv:
        writer = csv.DictWriter(sys.stdout, fieldnames=["time_utc", "kind", "status", "query", "url", "result_count", "page", "page_count", "http_status", "duration_ms"])
        writer.writeheader()
        for e in recent:
            writer.writerow({name: e.get(name, "") for name in writer.fieldnames})
    elif args.json:
        for e in recent:
            print(json.dumps(e, ensure_ascii=False))
    else:
        print(f"History: {path}")
        for e in recent:
            term = e.get("query") if e.get("kind") == "search" else e.get("url", "")
            if e.get("kind") == "page":
                term = f"page {e.get('page', '?')}/{e.get('page_count') or '?'}"
            count = f" | {e['result_count']} results" if "result_count" in e else ""
            status = e.get("status", "?")
            http = f" | HTTP {e['http_status']}" if "http_status" in e else ""
            print(f"{e.get('time_utc', '?')} | {e.get('kind', '?'):6} | {status:7} | {term}{count}{http}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
