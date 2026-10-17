# Bounded request caching and failure handling

Search and short-page fetch results are cached **in memory only** for the running MCP bridge. No cache entries are written to SQLite or JSONL. Audit history is a separate feature and can still log search terms; avoid secrets in queries.

| Item | Limit |
| --- | --- |
| Search result TTL | 45 seconds |
| Short public-page fetch TTL | 120 seconds |
| Maximum cached entries (combined) | 64 |
| Maximum approximate cache bytes (combined) | 4 MiB |
| Maximum serialized entry size | 128 KiB |
| Retries for local upstream calls | 2 attempts total (one 150 ms backoff) |

Search keys include the complete validated query, page, selected engines, categories, language, time range, result count, and the configured local SearXNG endpoint. Sensitive-looking queries containing URLs, email addresses, or apparent password/token parameters **bypass the cache**; this is a conservative heuristic, not a privacy detector. No search responses are shared across different running bridge processes.

Fetch cache keys retain HTTP(S) scheme, hostname, path, explicit port and raw/fit mode. **Fragment-bearing browser URLs bypass caching** because JavaScript and authentication flows can use fragments to produce distinct content. Fetches with query strings or URL credentials are **never cached**. Only successful, short, untruncated page responses can enter the fetch cache. Temporary continuation handles and pages from long documents are not cached by this feature. Avoid configuring your upstream crawler to reuse authenticated browser cookies: even queryless pages can be private. The bridge does not add authentication to target websites and has no reliable way to identify every private page.

**Every** `web_fetch` call must pass the latest domain approval, public DNS and egress-precondition checks before returning a cached value. An explicit dashboard domain-rule change invalidates *all* fetch cache entries immediately (conservative, not only one hostname). Fetches started before a rule change cannot repopulate the cache, and a concurrent rule change causes a blocked result rather than exposing the in-flight content. A cache hit does not contact Crawl4AI or re-run its `/health` check; to force an upstream check, use `fresh=true`. Ensure the crawler's outbound network isolation remains deployed.

To force a fresh request, add `fresh=true` to either MCP tool:

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux releases' fresh=true --auth none
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/article' fresh=true --auth none
```

Concurrent identical default requests share one in-flight upstream operation even before it is cached. `fresh=true` starts its own upstream request while default callers can still use a pre-existing cached entry. On success the fresh result replaces that entry; on failure the old entry is evicted. A late older request for the *same key* cannot overwrite the refreshed value; unrelated in-flight cache operations remain unaffected. Cache entries expire on access and are removed on later requests, or when the process exits; TTL is not a guarantee that bytes have been erased from idle process memory at the deadline.

SearXNG GET and Crawl4AI health GET calls retry *at most once* for transient connection errors, timeouts, HTTP 429, and selected 5xx statuses. Crawl4AI **POST** retries only connect-phase errors and HTTP 429/503; read timeouts are not retried because the original browser job may still be running. No retry is attempted for normal invalid arguments, authentication failures, 400/401/403/404 or other permanent responses. Retry-After values are never allowed to create an unbounded sleep. Failed or malformed results are not cached. Empty SearXNG responses containing nonempty `unresponsive_engines` are reported as an explicit upstream error instead of being cached as a valid zero-result search. Search keeps its existing list return shape on success, but errors now return structured `{success:false,status,backend,reason,http_status?}`. Fetch errors use the existing structured failure-result pattern and preserve the original requested URL. Clients should inspect the `success` or `status` field on an error instead of assuming all responses are a list.

The cache is deliberately ephemeral and process-local: there is no disk schema, cleanup job, persistent archive, or multi-worker sharing requirement.
