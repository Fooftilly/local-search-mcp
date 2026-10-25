# Portable domain-rule transfer (issue #13)

The loopback dashboard has a **Portable domain rules** section. Choose
**Export** to save versioned JSON containing only active explicit allow/deny
rules: canonical domain, status, exact/subdomains scope, and UTC expiry.

The export never includes pending requests, single-use grants, bearer proofs,
request IDs, reasons, request history, URL paths or queries, credentials,
tunnel tokens, other configuration or machine-specific paths.

Paste the JSON into the dashboard and choose **Validate and preview import**.
This read-only preview lists additions, identical rules and conflicts. Review
scope and permanent grants carefully: if an existing explicit rule differs,
the entire import is refused. Resolve that conflict with the regular local
decision controls. Imports never silently replace explicit domain rules.

After a conflict-free preview, type **IMPORT** to apply. A process-local,
10-minute confirmation is bound to the exact reviewed JSON and policy state;
a swapped payload or expired preview is rejected. The server also verifies
the preview's policy fingerprint inside an SQLite write transaction,
rejects stale previews and newly expired imports, and commits all rules or
none. Existing pending request IDs are preserved; sanitized decision-audit
events are created. The same local Host/Origin/Fetch-Metadata/CSRF defenses
apply. No MCP tool can import/export or otherwise mutate permissions.

Schema v1 example:

```json
{
  "schema": "local-search-mcp/domain-rules/v1",
  "rules": [
    {
      "domain": "docs.example.org",
      "status": "allowed",
      "scope": "exact",
      "expires_at": ""
    },
    {
      "domain": "blocked.example.net",
      "status": "denied",
      "scope": "subdomains",
      "expires_at": "2099-01-01T00:00:00Z"
    }
  ]
}
```

Canonical ASCII domains are required. Public suffixes, IPs, malformed
timestamps, expired rules, duplicates, and unknown JSON fields are rejected.
Limits: 250 rules and 48 KB. An oversized export fails explicitly rather
than truncating the rule set.

**Not a full backup:** this transfer does not preserve all application state
or secrets. Private state backups are documented in [BACKUP.md](../BACKUP.md)
(issue #15). Nonsecret history/upstream/cache editing, validated one-profile
settings transfer, and read-only service/port/limit status are available as
separate local dashboard pages (see [CONFIGURATION.md](CONFIGURATION.md)).
