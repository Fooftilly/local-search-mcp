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

## Remaining work for issue #12

The first part delivered scoped and expiring rules. This follow-up adds
stable IDs and sanitized local decision reasons/auditing. **Allow once** and
operator-only bulk approval/denial remain separate unimplemented work. Do not mark issue #12 complete until those features and
their race/privacy tests have also merged.

The tests cover legacy schema migration, scope boundaries, explicit child
exceptions, expiry across restart, concurrent expired requests, and HTTP
validation. As with other changes, browser egress isolation and real deployment
security require separate operator validation.
