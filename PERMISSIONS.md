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

## Single-use ledger and public-target preflight (PRs #43–#44)

The private ledger added in PR #43 stores scoped, expiring, single-use
capabilities apart from the ordinary domain policy table. It atomically
consumes at most one grant across concurrent SQLite connections and rejects
expired, replaced, revoked, and explicitly denied grants. Merely staging a
grant never changes `request()` or `evaluate_fetch_url()` into a permanent
allow. The private capability ID is not present in the MCP schema or public
permission/decision records.

PR #44 added an internal preflight that verifies a normalized HTTP(S) host,
standard public port and all resolved DNS records (no private or multicast
addresses) **before** claiming once. DNS failure does not consume the grant.
The guarded standalone fetch flow described below now uses that preflight;
there is still no operator-facing staging control.

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

Scoped and expiring rules were delivered in PR #36; request IDs and
origin-only auditing in PR #38; bulk local decisions in PR #39; and the
private ledger/DNS preflight in PRs #43–#44. The guarded direct-fetch proof
path has tests, but **the operator-only Allow once UI and decision audit are
not yet implemented**. Keep issue #12 open until the final integration is
reviewed, merged and validated.

The tests cover legacy schema migration, scope boundaries, explicit child
exceptions, expiry across restart, concurrent expired requests, and HTTP
validation. As with other changes, browser egress isolation and real deployment
security require separate operator validation.
