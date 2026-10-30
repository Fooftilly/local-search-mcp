# Document comparison — design and benchmark gate (#213)

**Status: proposal, not an implemented MCP capability.** This document intentionally does not alter `bridge.py`, authorize URLs, retain page versions or claim that documents have changed. Implement a production tool only after reviewing this gate and measured corpus results.

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
6. **Interleaved repetition:** repeated menu lines alternating with 420 independently changed article lines, testing expensive alignment patterns.
7. **Unicode:** multibyte emoji, Montenegrin Latin/diacritic text; verify code-point vs byte distinction.
8. **Full replacement:** all lines differ; ensure output remains bounded.

Record interpreter version, synthetic character counts, peak traced Python memory, elapsed milliseconds and edit-operation counts per case. A benchmark run measures only the offline algorithm; it **does not** validate the actual MCP protocol, security, network isolation, deployment, concurrent requests or real corpus quality.

## Acceptance decisions before feature code

- [ ] Run the offline corpus on Python 3.11 and 3.14; record measurements and acceptable upper bounds, including repetitive input.
- [ ] Decide whether `difflib.SequenceMatcher` is sufficiently bounded or whether a strictly linear fingerprint/line-hash summary is safer for untrusted text.
- [ ] Approve the UX for short documents. It must require explicit opt-in and reuse the existing document-cache budget.
- [ ] Specify two-handle atomic permission/revocation semantics and one-use handling; add tests for epoch changes during comparison, redirects and stale handles.
- [ ] Define exact bytes/code-points output limits, truncation and the contract for equal documents with partial extraction.
- [ ] Confirm independent publisher source identities and the difference between observed retrieval time and document publication/revision time.
- [ ] Add real in-process FastMCP tests and user-agent cross-client acceptance *after* the design gate.
- [ ] Leave #213 open until the gated production feature is implemented and accepted.

**Review finding addressed:** The initial aligned repeated-navigation corpus did not capture shifted or interleaved repetitive cases. Both are now included as distinct synthetic cases. A green CI run establishes functional bounded-input behavior, *not* a measured runtime guarantee on the operator host. Algorithm choice and timing/memory thresholds remain an explicit pending design decision.

**Out of scope:** PDF parser work (#141), screenshot/pixel diffs, crawling links, automatic price watching, source archive, periodic polling, change notifications, approvals or Docker management.
