# Exact-host and expiring domain rules (issue #12, first slice)

The local approval dashboard at `http://127.0.0.1:8791/` now lets the
**local operator** choose each allow/deny rule's scope and expiration.

- **Host + subdomains** (the default): a decision for `example.org` also
  applies to `docs.example.org`, except where a more-specific explicit rule
  overrides it. This preserves the behavior of existing decisions.
- **Exact host only**: a decision for `example.org` does not automatically
  apply to `docs.example.org`. Child hosts need their own permission, or may
  match a separate, less-specific rule.
- **Permanent** (the default), **1 hour**, **24 hours**, or **7 days**: expiration
  is saved in SQLite as a UTC timestamp and checked at every authorization,
  including after dashboard/MCP restarts. An expired rule never grants access.
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
scope and duration. Check both selectors before confirming. The table labels
expired rules and shows scope/expiry. As before, a manual **Remove** erases
the rule and a **Rename** preserves its existing limits.

## Remaining work for issue #12

This is a deliberately scoped first part of the broader permissions and audit
milestone. **Allow once**, a stable request identifier, richer sanitized
decision audit with reasons, and operator-only bulk approval/denial are not
implemented here. Do not mark issue #12 complete until those features and
their race/privacy tests have also merged.

The tests cover legacy schema migration, scope boundaries, explicit child
exceptions, expiry across restart, concurrent expired requests, and HTTP
validation. As with other changes, browser egress isolation and real deployment
security require separate operator validation.
