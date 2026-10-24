# On-demand backend preferences (issue #75)

The optional idle mode has a separate, strict nonsecret settings record at
`${XDG_CONFIG_HOME:-~/.config}/local-search-mcp/on-demand.json`. It is absent
by default, which means `enabled=false` and `idle_minutes=30`.

This foundation intentionally **does not activate Docker control**. A saved
`enabled=true` setting is only a requested preference; the runtime must
independently prove container identity, restart-policy recovery, crawler
firewall isolation, least-privilege authorization and full-stop status before
starting/stopping anything. Corrupt, linked, or overly permissive files fail
closed, not to an assumed enabled state.

The file contains only a schema identifier and two validated settings:
a boolean `enabled` and an integer idle time of 1–1440 minutes. It has
mode `0600` in an owner-only `0700` private directory, uses atomic replacement,
and never includes tokens, Docker IDs, credentials or URLs. Do not edit while
a running supervisor may use it; the future setup command will coordinate
configuration changes and restore prior restart policies where appropriate.

`./stop.sh` must suppress the supervisor before stopping the bridge/tunnel;
future integration will explicitly preserve that separate full-stop promise.
The current installation remains entirely unchanged while disabled.
