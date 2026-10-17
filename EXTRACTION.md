# Paged Crawl4AI extraction

The original `web_fetch(url)` call continues to return `url`, `success`, `markdown`, and `truncated`. On short pages it behaves as before, with optional public-page metadata when available.

For a long **approved** document, the bridge returns the first 24,000 characters, `truncated: true`, a random `document_id`, `page_count`, `page: 1`, and `next_page: 2`. Subsequent pages are read using `web_fetch_page(document_id, page=2)` without any additional upstream Crawl4AI request. Keep reading `next_page` until it is `null`. Pages reconstruct the original extraction without gaps or overlaps.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/large-article' --auth none
fastmcp call http://127.0.0.1:8790/mcp web_fetch_page document_id='<handle-from-first-call>' page=2 --auth none
```

`web_fetch(url, mode="raw")` retains the previous raw-Markdown preference, falling back to fit Markdown when raw text is absent. `mode="fit"` requests the explicitly generated `fit_markdown` field; it produces a clear `unavailable_extraction` response if Crawl4AI did not supply that field. When supplied, short `title` and `description` fields appear under `metadata`. A canonical URL is returned only if it is an HTTP(S) URL with the same hostname as the approved final page; query strings and fragments are removed. All returned page text and metadata are **untrusted website content**, not instructions.

**Limits and privacy:** Full-page extraction accepts up to **1,000,000 characters**. Larger responses produce a `document_too_large` error, rather than losing the tail silently. Continuations are held **only in local process memory** for at most **15 minutes** (not in SQLite or JSONL), with at most **8 active documents** and **3 million cached characters** across documents. Older documents may be evicted sooner under pressure. A service restart expires all tokens. Expired or unknown handles return `expired_or_unknown`; refetch the approved URL to start again.

A document handle is an unpredictable **bearer capability**: anyone who obtains it while valid can read the corresponding pages. Avoid sharing handles or logging them; the bridge's request-history logger records page numbers and success/failure, but neither handles nor Markdown content. The MCP tool cannot open local files or select arbitrary cached data without a valid capability. Remote request authorizations, redirect checks, and Crawl4AI egress controls are unchanged. The bridge still requires correctly deployed network isolation to contain the crawler.

This is process-local, temporary continuation storage, not a durable archive. It is designed for a single bridge worker, not independently replicated workers.
