# Document comparison — design and benchmark gate (#213)

**Status: historical design gate, completed in the bounded implementation in [Document comparison](DOCUMENT-COMPARISON.md) and closed issue #213.** The design below records the original proposals; the operator-host Python 3.14 benchmark and live ChatGPT MCP comparison are completed, while independent-client acceptance is tracked by #215. The candidate uses two existing authorized temporary handles (including opt-in short-page handles from `retain_for_evidence=true`) and no additional fetching.

## Current constraints and evidence

- The bridge currently exposes `web_fetch`, `web_fetch_page` and `web_find`; continuation handles exist only when the normalized approved Markdown exceeds the selected first-page size (default 24,000 Unicode characters).
- `document_pages.py` stores up to eight in-process entries, at most 1,000,000 characters each / 3,000,000 total, with a 900-second TTL. Any new compare handle must *share* these bounds, not create an unlimited version archive.
- Each existing continuation is an opaque bearer reference; the bridge separately revalidates current source/final-host permission, a consumed one-use proof (if present) and the policy epoch before returning text. Merely possessing a handle is not sufficient authorization.
- `retrieval_info` may provide observed retrieval timestamps and cache source. Neither that field, an ETag, a `Last-Modified` header nor a crawler timestamp establishes the publisher's historical revision or causality.
- A short page normally has no continuation token: this is a real MVP usability gap, **not** authorization to cache every short page automatically.

## Proposed opt-in surface (not implemented)

`web_compare_documents(left_document_id, right_document_id, max_changes=12, context_chars=120)`

The caller must explicitly obtain *two* separately authorized snapshots using the existing guarded `web_fetch` path. A future `retain_for_compare=true` opt-in (separately reviewed) may create a temporary handle for short text; no implicit recrawl, scheduled watching, automatic snapshots or durable archive. The tool must return `status=expired_or_unknown` for unavailable handles and `status=blocked` when **either** source/final host, once-proof or policy-epoch check fails. Check both against the **same** captured policy epoch and check again immediately before release. Redact both sides on failure; never return a partial diff of one authorized page.

Output can name verified *request/final URLs* separately, indicate source `retrieval_info` if actually known, mark `source_completeness=unknown`, and report structural edits with zero-based Unicode offsets into the exact fetched normalized Markdown, bounded excerpts and an explicit truncation flag. Both URLs must be independently authorized; matching domain is not sufficient. Comparison does not imply chronological order, publisher authority, or verified content provenance. The output is untrusted source text and must not contain the private once-use proof, bearer tokens, headers, cookies, credentials or unapproved URL content.

## Proposed fail-closed cost envelope

| Limit | Proposal | Rationale |
| --- | ---: | --- |
| Input snapshots | Exactly 2 | Explicit comparison; no monitoring |
| Compared text | At most 64,000 Unicode code points **per side** | Reject larger inputs instead of silent prefix diffs |
| Normalized lines | At most 1,024 per side | Predictable line-level work |
| Output | At most 12 changes and 6,000 UTF-8 bytes total | Low context and no unbounded excerpts |
| Per-change excerpts | At most 120 code points each side | Prevent accidental full-text disclosure |
| Handle age | Existing 900-second TTL; eviction earlier | No new retention |
| Storage | Existing bounded process memory only | No database, files, background workers |
| Requests | **Zero** extra HTTP/DNS/crawler calls in comparison | No side effects or new approvals |

These numbers are **proposals**, not verified production limits. All Unicode spans must agree with `web_find` and proposed `web_extract_section` offsets. Detect missing, invalid, over-limit and pathological inputs *before* computing a diff. Do not claim equality if one response was incomplete, fit-mode-only, an error page, or an inconsistent page snapshot.

## Deterministic offline corpus

Run `python3 scripts/document_compare_benchmark.py` from the repo root. It synthesizes the following cases, with no network, credentials, private files or persisted content:

1. **Unchanged:** exact repeated normalized Markdown; zero edits.
2. **Insertion/removal:** several distinct inserted lines; verify edits identified.
3. **Reorder:** same fragments moved; detect structural differences without claiming publisher revision.
4. **Repeated navigation:** many duplicate heading/menu lines, with a changed aligned suffix.
5. **Shifted repetition:** identical menu lines with a unique anchor moved by 50 positions, testing ambiguous matching.
6. **Interleaved repetition:** repeated menu lines alternating with 300 unique stable anchors and localized changes on every seventh article line, forcing many distinct matched regions even with difflib autojunk enabled.
7. **Unicode:** multibyte emoji, Montenegrin Latin/diacritic text; verify code-point vs byte distinction.
8. **Full replacement:** all lines differ; ensure output remains bounded.

Record interpreter version, synthetic character counts, peak traced Python memory, elapsed milliseconds and edit-operation counts per case. A benchmark run measures only the offline algorithm; it **does not** validate the actual MCP protocol, security, network isolation, deployment, concurrent requests or real corpus quality.

## Historical design decisions and acceptance status

- [x] Run the offline corpus on the operator's supported Python 3.14.8 (all nine cases; worst 15.084 ms, 283,484 bytes traced). The maintainer explicitly waived the previously requested Python 3.11 benchmark when dropping support for Python older than 3.14.
- [x] Choose bounded `difflib.SequenceMatcher` with the implemented 64K-character/1,024-line input limits, 12-region output cap, and 6 KB total JSON output ceiling; this is not a real-time guarantee.
- [x] Require explicit `retain_for_evidence=true` on short fetches, reusing existing time-limited document-cache budgets (implemented in #225).
- [x] Implement independent authorization and atomic two-handle permission/epoch rechecks with tests for revoked/expired/evicted or one-use handles (implemented in #225).
- [x] Define byte/code-point output limits, explicit truncation and `text_equal` semantics for normalized captured Markdown.
- [x] Preserve approved original/final source identities and explicitly decline to verify publisher chronology or revisions.
- [x] Add real in-process FastMCP tests and live ChatGPT MCP comparison acceptance. Independent second-client protocol checks remain in #215.
- [x] Close #213 after merged implementation, live operator comparison, supported Python 3.14.8 benchmark, and explicit waiver of obsolete Python 3.11 benchmarking.

**Current state:** The functionality is delivered and #213 is closed. Separate cross-client compatibility and deployment safety questions are tracked in #215 and #214, respectively.

**Review finding addressed:** The initial aligned repeated-navigation corpus did not capture shifted or interleaved repetitive cases. Both are now included as distinct synthetic cases. A green CI run establishes functional bounded-input behavior, *not* a measured runtime guarantee on the operator host. The chosen 64K-character/1,024-line algorithm bounds and the operator's 3.14.8 synthetic performance results were accepted for the personal deployment; they are not a real-time execution guarantee.

**Out of scope:** PDF parser work (#141), screenshot/pixel diffs, crawling links, automatic price watching, source archive, periodic polling, change notifications, approvals or Docker management.
