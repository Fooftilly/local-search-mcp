# Cache lifetime settings (issue #13)

The loopback dashboard has a **Cache TTL** page for search and fetch results.
Defaults are 45 and 120 seconds; allowed ranges are 5–900 and 5–1800.
POST writes require the same browser origin and CSRF checks as domain
permissions, an explicit `APPLY` confirmation and a fresh state token.
Unsafe files and stale edits are never overwritten.

Only two bounded integers are saved to a separate owner-only
`cache-ttl.json` (0600), under the existing owner-only config directory
(0700). No queries, URLs, cache responses, credentials or history are saved.

Changes apply to future **new cache entries** without a restart. In-flight
requests read the latest effective lifetime immediately before cache insertion.
Existing
cached responses retain their original expiration; no cache is wiped.
Cache size limits, privacy safeguards, `fresh=true` and mandatory
domain authorization are unchanged. This does not persist cache payloads.

Optional `LOCAL_SEARCH_MCP_CACHE_SEARCH_TTL` and
`LOCAL_SEARCH_MCP_CACHE_FETCH_TTL` environment overrides take precedence
and lock dashboard editing until removed and the service restarted.
Malformed storage or overrides fail back to the previous TTL defaults at
runtime, with a warning, but the file is never silently repaired.
The versioned cache TTL preferences are supported by the private state archive
and isolated restore smoke checks (see [BACKUP.md](../BACKUP.md)). A separate
[settings transfer](CONFIGURATION.md#portable-nonsecret-settings-transfer-issue-13)
page exports and previews/imports these two nonsecret values. Fixed cache
capacity limits and deployment ports are read-only on the local
`/status` page; port changes and restarts are managed outside the dashboard.
