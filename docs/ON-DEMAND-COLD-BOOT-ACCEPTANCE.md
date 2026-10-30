# Attended cold-boot and failure-recovery acceptance (#214)

**Not an automated reboot, a host fix, or an unattended-use certification.** This checklist collects evidence for the separate operational gate remaining after #75 and the successful SearXNG WAKE/APPLY/CANCEL/idle smoke (#163). The in-repository `ON-DEMAND.md` and `docs/EGRESS-SYSTEMD.md` remain the normative host/egress guides. Work only on the dedicated, operator-owned deployment with a maintenance window and independent console access.

## What is established vs unverified

The operator previously demonstrated that a *managed, verified* SearXNG container can sleep, explicitly wake for the read-only search-engine wizard, cancel without applying settings, and return to the 30-minute idle state. Both project-managed backends were then observed stopped, with Docker restart policy `no`. This proves neither a cold reboot nor the firewall-before-crawler startup order. The current `--on-demand-status` diagnostic explicitly lists **cold-boot recovery and fault handling** as still unverified.

## Phase 0 — Before any reboot (read-only)

Run from the reviewed project checkout; save the text **privately**, redacting any credentials, internal domain names or signed URLs before posting excerpts:

```sh
./setup.sh --boot-audit
./setup.sh --on-demand-status
./setup.sh --docker-status
./setup.sh --check
systemctl --user is-enabled local-search-mcp-bridge.service local-search-mcp-tunnel.service local-search-mcp-health.timer
systemctl --user is-active local-search-mcp-bridge.service local-search-mcp-tunnel.service
systemctl --user list-timers 'local-search-mcp*'
loginctl show-user "$USER" -p Linger
systemctl --user cat local-search-mcp-tunnel.service
systemctl status local-search-mcp-crawler-egress.service --no-pager
```

**No** `sudo` installation, `daemon-reload`, Docker `start`/`stop`, `reset-failed`, root nftables update, systemd restart or `--on-demand-enable` is performed by these commands. Inspect actual effective installed units: a checkout can be newer than root-owned installed guard helpers/drop-ins. The root service name above refers only to this project's documented root egress service; an absent/mismatched unit is a **blocker**, not permission to install one silently.

### Pre-reboot go/no-go

- [ ] `--boot-audit` reports `ready_for_manual_reboot_test` with no blocking findings; this is a *static* readiness result and must still report `safe_to_reboot_proven: false`.
- [ ] `--on-demand-status` shows the persisted `enabled=true`, recognized managed state and idle timeout. Do not treat `can_activate=false` alone as a failure: it is a separate read-only eligibility diagnostic.
- [ ] `--docker-status` identifies exactly the verified intended backends, **zero** unverified project containers and no unexpected published crawler. Both have restart policy `no`. If any ownership/restart-policy check fails, **stop**; do not alter unrelated Docker services.
- [ ] Installed root startup guard, three-command narrowly scoped sudoers rules, and effective tunnel `ExecStartPre` are independently inspected against the reviewed templates. A stopped crawler must be gated through `--tunnel-gate`; a cold wake must use the separate preflight and live probe.
- [ ] Operator has independent console/SSH/RDP recovery path, working firewall-backup and safe downtime. No other workloads depend on the same Docker bridge or published ports.

If any prerequisite is missing, record **blocked** and leave runtime untouched; do not reboot to make the checklist pass.

## Phase 1 — Attended reboot, only after operator approval

An authorized operator initiates the **manual host reboot**, never this MCP or a scheduled agent. On return, run the Phase 0 status and identity commands again *before sending any search or fetch request*.

Expected observations to record (do not assume they happened):

| Evidence | Desired behavior | Blocker |
| --- | --- | --- |
| Root firewall-before-tunnel | Installed guard active before usable remote tunnel | Tunnel accessible without verified guard |
| Systemd user manager | Enabled user units/timer start with the agreed linger/session policy | Start-limit-hit, missing effective drop-in |
| Managed Docker identities | Same two intended backends, restart policy still `no` | Shared/unknown containers, unexpected crawler start |
| On-demand initial state | Project-managed backends may be sleeping until requested | Unexpected automatic crawler start |
| Health and dashboard | Local bridge/dashboard available; a health check does not wake backend | Health poll starts an expensive backend |
| Operator rule state | Existing exact/once/deny grants unchanged | Silent approval or lost permissions |

Do not use `systemctl reset-failed`, reboot/restart Docker again, or change the firewall merely because the first snapshot differs. Investigate the error and preserve fail-closed status; recovery action requires a separate attended operator decision.

## Phase 2 — Authorized cold searches and guarded crawler wake

With the operator observing egress guard and logs, use **only** already-approved public targets. First perform one explicit `web_search(query=..., engines="yahoo", fresh=true)` using an *actually enabled* non-Google engine (e.g. Yahoo, only if enabled). With SearXNG independently verified sleeping **before the call**, `fresh=true` bypasses the process search cache and forces an authorized real search/wake. Confirm SearXNG wakes and returns source-attributed results; record whether the provider itself returns results or a bounded failure, without enabling any provider. Independently test one `web_fetch(url="https://already-approved.example/...", fresh=true)` on an **already approved** public HTTPS page with Crawl4AI verified sleeping before the call. `fresh=true` bypasses its process content cache; never use a cache hit as proof of cold wake. The root installed preflight must succeed before Crawl4AI starts, and a **fresh live guard probe** must pass before any page bytes are released. Do **not** create a new approval solely for acceptance, attempt private URLs, weaken egress, or use the blocked PDF path (#141).

Capture tool success/failure, backend identity and **before/after read-only supervisor states** showing an actually sleeping backend transitioned to running, plus the preflight/live probe evidence. A `fresh=true` request is a necessary cache-bypass precaution, not by itself proof of a wake; if the backend was already running or no verified transition occurred, label the cold-wake gate **not observed**. A healthy HTTP 200 from Crawl4AI **does not certify isolation**; record the separate guard evidence.

After normal inactivity, re-run:

```sh
./setup.sh --on-demand-status
./setup.sh --docker-status
```

Both managed backends should return to sleeping, `managed_running=0`, `managed_stopped=2`, active requests 0, with no unverified containers. Do not manually stop them just to satisfy this gate. No promised exact shutdown time: new requests and activity update the deadline.

## Phase 3 — Fault and negative-path acceptance (separate attended plan)

These scenarios must be **observed or safely simulated in isolated tests** with preapproved rollback, not forced on a live protected host solely for evidence:

1. Docker daemon unavailable or an intended project container missing/mismatched: no arbitrary container can be started; MCP returns bounded `upstream_unavailable`.
2. Root guard/preflight/probe missing or failing: tunnel/crawler request must fail **closed**, with no unguarded crawl or approval.
3. Tunnel start-limit-hit or bridge process restart: operator can diagnose with read-only `systemctl --user status`/`journalctl --user`, without automatic repeated destructive restarts.
4. Concurrent active search/fetch and idle timer: no stop under active lease; existing worker lifecycle and cancellation semantics remain explicit.
5. Root firewall flushed **after** an established tunnel: **known limitation** — the root startup guard is not a continuous kill switch. Do not assert that it closes an already established tunnel. This needs a separately approved mitigation/operational response, not a green acceptance checkbox.

## Evidence to attach to #214

For each phase record: local date/time and installed revision (never a token), operator consent, root/bridge/tunnel state, `--boot-audit` verdict, Docker identity/restart-policy checks, guarded wake status, completed idle return, failures and rollback, and any measurable RAM/latency costs. If a phase is incomplete, label it **not tested** or **blocked** rather than treating tests or code review as live host proof.

**Completion rule:** Close #214 only when the owner has reviewed the actual post-reboot observations, the required safe fault results and keep/de-scope decision. This documentation PR alone cannot satisfy that gate and does not change systemd, sudoers, nftables, Docker, approvals, or automatic service behavior.
