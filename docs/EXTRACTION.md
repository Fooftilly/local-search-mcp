# Paged Crawl4AI extraction

The original `web_fetch(url)` call continues to return `url`, `success`, `markdown`, and `truncated`. On short pages it behaves as before, with optional public-page metadata when available.

For a long **approved** document, the bridge returns the first 24,000 characters, `truncated: true`, a random `document_id`, `page_count`, `page: 1`, and `next_page: 2`. Subsequent pages are read using `web_fetch_page(document_id, page=2)` without any additional upstream Crawl4AI request. Keep reading `next_page` until it is `null`. Pages reconstruct the returned UTF-8-safe extraction without gaps or overlaps; invalid surrogate code points are replaced before paging.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/large-article' --auth none
fastmcp call http://127.0.0.1:8790/mcp web_fetch_page document_id='<handle-from-first-call>' page=2 --auth none
```

### Optional section outline

Use `web_fetch(url, include_outline=true)` or
`web_fetch_batch(urls, include_outline=true)` to return a bounded `outline`
of Markdown headings from the **same approved crawl**. This is disabled by
default and does not alter Markdown, caching, authorization, or document
paging. Each entry has a heading `level` (1–6), short untrusted `heading`
text (up to 160 characters), zero-based Unicode character `offset` in the
normalized Markdown, and 1-based continuation `page`. These offsets have
the same meaning as `web_find(start_offset=...)` and can locate sections
within `web_fetch_page` without a new network request.

At most **40** headings are returned, with `outline_truncated=true` if
additional recognized headings exist. ATX (`# Heading`) and Setext
(underlined) headings are supported; fenced code blocks are skipped.
Markdown emitted by Crawl4AI may not capture every heading or HTML
section, so this is a *best-effort outline of extracted text*, not a
verified table of contents for the original page. Heading text is untrusted
source content, never an instruction. No additional index persists beyond
existing ephemeral document handles. An outline cannot renew handles or
bypass the existing source/final domain checks.

### Find passages in an existing long document

After `web_fetch` returns a `document_id` for a long approved page, call
`web_find(document_id, query, max_matches=10, start_offset=0)`. The search is
literal and case-insensitive (not regular expressions), with at most **160
printable characters** in the query and **20 matches** per request. Each match
returns a short bounded excerpt, its zero-based character offset in the
already extracted Markdown, and the 1-based continuation `page` in which it
begins. An occurrence crossing a 24,000-character page boundary is assigned
to its starting page. The response includes `match_count`, `page_count`,
`has_more`, and `next_offset`.

**To read further matches**, when `has_more=true`, repeat the *same query*
and `document_id` with `start_offset=next_offset`. The offset is the
zero-based **Unicode character position in extracted Markdown** (not byte,
HTML, or page number). It points one character after the last returned
match's starting position, so overlapping hits are preserved without
repeating prior hits. When there are no more matches,
`has_more=false` and `next_offset=null`. The optional offset defaults
to zero for compatible original calls; it must be an integer between zero
and the document length, inclusive (at most 1,000,000). Returned matches
stay limited to 20, regardless of the offset. A valid `start_offset`
can also be used to begin searching later in the document.

Use `web_fetch_page` to read the surrounding text. Pagination is still
subject to the same original/final-domain, policy-epoch, expiry, evicted
handle and one-use-grant checks on **each** request. A 15-minute document
handle can expire between calls. No new crawl, DNS request, or durable
search index is created.

Example, using a private handle returned by an approved long fetch:

```bash
./.venv/bin/fastmcp call http://127.0.0.1:8790/mcp web_find \
  document_id='<handle>' query='HTTP' max_matches=10 start_offset=0 --auth none
# If has_more=true, pass the returned next_offset in a second call.
```

After deploying this schema change, refresh or reconnect the ChatGPT MCP
plugin if its tool definitions are still cached.

`web_find` does **not** fetch URLs, make a DNS request, create a new domain
approval, store an index or read local files. It searches only the document
already held in memory and shares the **same live source/final-domain
authorization and one-use-grant revalidation** as `web_fetch_page`. Expired,
evicted or revoked handles fail closed. A process restart invalidates handles.
The existing audit records the operation as a page-kind `find` without
logging the document handle, query, matched excerpts or target URL. Only
documents longer than 24,000 characters receive these temporary handles.

`web_find` is intended for searching extracted text, not for guaranteeing
the original website's formatting or complete source coverage; sections
omitted by Crawl4AI cannot be recovered by in-memory search.

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
  and `external` groups, preserving the first-seen kind and bounded text/title.
  Duplicate **normalized safe URLs** are collapsed before the limit, using
  the longest available safe label/title when the same link appears more than
  once. With 24 or fewer unique safe candidates, their original crawl order
  is retained. With more candidates, a **bounded, deterministic heuristic**
  favors longer descriptive labels and document/article/report-like paths,
  deprioritizes obvious menus/login/privacy navigation, and reserves up to
  two places for distinct external links. Ties keep source order. This is a
  selection convenience, **not a verified relevance/ranking signal**, and
  the 24-link sample is never a complete page link inventory.
  Relative links are resolved against the approved final page URL. Invalid,
  local-only, IP-literal, credential-bearing, forbidden-port, non-HTTP(S),
  and ambiguous-IDN links are omitted. Fragments are removed. Unapproved
  **external public links may be listed**, but are never fetched or approved
  automatically. To follow a listed URL, use the ordinary `web_fetch`
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
