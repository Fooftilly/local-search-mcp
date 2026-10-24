# Optional on-demand backend preflight (issue #75)

This first slice only validates future idle settings and performs an inert
Docker ownership check. **No container sleep, automatic wake or idle timer
is implemented or enabled.**

Run `python3 -m scripts.on_demand_eligibility` to preview a candidate.
Pass `--idle-minutes 45` to validate a different timeout (1–1440
minutes, default 30). Output never exposes container IDs, Compose paths,
credentials, Docker stderr or private data. It never mutates the installation.

A project shared with Hermes Web, a foreign container publishing crawler
port 11235, invalid Compose ownership, missing backends or unavailable Docker
fails closed. Even a dedicated two-container inventory is only a *candidate*;
the code always says `can_activate: false`.

Later work must prove per-service identity and least-privilege permissions,
implement serialized wake/idle-stop and restart-policy restoration, and
run a **live** firewall/egress probe after each crawler wake. An explicit
`./stop.sh` must disable all wake paths first; bridge/tunnel stay up only
during optional idle sleep, never during full stop. The #74 wizard opt-in
will be added only when those gates exist.

## Service identity gate

The inventory reports **only bounded service counts**, never Docker IDs, names,
Compose paths, or inspect output. A candidate must contain exactly one
verified `crawl4ai` container and one verified `searxng` container in the
project, regardless of their running/stopped mix. Two crawler containers
cannot masquerade as the required pair. Legacy or incomplete count-only
reports fail closed. Even a matching pair remains a read-only candidate:
`can_activate` is always `false`; there is no lifecycle operation yet.

## Read-only restart policy audit

A candidate must also have a readable Docker `HostConfig.RestartPolicy` for each
verified backend. The inventory exposes only the four fixed Docker policy names
(`no`, `always`, `unless-stopped`, `on-failure`), never raw inspect output,
container IDs or private configuration. Unreadable or unexpected policies
block the candidate. The `restart_policy_review` field reports
`requires_coordinated_change` if either backend would be restarted by Docker,
or `no_change_observed` when both currently use `no`.

The project's current Compose templates deliberately use
`restart: unless-stopped`. This diagnostic does **not** change those settings,
call `docker update`, or suggest the runtime is safe to sleep: shutdown policy
restoration and full-stop coordination must be implemented and tested in a
separate PR before any on-demand activation. `can_activate` stays `false`.
