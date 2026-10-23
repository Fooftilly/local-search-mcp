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

For explicit manual source updates, consult SYSTEMD.md, BACKUP.md and
DEPENDENCY-UPDATES.md. The existing optional `git pull`-triggered
restart watcher remains unchanged.
