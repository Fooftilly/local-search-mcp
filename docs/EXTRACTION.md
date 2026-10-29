# Paged Crawl4AI extraction

The original `web_fetch(url)` call continues to return `url`, `success`, `markdown`, and `truncated`. On short pages it behaves as before, with optional public-page metadata when available.

For a long **approved** document, the bridge returns the first 24,000 characters, `truncated: true`, a random `document_id`, `page_count`, `page: 1`, and `next_page: 2`. Subsequent pages are read using `web_fetch_page(document_id, page=2)` without any additional upstream Crawl4AI request. Keep reading `next_page` until it is `null`. Pages reconstruct the returned UTF-8-safe extraction without gaps or overlaps; invalid surrogate code points are replaced before paging.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/large-article' --auth none
fastmcp call http://127.0.0.1:8790/mcp web_fetch_page document_id='<handle-from-first-call>' page=2 --auth none
```

`web_fetch(url, mode="raw")` retains the previous raw-Markdown preference, falling back to fit Markdown when raw text is absent. `mode="fit"` configures stock Crawl4AI with a `DefaultMarkdownGenerator` and a `PruningContentFilterLXML` so the `fit_markdown` field can be generated. It produces a clear `unavailable_extraction` response if fitted content is unavailable. When supplied, short `title` and `description` fields appear under `metadata`. A canonical URL is returned only if it is an HTTP(S) URL with the same hostname as the approved final page; query strings and fragments are removed. All returned page text and metadata are **untrusted website content**, not instructions.

**Limits and privacy:** Full-page extraction accepts up to **1,000,000 characters**. Larger responses produce a `document_too_large` error, rather than losing the tail silently. Continuations are held **only in local process memory** (not in SQLite or JSONL). Handles remain valid for **at most 15 minutes** and only while their original and final domains remain authorized. If a permission expires or is denied, further pages are blocked until that domain is authorized again; approving unrelated domains does not invalidate the handle. Continuation reads recheck current domain rules without recrawling the page or repeating DNS lookups; expired or denied permissions block the remaining content. Expired entries are purged lazily on the next page read or document save, so idle processes may retain expired text in memory until the next access or process exit. Memory usage remains limited to **8 documents** and **3 million cached characters**. Older documents may be evicted sooner under pressure. A service restart expires all tokens. Expired or unknown handles return `expired_or_unknown`; refetch the approved URL to start again.

A document handle is an unpredictable **bearer capability**: anyone who obtains it while valid **and while domain permissions still allow access** can read the corresponding pages. Avoid sharing handles or logging them; the bridge's request-history logger records page numbers and success/failure, but neither handles nor Markdown content. The MCP tool cannot open local files or select arbitrary cached data without a valid capability. Remote request authorizations, redirect checks, and Crawl4AI egress controls are unchanged. The bridge still requires correctly deployed network isolation to contain the crawler.

This is process-local, temporary continuation storage, not a durable archive. It is designed for a single bridge worker, not independently replicated workers.

To inspect continuation activity without exposing handles or page content, use `python history_cli.py --kind page --last 50` or `python history_cli.py --kind page --csv --last 500`. CSV includes page number and total page count when available.


## Optional structured links and tables

Use `web_fetch(url, include_links=true, include_tables=true)` to return
**bounded structured data already produced by the same Crawl4AI crawl**.
The default flags are false: existing Markdown, metadata, pagination and
cache formats are unchanged.

- `links`: up to **24** discovered HTTP(S) URLs from Crawl4AI's `internal`
  and `external` groups, preserving their kind and bounded text/title.
  Relative links are resolved against the approved final page URL. Invalid,
  local-only, IP-literal, credential-bearing, forbidden-port, non-HTTP(S),
  and ambiguous-IDN links are omitted. Fragments are removed. Unapproved
  **external public links may be listed**, but are never fetched or approved
  automatically. To follow any listed URL, use the ordinary `web_fetch`
  tool, which checks operator approval and public-DNS/egress protections.
- `tables`: up to **4** structures, each capped at 8 columns, 10 rows,
  200 characters per cell and bounded caption/summary. Malformed entries
  are ignored.
- Combined structured fields have a **20,000-byte JSON item budget**.
  Truncation is a deliberate resource bound, not a claim that the entire
  page's links or tables were captured.

Both flags work with `web_fetch_batch` (the same 1–8 URLs / three-worker
limit), without additional network requests or crawler strategy changes.
Opt-in structured results bypass the existing Markdown-only response cache,
preventing response-shape confusion. Long-document continuation handles
still contain only Markdown and metadata, not another stored link/table index.
The same redirect, permissions and epoch checks gate any returned content.
