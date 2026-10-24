# ChatGPT-first setup and maintenance wizard (issue #74, foundation)

Use `./setup.sh` for a conservative interactive menu. This is the first
implementation slice: it reuses existing scripts, rather than introducing
another configuration authority or automatically migrating Docker services.
`./start.sh` and `./stop.sh` retain their original behavior.

Read-only commands (no prompts, writes, downloads, restarts or root privileges):

```sh
./setup.sh --help
./setup.sh --check
./setup.sh --status
./setup.sh --dry-run
./setup.sh --chatgpt
```

`--check` shows redacted configuration sources, Python prerequisites,
local health and user systemd state. It exits nonzero if the local stack is not
healthy or configuration inspection cannot be completed. `--status` provides
read-only diagnostics but does not prove a live remote tunnel registration,
account-side ChatGPT connection, or Docker ownership/firewall isolation.
A missing systemd bus is reported as unknown, not inactive.

`ready: true` still means the configured local services and dependencies
passed the existing health checks; it does **not** require a private Docker
project, since existing healthy shared backends remain supported. The separate
`dedicated_docker_ready` field requires exactly one verified running
Crawl4AI and SearXNG container in the owner-controlled project, with no
unverified project containers or external published crawler. An unknown,
stopped or shared Docker deployment makes only that ownership-specific field
false; it cannot be selected for future on-demand lifecycle management.
Neither field proves firewall containment, remote tunnel registration or
ChatGPT account-side connection.

Dependency checks in `--check` and interactive option 5 run the existing
local-only dependency reporter under the **project `.venv/bin/python`**, never
system Python. When `.venv` is missing, broken, or packages are absent, the
report is explicit and readiness is not claimed. These checks never query PyPI;
remote release checks remain separately and explicitly opt-in.

In the interactive menu, ChatGPT is the default integration. The account-side
MCP connection must still be completed in ChatGPT settings; no client accounts
are modified. Other clients are not installed by default. The first pass
provides an explicitly confirmed path to the existing `./start.sh --setup-only`
configuration workflow and the existing `./stop.sh` ownership-safe shutdown.
The former may install Python dependencies, download a verified tunnel client,
and save private credentials/profile. The latter does not stop a foreign
Hermes Web deployment or touch root-managed firewall rules.
Typing anything other than the advertised confirmation word cancels an action.

The local systemd unit preview and dependency inspection reuse their existing
read-only helpers. Update guidance performs no Git fetch/pull, pip install,
image download, or service restart.

Not yet provided in this initial slice: full managed Docker migration and
repair, direct updates with rollback plans, third-party client installers,
and optional on-demand backend sleep (issue #75; planned default idle timeout:
30 minutes). These remain separate reviewed follow-up work.

On a fresh host, first verify the backend prerequisites (SearXNG JSON mode,
Crawl4AI 0.9.5+ stable, crawler firewall isolation), then run the existing
setup route when those are healthy. The wizard does not bypass those gates.


## Docker project ownership diagnostics (second slice)

Run `./setup.sh --docker-status` or choose menu option 8.
`./setup.sh --status` and `--check` also include the ownership summary.
This inspects only the pinned local Docker socket, validates full container
IDs, the fixed `local-search-crawler` Compose project, managed service labels,
and owner-controlled private Compose files. It never executes Docker start,
stop, update, Compose up/down, or privileged firewall commands.

The report uses `running`, `stopped`, `partially_running`,
`not_started`, `not_configured`, `unverified`, or `unknown`.
It counts separately any containers publishing port 11235 that are
**not** part of the managed project, such as a legacy/shared deployment.
Published-port counts may include stopped containers. It does not reveal
foreign Compose paths, container IDs, names, image names, private labels,
environment values or Docker diagnostics. Unknown or unverified means
ownership **could not be proven**, not that resources are absent.

This ownership check does not verify firewall rules, SearXNG settings,
whether a shared backend is safe, a live remote tunnel registration, or
ChatGPT account enrollment. A security gate and live egress probe remain
mandatory before allowing remote crawling. The wizard never claims
ownership of the existing Hermes Web stack.


## Local update status (third slice)

Run `./setup.sh --update-status` or choose menu option 6 for an **offline,
read-only** comparison of local Git HEAD with the service update watcher's
saved baseline. This reuses `scripts/restart_on_update.py --status`'s
existing inspection logic and does not modify its saved state. A missing,
corrupted or inaccessible baseline is reported conservatively. No Git fetch,
pull, package installation, image download, unit restart, baseline reset or
rollback is performed.

This checks only revisions already available in the **local Git repository**.
It cannot tell whether a newer commit exists on GitHub; it does not indicate
whether an available remote update should be applied. An update must be
separately reviewed and approved by the operator, including security,
dependency and backup/rollback decisions. The wizard still offers no
automatic update operation.

For explicit manual source updates, consult docs/SYSTEMD.md, BACKUP.md and
docs/DEPENDENCY-UPDATES.md. The existing optional `git pull`-triggered
restart watcher remains unchanged.

## On-demand eligibility preview (fourth slice)

Run `./setup.sh --on-demand-status` or choose interactive menu option 9.
The existing `--status` and `--check` reports also show the disabled-by-default
`on_demand_preflight` using the **same** read-only Docker inventory as the
setup status check. It displays only sanitized readiness reasons and never
starts/stops containers, enables a supervisor, persists settings, or changes
firewall rules. `can_activate` remains false even for a dedicated candidate.
The future default idle timeout is 30 minutes, but there is no live idle timer.
Shared backends or incomplete Docker ownership/security verification must be
resolved through a separately reviewed deployment plan before any opt-in.


## Guided production tunnel protection (#74)

For a dedicated running `local-search-crawler` project (exactly one
owner-verified Crawl4AI container and one SearXNG container), with the MCP
bridge listening locally, the wizard can install the privileged firewall
service and protected ChatGPT tunnel without manual `visudo` editing:

```sh
./setup.sh --secure-tunnel-preview
./setup.sh --secure-tunnel --confirm-secure-tunnel SECURE
```

Alternatively choose **11** in the interactive wizard and type `SECURE`.
The preview makes no privileged calls or writes. Installation runs as your
**normal user**, requesting your sudo authentication when necessary.

The guarded action:
1. Requires verified, dedicated, running Docker containers (never controls
   foreign/shared Hermes Web containers), healthy SearXNG/Crawl4AI, and a
   running local bridge/dashboard. It does not start/recreate containers.
2. Refuses to modify an already active tunnel and refuses any conflicting
   existing root-owned guard, firewall unit, user drop-in or sudoers file.
3. Installs missing **fixed, repository-reviewed** guard and root systemd
   unit as root-owned copies. Root never executes an editable Git checkout.
4. Enables the firewall service and checks it is active, then writes exactly
   two narrowly scoped `NOPASSWD` sudoers rules (fixed `--probe` and
   optional on-demand `--preflight` commands) for the actual OS user.
   `visudo -cf` validates this rule both before and after installation.
5. Requires the fixed root-owned live private-denial and public DNS/TCP-443
   reachability probe to pass *without prompting*. This does not verify TLS.
6. Installs the fixed user tunnel `ExecStartPre=+` drop-in, reloads user
   systemd, and enables the tunnel and health timer only after confirming the
   drop-in is loaded.

No keys or tunnel profiles are exported. No Git fetch/pull, pip/Docker image
download, other MCP client changes, automatic on-demand Docker operation, or
live rollback occurs. Installation is idempotent for matching existing files;
conflicting files fail closed instead of being overwritten. If any step fails,
the wizard attempts to stop and disable a partially started tunnel, but
operators **must verify** `systemctl --user is-active local-search-mcp-tunnel.service`
after any failure before permitting remote access. Previously installed
firewall rules remain in place.

The firewall service is a boot-time guard, **not a live kill switch**:
removing nftables rules while the tunnel runs will not revoke the tunnel.
After every reboot, verify the backend containers start before the guard
and that the tunnel remains guarded; containers left with restart policy
`no` still require a separate reviewed startup plan. Even a successful
live probe is bounded evidence, not a full browser sandbox certificate.

No script can enroll an MCP app into the user's ChatGPT account: complete
that approval through ChatGPT settings and test `web_search` and an
approved `web_fetch` after successful installation.
