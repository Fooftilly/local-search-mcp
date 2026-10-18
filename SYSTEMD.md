# Opt-in systemd user services and health monitoring

Initial implementation for issue #3. This does not manage Docker, SearXNG, Crawl4AI, or firewall rules. Your existing `./start.sh` foreground mode remains supported.

## Prepare

First, run `./start.sh --setup-only` interactively. It prepares the Python virtual environment, private `~/.config/local-search-mcp/config.json`, tunnel binary and profile. The `--service` mode never downloads, prompts, upgrades, or changes credentials. Do not put keys in unit files or command-line arguments.

## Preview and install (as your normal Linux user)

```sh
python3 scripts/install_user_services.py
python3 scripts/install_user_services.py --install
systemctl --user daemon-reload
systemctl --user enable --now local-search-mcp-bridge.service local-search-mcp-tunnel.service local-search-mcp-health.timer
```

Installer preview is read-only. The `--install` flag writes only the four marked user units in your XDG systemd directory and never enables or starts them. Unrelated existing units are not overwritten. Do not run this installer using `sudo`.

The bridge and tunnel have independent `Restart=on-failure` supervision with a 30-second delay, limited to **five starts in five minutes**. Persistent crashes enter a failed state instead of restarting indefinitely. After correcting the cause, run `systemctl --user reset-failed local-search-mcp-bridge.service local-search-mcp-tunnel.service`, then `systemctl --user start local-search-mcp-bridge.service local-search-mcp-tunnel.service`. The tunnel has a soft dependency on the bridge, so enabling it also requests bridge startup. SearXNG and Crawl4AI are not started or changed by these units.

## Health and troubleshooting

```sh
python3 health_cli.py --json
systemctl --user status local-search-mcp-bridge.service local-search-mcp-tunnel.service
systemctl --user list-timers 'local-search-mcp*'
journalctl --user -u local-search-mcp-bridge.service -u local-search-mcp-tunnel.service -n 100 --no-pager
journalctl --user -u local-search-mcp-health.service -n 40 --no-pager
```

The optional timer checks local services every approximately two minutes. It probes SearXNG's lightweight `/healthz` endpoint without issuing searches or consuming engine rate limits, Crawl4AI's 0.9.5+ health/version, the dashboard HTTP endpoint, the local MCP TCP listener, and the tunnel's local TCP listener. HTTP probes reject redirects and bypass environment HTTP proxies. It exits nonzero if a check fails; each subsequent timer tick retries. It does not expose secrets or searches in the health output.

**Limitations:** A listening port does not prove MCP tools function or the remote tunnel is actually registered with OpenAI. A healthy report does not prove crawler network isolation. Failures of SearXNG/Crawl4AI are reported as degradation but do not cause the bridge to modify the upstream services. Full end-to-end connection monitoring and firewall startup ordering remain follow-up work on issues #3 and #2.

## Optional automatic restart after local Git updates (no auto-upgrades)

The bridge and tunnel can restart after a **local Git revision change** caused by
your own `git pull`, checkout, reset, cherry-pick or merge. This feature never
runs `git fetch`, `git pull`, `pip install`, or downloads anything. GitHub
updates remain entirely manual. Changes to documentation, tests and CI alone
do not restart the services.

Install the optional watcher **after** the core user services have been set up:

```sh
# Preview the additional units (does not modify anything):
python3 scripts/install_user_services.py --auto-restart
# Install their unit files and initialize the current checkout as the baseline:
python3 scripts/install_user_services.py --install --auto-restart
systemctl --user daemon-reload
systemctl --user enable --now local-search-mcp-update.path local-search-mcp-update.timer
```

The `.path` unit watches Git's local HEAD reflog; a one-minute fallback timer
also checks the local commit in case a filesystem event is missed, including
while a Git operation is unfinished. The handler waits briefly for the
checkout to settle, declines to act while Git is merging/rebasing or tracked
runtime files have uncommitted changes, and syntax-checks changed Python
files before touching active services. It restarts the bridge first and
then the tunnel; neither inactive service is started. If the update contains
only non-runtime files, its revision is recorded without a restart.
It does **not** watch all working-tree edits; only committed local revision
changes are eligible.

If `requirements.txt` changed, was renamed, or was removed, the restart is
**deferred**. Review and prepare the dependencies yourself first (this manual
step *may* access the package index), then explicitly acknowledge the change:

```sh
# When requirements.txt still exists:
.venv/bin/python -m pip install -r requirements.txt
# For removed/renamed manifests, prepare the replacement dependencies manually.
# Then, in either case:
python3 scripts/restart_on_update.py --dependencies-ready
```

The acknowledgement applies the pending local revision once and will not
change packages or credentials itself. A removed manifest is accepted only
after that explicit acknowledgement and an offline `pip check` of the existing
virtual environment; a replacement manifest's dependency constraints must be
verified manually. Missing or invalid Python source
also blocks the automatic restart. Preflight is not a full integration
test, so check the health report after significant upgrades. If a restarted
service subsequently fails, the attempted revision is recorded to prevent
the timer from causing an endless restart loop. Fix the update or roll
back manually and restart the systemd services.

The state of the last handled revision is stored in the private
`~/.local/state/local-search-mcp/last-service-commit` file (or under
`XDG_STATE_HOME`). The installer preserves an existing baseline; it does
not overwrite it on reinstallation. This feature only handles local Git
checkouts with an enabled HEAD reflog and cannot detect source archives.

```sh
# Observe update activity:
systemctl --user status local-search-mcp-update.path local-search-mcp-update.timer
journalctl --user -u local-search-mcp-update.service -n 50 --no-pager

# Disable the option without stopping the bridge/tunnel:
systemctl --user disable --now local-search-mcp-update.path local-search-mcp-update.timer
python3 scripts/install_user_services.py --remove-auto-restart
systemctl --user daemon-reload
```

Updates to the unit generator or the service paths still require an explicit
rerun of the installer and `systemctl --user daemon-reload`. The feature
does not manage upstream SearXNG, Crawl4AI or Docker containers.

## Disable, remove and return to manual mode

```sh
systemctl --user disable --now local-search-mcp-update.path local-search-mcp-update.timer local-search-mcp-health.timer local-search-mcp-tunnel.service local-search-mcp-bridge.service
python3 scripts/install_user_services.py --remove
systemctl --user daemon-reload
./start.sh
```

Removal touches only the generated units. It leaves private config, credentials, history, domain approvals, Docker containers and your tunnel profile unchanged.

## Login and logout

Without lingering, the services generally run only while your user manager is active. To run them without a login session, **explicitly opt in** with `loginctl enable-linger "$USER"`; this repository never enables lingering automatically. Stop user services before running the manual `./start.sh` to avoid port conflicts. Regenerate user units if the repository is moved.
