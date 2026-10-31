# ChatGPT-first setup and maintenance wizard (issue #74, foundation)

Use `./setup.sh` for the five-group interactive menu: **1 Setup**,
**2 Connections**, **3 Status**, **4 Update**, **5 Advanced**. Choose a group,
then an action number within it; `0` returns to the groups, and `0` from
the top menu exits. All existing operator actions remain available, plus the guided SearXNG engine selector and an opt-in Cloudflare Portal workflow;
the top level is no longer crowded. This changes only navigation; existing
CLI flags and confirmations remain available, and it does not add another
configuration authority or Docker management path. `./start.sh` and
`./stop.sh` retain their behavior.

Read-only commands (no prompts, writes, downloads, restarts or root privileges):

```sh
./setup.sh --help
./setup.sh --check
./setup.sh --status
./setup.sh --dry-run
./setup.sh --chatgpt
./setup.sh --client cursor
./setup.sh --client hermes
./setup.sh --client claude
./setup.sh --maintenance-versions
```

Local integrations for other clients are explicitly opt-in, and the
wizard **only prints** manual instructions without editing third-party
configurations:

```sh
./setup.sh --client cursor
./setup.sh --client hermes
./setup.sh --client claude
```

Cursor IDE (including the Grok model running *inside Cursor*) can use the
existing loopback Streamable HTTP MCP endpoint
`http://127.0.0.1:8790/mcp` via an `mcpServers.local-search.url` entry in
`~/.cursor/mcp.json`. If the file is absent, create it with
`{"mcpServers": {}}`; if the existing JSON object has no `mcpServers`
member, add that object while preserving its other settings. Insert only the
`local-search` member inside `mcpServers`, not another nested wrapper.
Preserve every existing entry; if the example name already exists, use
an unused name instead.
Hermes Agent on the same host can use
`hermes mcp add local-search --url http://127.0.0.1:8790/mcp`,
followed by `hermes mcp test local-search` and `/reload-mcp` in chat.
If Hermes already has an MCP named `local-search`, choose an unused server
name and substitute it consistently in both commands; never overwrite it.
Claude Code CLI running on the **same Linux host** can also connect via
Streamable HTTP. Select `./setup.sh --client claude` for read-only instructions.
First inspect `claude mcp list` to choose an unused server name, then
run `claude mcp add --transport http local-search http://127.0.0.1:8790/mcp`
**manually** in the directory where you want local-scope configuration.
If `local-search` already exists, use an unused name consistently in the
`claude mcp add` command and verify that new name in `claude mcp list`;
never overwrite an existing client/server configuration. Verify the
connection with `claude mcp list` and `/mcp` inside Claude Code.
The default CLI scope is `local` (private to this directory's Claude Code
configuration). `--scope project` writes a shared project `.mcp.json`, while
`--scope user` is a deliberate global choice for all projects. The wizard does not run Claude Code, create a Desktop
extension or write Claude/Cursor/Hermes configuration files.

Claude.ai, Cowork and Claude Desktop **remote** connectors connect through
Anthropic's cloud and cannot reach a laptop's `127.0.0.1`. They need a
separately secured HTTPS MCP endpoint. Local Claude Desktop MCP configuration
is a different integration not modified by this wizard; never copy the
OpenAI-only secure tunnel credentials to third-party clients. If Claude Code
runs in Docker, a devcontainer or another computer, its `127.0.0.1` is not
the Linux host's loopback; a separate secured reachable endpoint is required.

All three local clients (Cursor, Claude Code and Hermes) share the
existing bridge, crawler safety guard and optional backend idle timer. No new Docker resources are needed.

A **remote** Cursor/Grok bot or Docker-containerized Hermes process does
not reach the Linux host via its own `127.0.0.1`. Never expose loopback MCP
or the local approval dashboard on `0.0.0.0`; cloud agents require a separate
authenticated HTTPS MCP ingress. The OpenAI tunnel is not a generic ingress
for other providers. No enrollment, credential changes, client installation
or remote proxy setup is performed by these preview commands.

## Optional Cloudflare MCP Portal private origin

Connections → Cloudflare MCP Portal supports an offline redacted preview,
attended local hostname preparation and rollback. All steps are documented in
[docs/CLOUDFLARE-MCP-PORTAL.md](docs/CLOUDFLARE-MCP-PORTAL.md).
It never changes a Cloudflare account, creates a public route or starts the
two optional units automatically. Private hostname routing to loopback is
still an unverified deployment gate; do not expose a LAN listener to bypass it.

```sh
./setup.sh --cloudflare-portal-preview
./setup.sh --cloudflare-portal-prepare
./setup.sh --cloudflare-portal-rollback
```

## Choose SearXNG search engines without editing YAML (issues #137, #144)

Select **Setup → 4 (Choose SearXNG search engines)** or run
`./setup.sh --search-engines` from a normal-user terminal. This is an
**operator-only local action**, not an MCP administration tool or dashboard button.

1. **Recommended: Yahoo general default**: selected because Yahoo worked
   on the operator's deployment while the initial four general engines
   were rate-limited or CAPTCHA-blocked. If SearXNG is running, the preset
   excludes all other detected *general* engines. If the backend is sleeping,
   it only changes the known four failing general defaults and explicitly
   warns that other inherited defaults may remain active.
2. **Choose general engines**: list installed general engines from the
   loopback SearXNG `/config` response, with Yahoo first. Enter numbered
   choices, or blank to cancel. No Google engines are enabled implicitly.
3. **Exclude Google-named engines across every category**: inspect the
   running SearXNG's full installed engine list, then set `disabled: true`
   for every name starting with `google` (including `google cse images`).
   Previously enabled non-Google engines remain as configured. **Does not
   require Google to be rate-limited**; this is an explicit provider choice.
4. **Choose engines in a category**: select `images`, `news`, `science`,
   `general`, or any category actually advertised by the running instance.
   Pick installed engine numbers; in a *non-general* category, type `NONE`
   to disable every displayed engine explicitly. The list shows which
   engines are default-enabled and every category they belong to.

**Important:** SearXNG's `disabled` flag applies to the **engine globally**,
not separately to each category. Changing an engine shared by `general` and
`news` changes both. The menu labels shared engines, and existing managed
overrides for unrelated engines remain intact across subsequent choices.

After previewing the selection, type **`APPLY`** exactly; anything else
cancels. The operator-only editor verifies the dedicated Compose ownership,
the exact host bind mount to `/etc/searxng/settings.yml`, owner-only
settings permissions, `use_default_settings: true` and the checksum of its
previous managed `engines:` block. It refuses unmanaged overrides, preserves
the same file inode for Docker's bind mount, and restarts **only** the verified
project-owned running SearXNG container. It checks the resulting enabled
flags and restores previous settings if verification fails. A sleeping backend
remains asleep and uses the saved settings on its next managed wake.

The guided setup flow does not silently change engine settings and never
edits the dashboard, root firewall, Crawl4AI container or unrelated Docker
workloads. Any recovery prompt stays operator-confirmed.

## Create a private backup deliberately (issues #74 / #15)

The setup wizard can create a new owner-only archive using the existing
`scripts.backup_state.backup` implementation. Run:

```sh
./setup.sh --backup-create "$HOME/local-search-state-2026.zip" --confirm-backup BACKUP
```

Or select Advanced → 3 (create backup), supply the new archive filename,
and type `BACKUP` exactly. A relative filename is interpreted from
the directory where `setup.sh` was invoked, **not** the repository root;
absolute paths also work. Without the confirmation, the wizard writes
nothing. Existing destinations are never overwritten and no parent
directories are created automatically. On failure, only a generic status is
printed; check whether the destination already exists before retrying.
The result never includes archive paths, history or credentials.

The archive is private (0600), can contain sensitive history and domain
decisions, and is **not automatically consistent across files** if the
bridge is writing concurrently. For a coordinated backup, stop the
bridge/tunnel manually first and see [BACKUP.md](BACKUP.md). This action
does not modify live state, start/stop services, run a restore, or upload data.

## Verify private backups and test isolated recovery (issues #74 / #15)

To verify an existing private archive without changing your installation
(relative archive paths use the directory where `setup.sh` was invoked,
just like `--backup-create` and the interactive menu):

```sh
./setup.sh --backup-verify "$HOME/local-search-state-2026.zip"
```

This reuses the existing `backup_state.verified_payload` validator for
archive permissions, SHA-256 manifests, allowed settings and SQLite integrity.
For a fuller offline fresh-install simulation, use:

```sh
./setup.sh --restore-smoke "$HOME/local-search-state-2026.zip"
```

The smoke mode calls the existing `restore_smoke.smoke` implementation,
which writes **only disposable temporary files** and exercises the
application's real policy, history and nonsecret settings readers under
isolated XDG directories. Alternatively, choose Advanced → 2 (verify or smoke-test backup),
provide the path and select VERIFY (default) or SMOKE. Neither operation
creates a backup, replaces active files, installs services or activates a
rollback. The JSON result contains status/safety flags and aggregate counts (file
count for verification; total policies and at most 25 recent indexed events
for the smoke test) or a generic failure message, never file paths, approvals,
search terms or credentials. These bounded event counts do not prove full
historical recovery.
Invalid/private-ownership archives fail with a nonzero CLI exit code.
Archives themselves can contain sensitive history: keep them on trusted
private storage and refer to [BACKUP.md](BACKUP.md) for compatibility limits.

## Optional maintenance version inventory (issue #74 / #15)

Run `./setup.sh --maintenance-versions` or select Status → 6 (maintenance versions)
and press Enter for the **offline default**. The wizard reuses
`scripts.maintenance_versions.report` to report project-venv dependency
versions, local Git revision, tunnel-client fingerprint and the
fixed loopback Crawl4AI version/health check. It does not query public
websites, fetch Git objects, install packages, pull images or restart services.

`./setup.sh --maintenance-versions-remote` requests the existing
bounded public PyPI release metadata and GitHub release **and repository
commit** metadata checks (including the public bridge `/commits/master`
endpoint). Status → 6 requires choosing `REMOTE` and then
typing `PUBLIC` to opt in. A different response cancels without public
requests. Interrupting an in-flight lookup may leave some public requests
already sent; it does not apply updates. Public release or revision differences
are informational only; they do not establish compatibility, availability of
container images, or safe installation. Neither mode applies upgrades,
edits credentials, or changes MCP connections. See
`docs/DEPENDENCY-UPDATES.md` and `BACKUP.md` before any manual update.

For on-demand reboot diagnostics, run `./setup.sh --boot-audit` or
select Status → 5 (reboot audit). This read-only audit checks the actual
container restart policies, reviewed root-owned helper/unit installation,
effective root/tunnel startup gates and systemd enablement/linger without
mutating the host or running live firewall probes. The `--boot-audit`
CLI flag returns nonzero for missing prerequisites; Status → 5 (reboot audit)
prints the same report but leaves the menu running. Neither mode declares
reboot safety proven. Review
`ON-DEMAND.md` and `docs/EGRESS-SYSTEMD.md` before a manual reboot test.

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

Dependency checks in `--check` and Status → 2 (dependency versions) run the existing
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

Not yet provided: full managed Docker migration and repair, direct updates
with rollback plans, and third-party client installers. These remain separate
reviewed follow-up work. Optional on-demand backend sleep/wake is implemented
for verified dedicated containers, with an explicit wizard confirmation and
a 30-minute default idle timeout; unattended reboot recovery remains unverified.

On a fresh host, first verify the backend prerequisites (SearXNG JSON mode,
Crawl4AI 0.9.5+ stable, crawler firewall isolation), then run the existing
setup route when those are healthy. The wizard does not bypass those gates.


## Docker project ownership diagnostics (second slice)

Run `./setup.sh --docker-status` or choose Status → 3 (Docker ownership).
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


## Attended software update

Run `./setup.sh --update` (or Update → 1 (attended update)) for the one
supported update procedure. The operator must approve `FETCH` before any
GitHub network request, review the proposed revision and changed file list,
then approve `UPDATE` before project services, state backup, Git checkout or
Python dependencies are changed. Temporary MCP downtime is expected.

The workflow stops only verified project-owned systemd units, saves the
existing private policy/history archive, fast-forwards the reviewed GitHub
master, and starts previously active bridge/tunnel units. It does not modify
Docker, the root-owned egress firewall or unrelated services. A failed update
does not automatically roll back Git/state; consult [BACKUP.md](BACKUP.md)
for safe manual recovery.

`./setup.sh --update-status` remains available as a **read-only local
revision** report; it no longer reads a watcher baseline and does not contact
GitHub. Optional public version metadata checks remain explicitly opt-in.
The retired Git watcher is not installed by the service installer. On hosts
where the old watcher was previously installed, disable both legacy
path/timer triggers before removing their managed unit files as described
in [docs/SYSTEMD.md](docs/SYSTEMD.md).

## On-demand eligibility preview (fourth slice)

Run `./setup.sh --on-demand-status` or choose Status → 4 (on-demand eligibility).
The existing `--status` and `--check` reports also show the disabled-by-default
`on_demand_preflight` using the **same** read-only Docker inventory as the
setup status check. It displays only sanitized readiness reasons and never
starts/stops containers, enables a supervisor, persists settings, or changes
firewall rules. `can_activate` remains false for this **read-only** report,
even if the optional runtime is already enabled. The separately reported
`runtime_activity` includes the live supervisor's state and idle deadlines.
The implemented opt-in mode defaults to a 30-minute idle timeout, remains off
until explicitly enabled, and requires verified dedicated ownership plus a
live root-owned firewall probe. Restart the bridge after changing settings;
shared or unverified backends cannot be enrolled.


## Guided production tunnel protection (#74)

For a dedicated running `local-search-crawler` project (exactly one
owner-verified Crawl4AI container and one SearXNG container), with the MCP
bridge listening locally, the wizard can install the privileged firewall
service and protected ChatGPT tunnel without manual `visudo` editing:

```sh
./setup.sh --secure-tunnel-preview
./setup.sh --secure-tunnel --confirm-secure-tunnel SECURE
```

Alternatively choose **Connections → 3** in the interactive wizard and type `SECURE`.
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
   `--preflight` commands) for the actual OS user. Both are installed;
   only later use of `--preflight` for on-demand backends is optional.
   `visudo -cf` validates both rules before and after installation.
5. Requires the fixed root-owned live private-denial and public DNS/TCP-443
   reachability probe to pass *without prompting*. This does not verify TLS.
6. Installs the fixed user tunnel `ExecStartPre=+` drop-in, reloads user
   systemd, and verifies the **effective** `ExecStartPre` command from
   `systemctl show` before enabling the tunnel and health timer. A later
   drop-in that clears, replaces or ignores the firewall probe makes the
   installer refuse to enable the tunnel and health timer, even when the
   guard still appears in `systemctl cat`. This validation runs at install
   time, not continuously: later unit changes require manual revalidation.

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
