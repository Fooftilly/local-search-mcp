# Indexed private request history (issue #14, first slice)

The local history dashboard at `http://127.0.0.1:8791/history` now reads
an indexed SQLite sidecar, built from the existing JSONL history and its three
rotated files. It supports exact **operation**, **status**, and **domain** filters,
shows the most recent 25 matching events, and includes grouped counts and
average duration (ms). Only the loopback-only dashboard exposes this view;
there is no permission-changing MCP tool.

## Storage, migration and privacy

- **JSONL remains the source of truth**; original rotation and CSV/JSON CLI
  exports are unchanged. The sidecar is named `history.jsonl.sqlite3` beside
  the configured `LOCAL_SEARCH_MCP_HISTORY_FILE`, with private permissions.
  Deleting that sidecar causes it to rebuild from the currently retained JSONL
  files on the next dashboard request.
- Import uses the existing append lock, holds a SQLite write transaction, and
  tracks file inode + byte offset to avoid duplicate imports when rotation
  renames files. Incomplete trailing lines are retried after completion;
  malformed or oversized events are ignored.
- The SQLite index includes **time, tool, status, hostname, backend and
  duration only**. It does **not** copy URL paths, query/fragment data, search
  terms, result snippets, page bodies, credentials or opaque document handles.
  JSONL remains private and may already contain search-query terms.
- Indexed records are retained for **30 days by default**; configure
  `LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS` from 1 to 365. A maximum of
  100,000 newest rows is enforced. Expiry purges the sidecar, **not the
  JSONL originals**. Existing JSONL rotation still determines how far back
  the index can be rebuilt after deleting it.
- The filter only matches full hostnames and statuses; it does not do
  substring searches over potentially sensitive source text.

## Operational notes

This is the first observability slice. MCP-native metrics, finer-grained
cache/error counters, and optional enhanced CLI indexing remain tracked by
[issue #14](https://github.com/Fooftilly/local-search-mcp/issues/14).
No live firewalling or crawler egress isolation is implied by the dashboard.

## Read-only MCP operational metrics

The `web_metrics(hours=24)` MCP tool returns aggregate operation counts,
status outcomes and average duration in milliseconds for the last **1**, **24**
(default) or **168** hours. Metrics derive from the same retention-bounded
SQLite history index; old events that have rotated out of JSONL or passed the
retention cutoff cannot be reconstructed. A missing or broken index returns a
bounded `history_unavailable` result, not path details or credentials.

The tool additionally reports **process-lifetime** search/fetch cache hit,
miss, coalescing, refresh, uncached operation, stored-entry, eviction and fetch
policy-invalidation counters, along with current cache entry counts and byte
usage. Cache counters reset when the bridge restarts and are deliberately not
mixed into durable history totals. No URL keys, search queries, page text,
document handles, individual request IDs or per-domain user activity are
exposed through the MCP endpoint. The tool is read-only and does not provide
any policy-changing actions. It measures bridge-level events; it does not
certify the crawler's separate network isolation or expose backend-native
Crawl4AI statistics.
