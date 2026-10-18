# Exact-host and expiring domain rules (issue #12, first slice)

The local approval dashboard at `http://127.0.0.1:8791/` now lets the
**local operator** choose each allow/deny rule's scope and expiration.

- **Host + subdomains** (the default): a decision for `example.org` also
  applies to `docs.example.org`, except where a more-specific explicit rule
  overrides it. This preserves the behavior of existing decisions.
- **Exact host only**: a decision for `example.org` does not automatically
  apply to `docs.example.org`. Child hosts need their own permission, or may
  match a separate, less-specific rule.
- **Permanent** (the default), **1 hour**, **24 hours**, or **7 days**:
  timed expirations are saved in SQLite as UTC timestamps and checked at every
  authorization, including after restarts. Permanent rules have no expiry.
  An expired rule never grants access.
  An expired exact-host rule becomes pending on the next request only if
  no less-specific active rule matches **and** the bounded approval queue has
  capacity. Otherwise a less-specific decision applies, or the request reports
  `approval_queue_full`; the expired rule remains visible for manual review.

The most-specific *currently active* rule wins. Expiration and scope are
checked during authorization, not just when the dashboard page is rendered.
Old SQLite databases acquire new columns with defaults that preserve their
existing permanent, subdomain-covering decisions. Invalid scope/duration
inputs are rejected by the server, not trusted from HTML.

Reapproving a domain with the **Allow** or **Deny** action replaces that rule's
scope and duration. Existing-rule forms require an explicit duration selection;
no time-limited rule silently becomes permanent through a default selection. The table labels
expired rules and shows scope/expiry. As before, a manual **Remove** erases
the rule and a **Rename** preserves its existing limits.

## Stable request IDs and local decision audit

The dashboard now shows an opaque stable request ID, the first and most recent
request times, repeat counts and the request origin (scheme and host only). Old SQLite
rows receive IDs during an idempotent migration without changing decisions
or their scope. Each new pending request is assigned an ID; repeats,
reapproval and rename retain it. Local decisions, including bulk queue
deletion records, retain their associated request ID, source origin
and request count at decision time. Historical decisions without identifiers
remain intact with blank new fields.

The operator can enter an optional 160-character reason alongside Allow or
Deny. The local dashboard lists the latest 25 decision records, displaying
time, ID, action, source, request history and the reason. The reason is
validated server-side and HTML-escaped; never put passwords or secrets into
operator notes. Decision history is local-only: it is not a remote MCP tool,
and page bodies, URL paths, and query strings are not stored there.

## Operator-only bulk decisions

The loopback dashboard can now approve or deny **up to 25 selected pending
requests** in one atomic SQLite transaction. Check the desired hosts in the
pending table, choose **Exact host** or **Host + subdomains**, explicitly choose
a duration, optionally enter a 160-character reason, then press **Allow selected**
or **Deny selected**. Every selection carries its stable request ID: if another
dashboard tab already decided or replaced one request, the entire submission
is rejected as stale, with **no partial grants**. Refresh and reselect.

Selected child rules are preserved as explicit decisions even when a selected
parent covers its subdomains. Automatically cleared, unselected child requests
are retained as ID-correlated, origin-redacted audit entries. The rolling-hour
pending admission quotas remain intact. Selection is preserved during live
updates while checkboxes remain checked; the backend always checks the latest
request IDs regardless of what is displayed.

Bulk mutations use the existing loopback-only Host/Origin/CSRF boundary and
**are not MCP tools or remote APIs**. Existing single-domain controls remain
available and unchanged.

## Inert single-use authorization ledger (preparatory slice)

The policy database now has a separately stored, **inert** one-use ledger.
Internal transaction primitives can stage an exact-host or subdomain-scoped
single-use grant, atomically claim it once across concurrent SQLite connections,
verify a private proof against expiry/deny/replacement/revocation, and explicitly
revoke it. Grant IDs are random internal correlators and are **never exposed to
an MCP tool, local dashboard, or existing request/decision API** in this slice.

**Critical:** Staging a grant does **not** make a host effectively allowed,
and no new `Allow once` option is available to the operator yet. Existing
`request()`, `evaluate_fetch_url()`, `web_fetch`, cached fetches, redirects,
batch fetches and continuation pages retain their existing permissions, because
there is no safe grant propagation through all these paths yet. The next slice
must hold private one-use proof data through authorization and result release;
redirects must check both source and final hosts, and batch/page results must
not bypass the consumed grant. This distinction prevents a half-implemented
feature from becoming an implicit blanket allow.

Unit tests exercise inert behavior, concurrent single-claim, expiry/restart,
exact/subdomain boundaries, explicit-deny precedence, replacement/revocation
and invalid input without enabling authorization through MCP.

## Public-target preflight before single-use claims (preparatory slice)

The private `_claim_once_for_public_url(url)` helper validates an HTTP(S)
URL, standard public ports, normalized hostname and public DNS results **before**
consuming a one-use grant. Any malformed or private/mixed DNS answer leaves
the grant unspent. Once DNS succeeds, an atomic SQLite claim rechecks expiry
and explicit deny precedence under a write lock, so concurrent requesters
cannot reuse one grant. Its return value is a private `(hostname, grant_id)`
proof; it is **never included in an MCP response, request log, or dashboard**.

**Preflight itself is internal:** the guarded standalone `web_fetch` flow now uses it; `web_fetch_batch`,
continuation pages or operator decision forms. In particular, the helper
alone is *not* a complete authorization: future fetch integration must guard
final navigation, changing decisions, cache hits, page handles, and late
batch responses against stale or revoked grants. Browser/container egress
isolation remains a separate deployment requirement.

## Guarded one-use fetches and private continuation proofs

When an internal one-use grant has been staged by the local authorization
ledger, a **standalone** `web_fetch` may now claim it once. Standard permanent
permissions still take precedence. The claim is made **only after** DNS/public
target validation and an atomic SQLite recheck. No approval mutation is
exposed as an MCP tool.

The private grant ID remains within the server process and in the
short-lived, in-memory continuation record; it is **never present** in MCP
output, dashboard rows, JSONL history, exported records, or response metadata.
A one-use result bypasses shared fetch caching and request coalescing, and
checks the grant again immediately before releasing content. Expiry, new
explicit denial, grant replacement, and revocation redact results, even if
Crawl4AI finished successfully. Redirects to a distinct hostname additionally
require that final host to have an independently active regular approval.

A `web_fetch_page` continuation from the original one-use fetch is part of
**the same authorization**. Its private proof is checked on every follow-up
read; an expired/revoked proof can never release later document chunks.
Other documents still use their existing permission checks and page API.

`web_fetch_batch` intentionally **does not claim** single-use permissions:
multiple worker results, deduplication and late revalidation require regular
reusable domain approvals. A single-use-only host returns
`approval_required` in a batch without consuming the grant; fetch it
individually instead. The dashboard still does **not** offer an Allow once
button. That is the final UI/audit slice; this code must not be described as
an operator-complete feature before the local control exists.

## Remaining work for issue #12

Scoped and expiring rules were delivered in PR #36; request IDs, origin-only
URL snapshots and decision reasons were delivered in PR #38. PR #39 adds operator-only bulk decisions. A later preparatory slice adds the
inert one-use ledger. **Allow once** in the operator UI and guarded fetch
pipeline remains unimplemented. Do not mark issue #12 complete until
allow-once and its concurrency and post-fetch privacy tests have merged.

The tests cover legacy schema migration, scope boundaries, explicit child
exceptions, expiry across restart, concurrent expired requests, and HTTP
validation. As with other changes, browser egress isolation and real deployment
security require separate operator validation.
