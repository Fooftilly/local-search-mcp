# Bounded batch fetching

`web_fetch_batch(urls: list[str], mode="raw", fresh=false)` is a separate, opt-in MCP tool for fetching several approved public URLs. The original `web_fetch` and `web_fetch_page` calls are unchanged. **This tool cannot approve domains**, and the local dashboard remains the only authority for granting permissions.

## Resource limits

| Property | Limit |
| --- | --- |
| URLs per batch | 1–8 |
| Maximum concurrent unique fetches | 3 |
| Maximum individual URL length | 2,048 characters |
| Maximum combined URL length | 8,192 characters |
| Retry and request timeouts | The existing `web_fetch` security probe, Crawl4AI HTTPX timeouts and bounded retry rules |
| Continuation pages | Existing 24,000-character pages and process-local 15-minute handles |

Input size, `mode` and `fresh` types are validated **before** starting any fetches. If a batch is oversized or otherwise invalid, the MCP tool returns a validation error and does not submit workers. A separate worker handles each **distinct input URL**; repeated exact URLs share one in-batch result even when `fresh=true`, but remain separate result rows. No disk cache or batch-result archive is created.

Results preserve **original input order** despite completion order:

```json
{
  "count": 2,
  "results": [
    {"index": 0, "requested_url": "https://example.com/a",
     "result": {"url": "https://example.com/a", "success": true,
                "markdown": "Public content", "truncated": false}},
    {"index": 1, "requested_url": "https://example.org/b",
     "result": {"url": "https://example.org/b", "success": false,
                "status": "approval_required", "reason": "Approve locally first"}}
  ]
}
```

The example is illustrative: the exact approval response comes from the existing domain policy. Batch failures stay **per URL** and do not abort the other tasks. If a local domain approval changes while the batch runs, earlier successful results are replaced with a conservative `blocked` status before being returned, without leaking already-fetched content. Exceptions produce a bounded `fetch_error` (or `cancelled` for individually cancelled futures), with the exception *type*, never untrusted exception text. Individual successful long-page results retain their opaque `document_id`; use `web_fetch_page` to read later pages normally. Duplicate URL entries share the same continuation capability; treat it as a private bearer token. Because the shared temporary continuation store is globally memory-bounded, a long document's handle can be evicted by later fetches in the same batch. A final validity check substitutes a clear `continuation_evicted` failure for any handle that is **already unusable before batch return**, rather than returning an invalid token. Other concurrently running clients can still evict handles after this check; the capability is ephemeral, not a pin or durable reservation. Retry that URL individually if necessary.

Each input gets a **sanitized private fetch audit event**, including repeated URLs; in-batch duplicate entries are audited without downloading the same page again. History still strips URL queries, fragments and credentials and never saves page text or document handles. The tool's result to its caller includes the requested URLs and fetched text, as the caller originally supplied/requested.

The worker pool is bounded, but it does **not** forcibly interrupt an already running Crawl4AI browser job or DNS resolution. HTTPX timeouts are per network operation, not a hard total wall-clock deadline for the entire batch. MCP request cancellation signals the batch's pending workers to skip URLs not yet started; already-running Crawl4AI/browser jobs cannot be forcibly interrupted. Those running jobs may finish after the caller disconnects, so the bridge's existing browser containment and short retry ceilings remain necessary. Do not use this tool for high-volume crawling or private/authenticated websites.

## Usage

```bash
fastmcp call http://127.0.0.1:8790/mcp web_fetch_batch urls='["https://example.com/a","https://example.org/b"]' --auth none
```

Unknown hosts return `approval_required` when queued or `approval_queue_full` when pending-queue rate/capacity limits are reached; denied hosts return `denied`. None reaches Crawl4AI. For a full queue, ask a local operator to approve deliberately or retry after capacity frees (retrying does not grant approval). Review requests and approve only intentional public domains in the **local** dashboard.
