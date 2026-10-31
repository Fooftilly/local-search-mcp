# Opt-in systemd user services and health monitoring

Initial implementation for issue #3. This does not manage Docker, SearXNG, Crawl4AI, or firewall rules. Your existing `./start.sh` foreground mode remains supported.

## Prepare

First, run `./start.sh --setup-only` interactively. It prepares the Python virtual environment, private `~/.config/local-search-mcp/config.json`, tunnel binary and profile. The `--service` mode never downloads, prompts, upgrades, or changes credentials. Do not put keys in unit files or command-line arguments.

## Version-controlled systemd unit templates

The four supported bridge/tunnel/health unit templates are in
`config/systemd/` as reviewable `.service` and `.timer` files. The installer
substitutes only local Python executable and checkout paths. The optional
guard probe units can be included explicitly. The old Git-update path/timer
templates were retired; no new installations can enable them. Runtime tokens,
credentials and history are never written into the unit files.

You can review or change the service definitions directly in Git before
installation. An install still writes managed copies into your XDG systemd
user directory; it refuses symlink or unmanaged destination files and does
not enable or start services. After an approved template change, rerun the
installer explicitly and `systemctl --user daemon-reload` as described below.
Missing or symlinked source templates fail rather than being silently generated.

## Preview and install (as your normal Linux user)

```sh
python3 scripts/install_user_services.py
python3 scripts/install_user_services.py --install
systemctl --user daemon-reload
systemctl --user enable --now local-search-mcp-bridge.service local-search-mcp-tunnel.service local-search-mcp-health.timer
```

Installer preview is read-only. The `--install` flag writes only the four marked user units in your XDG systemd directory and never enables or starts them. Unrelated existing units are not overwritten. Do not run this installer using `sudo`.

**Bridge startup readiness:** systemd `After=local-search-mcp-bridge.service` orders service launches but does not wait for the MCP socket on `127.0.0.1:8790` to listen. During an observed simultaneous start, tunnel-client v0.0.16 attempted MCP initialization and OAuth discovery before that socket was available. Both got `connection refused`; later MCP requests succeeded but tunnel `/readyz` retained HTTP 503. The managed `--service tunnel` launcher now defaults `MCP_STARTUP_WAIT_TIMEOUT=60s` (supported by the installed v0.0.16), preserving explicit inherited overrides including `0s`. The bridge, foreground launcher, domain approvals, guard and backend wake policy are unchanged.

This setting uses [tunnel-client v0.0.16's startup listener wait](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/configuration.md#startup-listener-wait-optional). For the HTTP-streamable main binding, the client retries **pre-connect** connection-refused/missing-socket failures before its first control-plane poll and OAuth discovery. Any HTTP response counts as listener reachability: it **does not prove MCP initialization or OAuth readiness**. If the 60-second wait expires, the client resumes polling for compatibility while `/readyz` can remain 503; the wait is neither a permanent fail-closed barrier nor a general runtime recovery mechanism. Launcher unit tests verify environment propagation only, not upstream timing or live readiness.

**Acceptance after separately authorized deployment:** source updates do not change the already-running tunnel process. On an attended, guard-verified simultaneous bridge/tunnel startup, verify that the bridge socket is reachable before the tunnel's initial discovery, then check `curl --noproxy '*' -sS -i http://127.0.0.1:8080/readyz` for HTTP 200, `/health?details=true` for non-failed OAuth/MCP startup state, and the bounded bridge/tunnel startup journal for connection failures. If readiness remains 503, preserve the failing evidence; investigate before declaring acceptance. No change to tunnel startup readiness alone proves a ChatGPT tool registry refresh: the 8-vs-12 discovery discrepancy remains tracked in [#215](https://github.com/Fooftilly/local-search-mcp/issues/215).

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

## Retiring the optional Git-change watcher

The supported update command is now **`./setup.sh --update`**. Do not
install or enable the old Git-change path/timer watcher. If it is currently
installed, the attended update disables both triggers before modifying Git.
Once it is safely disabled, the operator can remove the owned legacy unit
files using these existing commands:

```sh
systemctl --user disable --now local-search-mcp-update.path local-search-mcp-update.timer
python3 scripts/install_user_services.py --remove-auto-restart
systemctl --user daemon-reload
```

This does not stop Docker, delete policy/history/credentials or modify the
root-owned crawler egress guard. The legacy watcher script and source templates
are no longer shipped. Existing managed unit files must be disabled before
removal. There is no second supported update mechanism.

## Optional public bridge revision check (no automatic upgrades)

The manually requested command compares local Git HEAD against the public GitHub
master commit, without downloading source code:

```sh
python3 scripts/check_bridge_version.py --check-remote
```

It makes one bounded HTTPS request to a pinned public GitHub API endpoint,
bypasses proxies, rejects redirects and sends no runtime credentials or
configuration. It never runs Git fetch/pull, installs packages, changes the
checkout, writes update state, or restarts services.

A differing SHA **does not prove an update is available**: local commits,
divergence and force-pushes are possible. Inspect history, release notes,
dependencies and backups before any manual update. Rate limits, network
failures and malformed responses produce a safe unavailable result. The
command is not scheduled or enabled by the installer. Dependency, Crawl4AI
and tunnel-client update checks remain separate issue #15 work.

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
