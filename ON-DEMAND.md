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

## Opt-in runtime installation and operator smoke test (#75)

**Off by default.** PRs #98 and #99 provide private preferences and
project-scoped Docker operations; the bridge integration is also opt-in.
The bridge and tunnel stay running while dedicated Docker backends sleep.

Before enabling, install a **separate** owned pair of Crawl4AI and SearXNG
containers using the private Compose setup and verified firewall instructions.
Shared Hermes containers cannot be managed this way. The default
`unless-stopped` policy is preserved during transient manual Docker stop;
`always` or `on-failure` policies are not eligible. The bridge user must
already have permission to access the fixed local Docker socket; that is a
powerful host privilege and should be reviewed by the operator.

Install and review the *root-owned* firewall helper and exact sudoers
permission in `docs/EGRESS-SYSTEMD.md`. Do not grant sudo to a user-writable
checkout. The bridge's `NoNewPrivileges=true` stays in place: a separate
operator-installed user-systemd oneshot obtains the narrow privilege:

```sh
python3 scripts/install_user_services.py --install --guard-probe
systemctl --user daemon-reload
systemctl --user start local-search-mcp-guard-probe.service
./setup.sh --on-demand-enable --idle-minutes 30 --confirm-on-demand ENABLE
```

Activation checks current local backend health, the full owned pair, safe
restart policies and the **live** root-owned firewall negative/public probe.
A verified installation with unavailable sudo/systemd/egress returns an
error instead of activating. After changing mode, **restart only the bridge**
(or restart foreground `./start.sh`); keep the tunnel active.

```sh
./setup.sh --on-demand-status
./setup.sh --on-demand-disable --confirm-on-demand DISABLE
./setup.sh --on-demand-disable --wake-before-disable --confirm-on-demand DISABLE
```

The first disable leaves already sleeping backends stopped; the second
explicitly starts them using the scoped Docker adapter and crawler live
probe before leaving the mode off. `./stop.sh` always persists OFF before
disabling services, so an explicit full stop cannot be undone by a timer.

**Smoke tests on a real installation (never run blindly on a shared stack):**

1. Start the bridge and tunnel, ensure SearXNG and Crawl4AI are healthy, and
   verify `--on-demand-status` shows enabled, running and idle deadlines.
2. Wait the configured quiet interval without requesting MCP work. Inspect
   the two **verified project-owned** backends; they should stop while ports
   8790 (bridge) and 8080 (tunnel) remain usable. A health/dashboard poll
   must not wake them or refresh idle time.
3. Request `web_search` after sleep; only SearXNG should wake and return
   real results. A cached hit should return without starting Docker.
4. Request `web_fetch` on an explicitly approved public domain; Crawl4AI
   should wake and run the fixed privileged egress probe **before** crawling.
   A missing/broken guard must return a safe unsuccessful result, not content.
5. Send overlapping requests and a long batch. Neither backend may be
   stopped while a request is active. Verify idle shutdown happens only
   after the *last* work lease ends. Restart the bridge during sleep and
   confirm no automatic startup occurs until an actual MCP cache miss.
6. Disable with and without `--wake-before-disable`, test a full
   `./stop.sh`, then restart the bridge without re-enabling. There must
   be no background Docker wake.
7. Record actual `docker stats --no-stream` memory before sleep, while
   stopped, and after wake; results depend on backend images and workloads.
   Never claim a fixed MB saving without these measured host results.

The default installation is unchanged. No MCP client can select Docker
names, IDs, shell commands, image references, privileges, or firewall actions.
The supervisor is process-local; a bridge crash discards outstanding leases,
but a restarted bridge reads private settings and verifies the actual Docker
state before arming existing containers. Corrupt settings or wrong/foreign
project identities fail closed. A full host reboot must be tested with
the operator-managed root egress service installed. Do not close #75 until
real hardware tests including crash/reboot and measured RAM pass.
