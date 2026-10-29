# Indexed private request history (issue #14)

The local history dashboard at `http://127.0.0.1:8791/history` now reads
an indexed SQLite sidecar, built from the existing JSONL history and its three
rotated files. It supports exact **operation**, **status**, and **domain** filters plus a
rolling UTC time window (**all retained**, last **1 hour**, **24 hours**, or
**7 days**). The most recent 25 matching events appear above an accessible,
no-JavaScript-required bar visualization of counts by operation and outcome,
including average duration in milliseconds. Both tables use the same filters
and update via same-origin live polling. Only the loopback-only dashboard exposes this view;
there is no permission-changing MCP tool.

## Storage, migration and privacy

- **JSONL remains the source of truth**; rotation is unchanged. For safer
  operator inspection, CSV exports escape terminal controls and prefix formula-like
  strings with a literal apostrophe (including formulas preceded by Unicode
  whitespace); JSON exports escape Unicode controls. CSV is therefore not a
  byte-for-byte copy of the stored history. The sidecar is named `history.jsonl.sqlite3` beside
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
- Filters only match exact hostnames/statuses and bounded time windows; they do
  not search potentially sensitive source text. The bars show counts for **all**
  matching retained events, not only the 25 most recent table rows.

## Opt-in original history details

The **Show search queries and page URLs** checkbox on the Request history
page is disabled by default. When explicitly enabled, search rows display
their original stored search query and fetch rows display the stored page URL
(scheme, hostname and path) rather than only the domain. Live refresh and
filter submissions preserve the operator's selection. Continuation reads
remain domain-only and never reveal private document handles.

This view uses the index's existing generation and byte-offset coordinates
to retrieve matching records from the original private rotating JSONL files.
No URL paths or search terms are copied into SQLite. Missing, evicted, or
unverifiable source records display an unavailable placeholder; there is no
timestamp-based fallback. Untrusted text is safely escaped and long paths
wrap in the existing table. **URL query parameters, fragments, credentials
and ports are stripped by the audit writer**, so the original full URL
including these components cannot be reconstructed from past history.
The displayed URL does not approve or fetch the target.

## Operational notes

The indexed view and read-only MCP metrics address the roadmap's basic
observability scope. The current cache counters cover the bridge process, not
Crawl4AI's separate internal cache. The history SQLite sidecar is rebuildable,
while the original JSONL CLI CSV/JSON exports remain available.
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
