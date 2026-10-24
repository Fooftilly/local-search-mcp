# Optional on-demand backend preflight (issue #75)

The initial eligibility report remains deliberately read-only, but the
separately confirmed opt-in runtime can now sleep and wake verified private
Crawl4AI/SearXNG containers. It is **disabled by default**; after enabling,
the bridge and tunnel remain running while those backends are asleep.
Use `./setup.sh --on-demand-status` to inspect both the read-only ownership
candidate and the live bridge supervisor. `can_activate: false` reflects the
read-only report, **not** the status of an already enabled runtime.

Run `python3 -m scripts.on_demand_eligibility` to preview a candidate.
Pass `--idle-minutes 45` to validate a different timeout (1–1440
minutes, default 30). Output never exposes container IDs, Compose paths,
credentials, Docker stderr or private data. It never mutates the installation.

A project shared with Hermes Web, a foreign container publishing crawler
port 11235, invalid Compose ownership, missing backends or unavailable Docker
fails closed. Even a dedicated two-container inventory is only a *candidate*;
the code always says `can_activate: false`.

The opt-in runtime now verifies dedicated service identity, performs serialized
wake/idle-stop with strict Docker identity checks, and requires a **live**
firewall probe after each crawler wake. An explicit `./stop.sh` disables
wake paths before shutting down bridge/tunnel/backends. Unattended reboot
recovery and fault handling still need host acceptance tests.

## Service identity gate

The inventory reports **only bounded service counts**, never Docker IDs, names,
Compose paths, or inspect output. A candidate must contain exactly one
verified `crawl4ai` container and one verified `searxng` container in the
project, regardless of their running/stopped mix. Two crawler containers
cannot masquerade as the required pair. Legacy or incomplete count-only
reports fail closed. Even a matching pair remains a read-only candidate:
`can_activate` is always `false` in this read-only candidate report;
a separately confirmed opt-in lifecycle is available through the wizard.

## Read-only restart policy audit

A candidate must also have a readable Docker `HostConfig.RestartPolicy` for each
verified backend. The inventory exposes only the four fixed Docker policy names
(`no`, `always`, `unless-stopped`, `on-failure`), never raw inspect output,
container IDs or private configuration. Unreadable or unexpected policies
block the candidate. The `restart_policy_review` field reports
`requires_coordinated_change` if either backend would be restarted by Docker,
or `no_change_observed` when both currently use `no`.

The project's current Compose templates deliberately use
`restart: unless-stopped`. This diagnostic does **not** change those settings
or call `docker update`. The opt-in runtime separately allows verified `no`
and `unless-stopped` policies, preserves them during transient sleep, and
requires full-stop coordination. Reboot policy restoration is not automatic.
`can_activate` remains `false` only because this is a read-only diagnostic.

## Protected reboot with intentionally sleeping backends

The root-owned system service now supports a `--startup` guard mode: after
Docker becomes available, it installs the verified nftables policy for the
fixed crawler bridge even when Crawl4AI is stopped by the on-demand supervisor.
The protected user tunnel's `--tunnel-gate` pre-start command accepts a
**stopped** crawler only if the root firewall rules remain present and match
the verified bridge. A **running** crawler still requires the complete live
negative/public egress probe. Cold `web_fetch` calls must still pass both
the pre-start and post-start probes in the supervisor before fetching content.

This requires an **operator-reviewed upgrade** of the root-owned guard,
root system service, fixed sudoers rule and user tunnel drop-in: an ordinary
`git pull` does not change privileged installed files. Follow
`docs/EGRESS-SYSTEMD.md`; the setup wizard rejects mismatched existing
security files instead of silently overwriting them. Reboot tests require
restart policy `no` on the verified backends: Docker can start a container
using `unless-stopped` at daemon boot **before** the root guard has installed
its nftables rules. Verify both remain stopped until actual MCP work, then
confirm the tunnel reconnects after the root firewall is installed. Do not change Docker
restart policies automatically or remove existing firewall protection.
A host reboot and Docker-daemon-restart validation remain required before
closing #75. System and user systemd services have no cross-manager ordering
guarantee, so an early tunnel attempt must fail closed until the root guard
is ready.

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
