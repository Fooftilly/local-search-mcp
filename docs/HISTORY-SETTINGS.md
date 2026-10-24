# Nonsecret history settings (issue #13)

The loopback dashboard at `http://127.0.0.1:8791/settings` has two
validated, nonsecret settings:

- **Indexed history retention:** 1–365 days (default: 30). Applies when the
  history index next processes records. Shortening retention can permanently
  discard indexed rows. It does not restore entries already deleted.
- **JSONL history rotation threshold:** 64,000–100,000,000 bytes (default:
  8,388,608). Applies on the next JSONL append; it does not immediately
  rotate an existing file.

Enter valid values and type **APPLY**. The server checks the current saved
version to reject stale form submissions, and either writes the complete
validated pair or makes no change. No restart is needed for these two values.
The form inherits dashboard Host, Origin, Fetch Metadata and CSRF protections.

The settings file is separate from the launcher's credential-bearing JSON:
`${XDG_CONFIG_HOME:-$HOME/.config}/local-search-mcp/history-settings.json`.
It is written through a private owner-only directory, owner-only file
permissions, and atomic replacement with a lock. The file never contains
API keys, tunnel tokens, URL values, domain approvals, or machine paths.
Malformed, symlinked, world-readable, unsupported or stale configuration
is refused rather than silently overwritten.

Existing `LOCAL_SEARCH_MCP_HISTORY_RETENTION_DAYS` and
`LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES` environment overrides take precedence,
and disable dashboard editing while active. Remove them from the service
environment and restart that service before editing these settings via
the dashboard; the saved value does not override the environment. For
compatibility, the JSONL size environment override retains its historical
64 KB–100 MB clamping behavior.

This remains a partial issue #13 settings implementation. Network ports,
backend health-check and other timeouts beyond the SearXNG search and Crawl4AI
crawl request timeouts documented below, cache policy and other higher-risk
settings still require explicit service ownership, safe restart/apply semantics
and a separate review. There is no automatic service restart or new MCP
mutation tool.

## Live upstream request timeouts (issue #13 follow-up)

The same local dashboard now links to
`http://127.0.0.1:8791/upstream-settings`, a **separate** nonsecret form:

- **SearXNG search timeout:** 5–120 seconds (default 25).
- **Crawl4AI crawl timeout:** 15–300 seconds (default 75).

The new `upstream-timeouts.json` is a separate versioned, owner-only file in
the same private configuration directory. It does not alter the existing
`history-settings.json` format or the credential-bearing `config.json`.
The same Host/Origin/Fetch Metadata/CSRF checks, explicit APPLY confirmation,
full-form validation, stale form rejection and atomic no-follow writes apply.
Invalid or insecure timeout settings never block search/fetch: the original
defaults are used and a warning is logged; the unsafe file is not overwritten.

Timeout changes apply to the next outgoing request without a restart; in-flight
requests and previously cached results are not changed. Crawl4AI's short
`/health` validation timeout is intentionally **not** configurable. Optional
`LOCAL_SEARCH_MCP_SEARXNG_TIMEOUT` and
`LOCAL_SEARCH_MCP_CRAWL_TIMEOUT` environment variables, when set, have
precedence and disable all changes on the timeout form. Remove the overrides
from the service environment and restart that service before editing.
Invalid overrides are safely ignored in favor of the original defaults.

This remains a partial #13 slice. Ports, cache-policy tuning, and disruptive
restart controls are deliberately excluded.
