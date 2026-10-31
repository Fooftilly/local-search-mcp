# Bounded comparison of two temporary Markdown snapshots

`web_compare_documents(left_document_id, right_document_id, max_changes=12,
context_chars=120)` compares **two separately authorized, already fetched**
temporary Markdown snapshots. The call never crawls either URL, requests a new
domain approval, follows a link, stores page content, schedules a future check
or claims to observe a publisher revision. It accepts only temporary opaque
`document_id` handles returned by the existing guarded `web_fetch` path;
for a short document, obtain an explicit handle using
`web_fetch(url, retain_for_evidence=true)`. The normal short-fetch default
remains unchanged. Existing handles expire after 900 seconds and may be
evicted earlier from the shared eight-document/3-million-character cache.

**Independent authorization and fail-closed behavior.** Both handles are
read together under the existing process cache lock. The bridge revalidates
the **original requested and final URL hostname** and any one-use grant
proof for **each** handle against one policy epoch **before and after**
computing. Expiration, revocation, policy changes, eviction or one invalid
partner cannot expose the other side's content or URLs. There is no
unilateral fallback or additional approval. A missing handle yields a generic
`expired_or_unknown` and unauthorized snapshots yield generic `blocked`
without identifying the side.

**Predictable bounds.** At most 64,000 Unicode code points and 1,024 lines
per side; larger snapshots are rejected, never silently truncated into
a false comparison. The line-oriented, bounded `difflib.SequenceMatcher`
comparison returns at most 12 changed regions, excerpts of at most 120
code points **per side** per change, and a full JSON output of at most
6,000 UTF-8 bytes in JSON serialization. Extra changes/excerpts are
represented by `changes_truncated=true`. Reported start and end offsets
are zero-based Unicode code-point positions in the exact captured normalized
Markdown. `total_changed_regions` counts the algorithm's grouped edits
even when excerpts are omitted; it is not an editing history.

`text_equal=true` means only the **two captured Markdown strings** are
equal. It does not imply a complete webpage, chronological direction, an
authentic published edition, equivalent HTML, or that a publisher changed
its content. `source_completeness=unknown`,
`publisher_revision_verified=false` and
`chronological_order_verified=false` communicate these limitations.
Original and final source URL labels strip queries/fragments and preserve
ports; labels longer than 512 characters are omitted explicitly instead of
being cut into a misleading URI. Raw bearer tokens, one-use proofs, headers,
URL queries and captured page text are not stored in the audit history.
The private audit records only a fixed `compare` action and aggregate duration.

**Security and performance scope.** The existing Host/Origin/CSRF policies,
guarded fetch path and operator domain approvals remain untouched.
Document excerpts are untrusted external Markdown, not executable HTML.
Large repetitive paragraphs may be represented as one structural change;
line-level algorithms cannot prove a semantic or factual difference.
The 64,000-character/1,024-line limits bound work, but process-specific
runtime bounds require actual profiling before treating the tool as a
production service-level latency promise.

The existing synthetic corpus in
`scripts/document_compare_benchmark.py` covers unchanged, inserts,
reorders, repeated navigation, shifted/interleaved repeats, Unicode and
full replacement. In this development session a Python 3.13.5 local
profiling run on the equivalent corpus showed 3.5–47.2 ms and
approximately 80–284 KiB traced peak allocations depending on case;
these are **not** measured operator-host timings or Python 3.11/3.14
performance evidence. In-process FastMCP and synthetic tests run in the
project CI matrix. Remaining acceptance includes live installed MCP
client interoperability and the operator host's own benchmark before
closing issue #213.
