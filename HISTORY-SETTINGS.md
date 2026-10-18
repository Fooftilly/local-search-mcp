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

This is a partial issue #13 settings slice. Network ports, backend timeouts,
cache policy and other higher-risk runtime settings still require explicit
service ownership, safe restart/apply semantics, and a separate review.
There is no automatic service restart and no new MCP mutation tool.
