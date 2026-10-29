# Bounded request caching and failure handling

Search and short-page fetch results are cached **in memory only** for the running MCP bridge. No cache entries are written to SQLite or JSONL. Audit history is a separate feature and can still log search terms; avoid secrets in queries.

| Item | Limit |
| --- | --- |
| Search result TTL | 45 seconds |
| Short public-page fetch TTL | 120 seconds |
| Maximum cached entries (combined) | 64 |
| Maximum approximate cache bytes (combined) | 4 MiB |
| Maximum serialized entry size, including key | 128 KiB |
| Maximum cacheable fetch URL key | 2 KiB of UTF-8 |
| Retries for local upstream calls | 2 attempts total (one 150 ms backoff) |

Search keys include the complete validated query, page, selected engines, categories, language, time range, result count, and the configured local SearXNG endpoint. Sensitive-looking queries containing HTTP(S) URLs, host/path forms such as `example.com/private`, email addresses, or apparent password/token parameters **bypass the cache**; this is a conservative heuristic, not a privacy detector. No search responses are shared across different running bridge processes.

Fetch cache keys retain HTTP(S) scheme, hostname, path, explicit port and raw/fit mode. **Any URL containing a literal `?` or `#` delimiter bypasses caching, even when the query or fragment is empty** (such as `/page?` or `/page#`). Origin servers can distinguish an empty query delimiter (`/page?`) from no query; URL fragments, including an empty `#`, are not sent to the origin but remain visible to browser JavaScript. Fetches with URL credentials are also **never cached**. Only successful, short, untruncated page responses can enter the fetch cache. Temporary continuation handles and pages from long documents are not cached by this feature. Avoid configuring your upstream crawler to reuse authenticated browser cookies: even queryless pages can be private. The bridge does not add authentication to target websites and has no reliable way to identify every private page.

**Every** `web_fetch` call must pass the latest domain approval, public DNS and egress-precondition checks before returning a cached value. An explicit dashboard domain-rule change invalidates *all* fetch cache entries immediately (conservative, not only one hostname). Fetches started before a rule change cannot repopulate the cache, and a concurrent rule change causes a blocked result rather than exposing the in-flight content. A cache hit does not contact Crawl4AI or re-run its `/health` check; to force an upstream check, use `fresh=true`. Ensure the crawler's outbound network isolation remains deployed.

## Optional fetch retrieval timestamps and cache age

After updating the MCP and reconnecting a client, pass
`include_retrieval_info=true` to `web_fetch` or `web_fetch_batch`.
Successful fetches gain a small `retrieval_info` object without changing
the default response, enabling extra network requests, or adding storage:

- `retrieved_at`: UTC ISO-8601 timestamp recorded **when the bridge received
  and processed the returned crawl result**. A cache hit preserves the original
  timestamp. This is **not** a publisher's publication/update date.
- `served_from_cache`: true only for a completed cache hit.
- `cache_age_seconds`: nonnegative **monotonic** age of the cached result,
  with millisecond precision; zero when the result came from this request's
  new crawl or an in-flight coalesced crawl.
- `source`: `fresh` (this call ran a new, **cache-eligible**
  crawl, possibly with `fresh=true`), `cache_hit` (served previously
  stored content), `coalesced` (shared another concurrent cache-eligible
  in-flight crawl), or `uncached` (the result could not enter the reusable
  short-page cache, even if `fresh=true` was passed).

Opting in **does not bypass or disable** caching for eligible ordinary
short public pages; it simply reads the cache entry's own origin stamp.
Queries/fragmented URLs, structured or compact fetches, long documents
and one-use grants continue to bypass the reusable cache. For these,
retrieval info is marked `uncached`; no nonexistent cache age is claimed.
A `fresh=true` request crawls upstream and reports `fresh` when
eligible for the cache. The existing cache policy, redirect restrictions,
revalidation, TTL settings, size limits and coalescing are unchanged.

A shifted wall clock does not alter cache age or retention: elapsed age
uses Python's monotonic clock, while the UTC timestamp is only a display
anchor. Errors and blocked/revoked results do **not** include retrieval
information. No query, URL, document handle, content or timestamp is
persisted to audit history or a new file for this feature. A cache hit
proves only that this bridge previously retrieved the text; it does not
prove the publisher's content is still current.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/approved-page' include_retrieval_info=true --auth none
```

To force a fresh request, add `fresh=true` to either MCP tool:

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux releases' fresh=true --auth none
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/article' fresh=true --auth none
```

Concurrent identical default requests share one in-flight upstream operation even before it is cached. `fresh=true` starts its own upstream request while default callers can still use a pre-existing cached entry. On success the fresh result replaces that entry; on failure the old entry is evicted. A late older request for the *same key* cannot overwrite the refreshed value; unrelated in-flight cache operations remain unaffected. Cache entries expire on access and are removed on later requests, or when the process exits; TTL is not a guarantee that bytes have been erased from idle process memory at the deadline.

SearXNG GET and Crawl4AI health GET calls retry *at most once* for transient connection errors, timeouts, HTTP 429, and selected 5xx statuses. Crawl4AI **POST** retries only connect-phase errors and explicit HTTP 429/500/502/503/504 responses; read timeouts and ambiguous response-loss failures are not retried because the original browser job may still be running. No retry is attempted for normal invalid arguments, authentication failures, 400/401/403/404 or other permanent responses. Retry-After values are never allowed to create an unbounded sleep. Failed or malformed results are not cached. Empty SearXNG responses containing nonempty `unresponsive_engines` are reported as an explicit upstream error instead of being cached as a valid zero-result search. Search keeps its existing list return shape on success, but errors now return structured `{success:false,status,backend,reason,http_status?}`. Fetch errors use the existing structured failure-result pattern and preserve the original requested URL. Clients should inspect the `success` or `status` field on an error instead of assuming all responses are a list.

The cache is deliberately ephemeral and process-local: there is no disk schema, cleanup job, persistent archive, or multi-worker sharing requirement.

## Bounded structured table continuation

An approved `web_fetch(include_tables=true)` retains its current bounded
four-table, ten-row preview. If the extracted source exceeds that preview,
the response also contains `table_continuation.table_id` and retained
source-table indices. Call `web_table_page(table_id, table_index,
row_offset=0, max_rows=10, column_offset=0, max_columns=8)` to continue without a new crawl. The preview's
`structured_info.tables.returned_source_indices` maps the four preview
positions to Crawl4AI's extracted table indices.

Only table rows in the **same** approved Crawl4AI response can be retained.
No links are followed, new approvals requested or documents persisted.
The bridge normalizes at most 16 extracted tables, 250 rows per table and
24 retained columns, served in pages of at most 8 columns; cells and
serialized rows are bounded. Retention uses at most
800,000 conservatively estimated JSON bytes per handle, with the **same**
15-minute expiry, eight-entry capacity and combined 3-million-character/byte
memory budget as Markdown continuation. Large tables may evict older
Markdown documents and vice versa.

The response's `has_more` and `next_offset` apply to **retained** rows.
`has_more_columns` and `next_column_offset` describe retained column pages;
callers can combine both offsets to reconstruct a bounded 2D table.
`retained_truncated`, `columns_truncated`, and top-level
`retention_truncated` expose bridge truncation. The source may contain
tables/rows that Crawl4AI did not extract: `source_completeness=unknown`
is intentional, and reaching the last retained row does not prove
publisher completeness.

Every table page revalidates original/final domain authorization, the
policy epoch and single-use proofs through the existing document gate.
The table handle and content are not written to request history. The
operator's installed-host Crawl4AI 0.9.5+ table shape and memory behavior
still need attended acceptance testing.
