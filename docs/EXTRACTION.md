# Paged Crawl4AI extraction

The original `web_fetch(url)` call continues to return `url`, `success`, `markdown`, and `truncated`. On short pages it behaves as before, with optional public-page metadata when available.

For a long **approved** document, the bridge returns the first 24,000 characters, `truncated: true`, a random `document_id`, `page_count`, `page: 1`, and `next_page: 2`. Subsequent pages are read using `web_fetch_page(document_id, page=2)` without any additional upstream Crawl4AI request. Keep reading `next_page` until it is `null`. Pages reconstruct the returned UTF-8-safe extraction without gaps or overlaps; invalid surrogate code points are replaced before paging.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/large-article' --auth none
fastmcp call http://127.0.0.1:8790/mcp web_fetch_page document_id='<handle-from-first-call>' page=2 --auth none
```

### Optional compact first-page responses

To reduce the immediate agent-context size, use
`web_fetch(url, compact_chars=4096)` or
`web_fetch_batch(urls, compact_chars=4096)`. The existing default
is **24,000 characters**, so normal clients keep exactly the same
response format and behavior. A caller may choose any integer from
**2,048 through 24,000**. This limits only the *first* Markdown response,
not the content fetched, the 1,000,000-character extraction bound, or
the 3M-character / 8-document in-process continuation cache.

If a page exceeds the requested first-page length, a normal short-lived,
**authorization-bound** `document_id` is returned even if the whole
document would have fit in the old 24K first page. Read
`web_fetch_page(document_id, page=2)` to continue from exactly
`compact_chars` into the original extracted Markdown, in chunks of up
to 24,000 characters. Subsequent pages, `web_find` match page numbers,
and optional outline page numbers use this same first-page boundary.
The full UTF-8-safe Markdown is reconstructed without gaps or overlap.
The opaque handle still expires after 15 minutes or eviction and
rechecks the **original and final source-host permissions** and any
private one-use proof on every continuation; it is never persisted.

Opted-in compact fetches bypass the normal short-page response cache;
they cannot accidentally reuse a 24K cached response or store a
reusable document capability under the ordinary cache key. They make
one normal guarded Crawl4AI request and require **no extra domain
approval**. The first response contains less text, not necessarily
fewer total tokens if the agent subsequently reads every page.

Illustrative deterministic length comparison (not a live model-token
benchmark): for an 8,000-character approved Markdown document,
`compact_chars=2048` returns **74.4% fewer initial Markdown
characters** than the default 8,000; the remaining 5,952 characters
stay available via page 2. Full-output quality and real token cost
depend on the document and the agent's subsequent page requests.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_fetch url='https://example.com/approved-page' compact_chars=4096 --auth none
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

### Find passages in a temporarily paged document

Whenever `web_fetch` returns a `document_id` for an approved paged fetch (including a compact response to a shorter document), call
`web_find(document_id, query, max_matches=10, start_offset=0)`. The search is
literal and case-insensitive (not regular expressions), with at most **160
printable characters** in the query and **20 matches** per request. Each match
returns a short bounded excerpt, its zero-based character offset in the
already extracted Markdown, and the 1-based continuation `page` in which it
begins. A match crossing a page boundary is assigned to its starting
page; the first boundary is the chosen `compact_chars` or the 24,000-character
default, and remaining continuation pages are up to 24,000 characters each. The response includes `match_count`, `page_count`,
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

Example, using a private handle returned by an approved paged fetch:

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
logging the document handle, query, matched excerpts or target URL. Normally documents exceeding 24,000 characters receive these temporary
handles; opted-in compact fetches also issue handles when the result exceeds
the requested shorter first-page limit.

`web_find` is intended for searching extracted text, not for guaranteeing
the original website's formatting or complete source coverage; sections
omitted by Crawl4AI cannot be recovered by in-memory search.

`web_fetch(url, mode="raw")` retains the previous raw-Markdown preference, falling back to fit Markdown when raw text is absent. `mode="fit"` configures stock Crawl4AI with a `DefaultMarkdownGenerator` and a `PruningContentFilterLXML` so the `fit_markdown` field can be generated. It produces a clear `unavailable_extraction` response if fitted content is unavailable. When supplied, short `title` and `description` fields appear under `metadata`. A canonical URL is returned only if it is an HTTP(S) URL with the same hostname as the approved final page; query strings and fragments are removed. All returned page text and metadata are **untrusted website content**, not instructions.

**Limits and privacy:** Full-page extraction accepts up to **1,000,000 characters**. Larger responses produce a `document_too_large` error, rather than losing the tail silently. Continuations are held **only in local process memory** (not in SQLite or JSONL). Handles remain valid for **at most 15 minutes** and only while their original and final domains remain authorized. If a permission expires or is denied, further pages are blocked until that domain is authorized again; approving unrelated domains does not invalidate the handle. Continuation reads recheck current domain rules without recrawling the page or repeating DNS lookups; expired or denied permissions block the remaining content. Expired entries are purged lazily on the next page read or document save, so idle processes may retain expired text in memory until the next access or process exit. Memory usage remains limited to **8 documents** and **3 million cached characters**. Older documents may be evicted sooner under pressure. A service restart expires all tokens. Expired or unknown handles return `expired_or_unknown`; refetch the approved URL to start again.

A document handle is an unpredictable **bearer capability**: anyone who obtains it while valid **and while domain permissions still allow access** can read the corresponding pages. Avoid sharing handles or logging them; the bridge's request-history logger records page numbers and success/failure, but neither handles nor Markdown content. The MCP tool cannot open local files or select arbitrary cached data without a valid capability. Remote request authorizations, redirect checks, and Crawl4AI egress controls are unchanged. The bridge still requires correctly deployed network isolation to contain the crawler.

This is process-local, temporary continuation storage, not a durable archive. It is designed for a single bridge worker, not independently replicated workers.

To inspect continuation activity without exposing handles or page content, use `python history_cli.py --kind page --last 50` or `python history_cli.py --kind page --csv --last 500`. CSV includes page number and total page count when available.


## Source-advertised alternate document formats

Use `web_fetch(url, include_alternatives=true)` or
`web_fetch_batch(urls, include_alternatives=true)` to inspect **explicitly
advertised format links in the same already-approved HTML crawl**. This is
disabled by default and does not affect the normal fetch response shape or
the short-page cache. `alternatives` is empty when evidence is absent.

The deliberately narrow initial rule recognizes a Markdown `View as:`
group with explicit labelled `Plain Text`, `HTML` / `Compact HTML`, and
`PDF` links for the **same literal path stem** with `.txt`, `.html` and
`.pdf` extensions. RFC Editor's RFC 9110 info page advertises these three
formats together. A generic navigation label such as `Alternative
Implementations` is not sufficient evidence. Other publisher schemas
are intentionally not guessed or treated as verified equivalence.

Each record has `format` (`txt`, `html`, `pdf`), a bounded normalized
`url`, and `evidence="source_labeled_view_as"`. Up to three records share
the same 20 KB structured-data budget with optional links and tables.
`structured_info.alternatives.format_verified=false` means the target's
content type, equivalence, copyright and accessibility are **unverified**.

Only safely normalized public-looking HTTP(S) URLs are reported; data,
local/IP-literal, credential-bearing, unsafe-port and ambiguous-IDN
destinations are rejected. **Listing is not approval**: any later fetch
still requires the full operator-domain authorization, DNS, redirect,
once-grant and isolated-crawler checks. No discovered destination is
fetched automatically, and this is not a PDF extraction workaround.

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
- Opted-in structured extraction also returns `structured_info` with the
  number of **safe unique link candidates examined**, returned links,
  source-field presence, and whether link scan/selection limits omitted any
  candidates. For tables it includes observed input item count, returned table
  count, and per-returned-table observed row counts and row/column truncation.
  These are observations of bounded Crawl4AI output, **not original-site
  totals**. `source_completeness="unknown"` is always explicit; a missing
  Crawl4AI field is not evidence that the page contains no such content.
  Input scan limits (first 100 links per kind and first 10 tables) are
  separately flagged. Malformed items or shared-byte-budget omissions can
  also mark an output truncated.
- Combined structured fields, **including** `structured_info`, retain the
  **20,000-byte JSON item budget**; metadata space is reserved from the
  existing extraction allowance.
  Truncation is a deliberate resource bound, not a claim that the entire
  page's links or tables were captured.

Both flags work with `web_fetch_batch` (the same 1–8 URLs / three-worker
limit), without additional network requests or crawler strategy changes.
Opt-in structured results bypass the existing Markdown-only response cache,
preventing response-shape confusion. Long-document continuation handles
still contain only Markdown and metadata, not another stored link/table index.
The same redirect, permissions and epoch checks gate any returned content.


### Bounded link continuation (opt-in)

`web_fetch(url, include_links=true)` continues to return at most 24
normalized, deduplicated links with its existing scoring and source-order
behavior. When the *same approved Crawl4AI response* contains additional
eligible candidates, the opt-in response now includes
`link_continuation.link_id` and bounded retention metadata.

`web_links_page(link_id, offset=0, max_links=10)` retrieves the unshown
retained link candidates in deterministic content-score/source order, up to
10 per call and a 20 KB response budget. Follow `next_offset` until
`has_more=false`. The ephemeral handle retains at most **96** additional
links and **96 KB** of normalized link records across the first 100
internal and first 100 external upstream rows. `retained_truncated`,
`input_scan_capped` and `source_completeness="unknown"` prevent
claims of full source coverage.

Both the initial link selection and continuation use exactly the same safe
URL normalization. The continuation is valid for at most **15 minutes**
(and may be evicted sooner by the existing shared memory bounds).
Every read checks the currently approved original and final host and any
private one-use proof, using the existing authorization release gate.
A link entry is **not** an authorization to fetch that link; following any
entry still requires `web_fetch` and normal approval/DNS/egress checks.
No new crawl, DNS lookup, script execution, auto-approval, persistent URL
index or audit of bearer handles is introduced. Default
`web_fetch(url)` output remains unchanged.


### Precise bounded passage extraction (opt-in)

For an existing `document_id` returned by `web_fetch` (including
`compact_chars` fetches), call
`web_extract_section(document_id, start_offset, max_chars=3072)`.
This returns an exact **1–8,000 character** Unicode slice from the same
normalized Markdown already held temporarily in the bridge. Unlike
`web_fetch_page`, a requested span can cross page boundaries, and the
response need not return an entire 24K-character continuation page.

The response includes `text`, `start_offset`, `end_offset`,
`next_offset` (or null at end), `page` (containing the slice's starting
position), `page_count`, `total_chars`, and source `url`.
Character offsets are zero-based Python Unicode code-point positions
in the normalized Markdown, matching `web_find` and heading outline
offsets; they are not source HTML positions or UTF-8 bytes.

The tool never recrawls, follows links, accesses files or creates a durable
index. It uses the **same live authorization/revocation checks** as
`web_fetch_page` and `web_find`: original and final domains,
one-use proof, epoch change, TTL and eviction. Handle names, passage text
and query text are absent from the private audit trail; reads are marked as
`page` kind with `action=section`. A token expires after at most 15
minutes and may be evicted sooner. No change to the original fetch defaults.

## Empty extraction and possible access-gate diagnostics

A successful Crawl4AI pipeline is **not** evidence that readable page text was
extracted. When a normally successful `web_fetch` has empty or whitespace-only
raw/fit Markdown, the bridge now returns `success=false` with
`status="empty_extraction"` instead of a misleading successful empty document.

The bridge may additionally supply `possible_access_gate` with
`confidence="heuristic"` and an **evidence category only**, when the verified
final URL ends in a recognized login/challenge path or the already-returned
short document title exactly matches a small known challenge/login title.
These hints are **not verified proof** of a CAPTCHA, paywall or access block.
General mentions of login/CAPTCHA in ordinary article content do not trigger
them; no extra HTTP requests, HTML/body disclosure or challenge-bypass
attempts occur.

Confirmed HTTP 4xx/5xx still return `http_error`; Crawl4AI-reported failure
still returns `crawl_failed`, even if the upstream supplies partial Markdown.
`mode="fit"` with no fit Markdown retains `unavailable_extraction`.
No raw upstream error messages, page bodies or secret metadata appear in
these failures. Existing successful nonempty Markdown output is unchanged.
