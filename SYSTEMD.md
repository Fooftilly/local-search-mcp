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

## Disable, remove and return to manual mode

```sh
systemctl --user disable --now local-search-mcp-health.timer local-search-mcp-tunnel.service local-search-mcp-bridge.service
python3 scripts/install_user_services.py --remove
systemctl --user daemon-reload
./start.sh
```

Removal touches only the generated units. It leaves private config, credentials, history, domain approvals, Docker containers and your tunnel profile unchanged.

## Login and logout

Without lingering, the services generally run only while your user manager is active. To run them without a login session, **explicitly opt in** with `loginctl enable-linger "$USER"`; this repository never enables lingering automatically. Stop user services before running the manual `./start.sh` to avoid port conflicts. Regenerate user units if the repository is moved.
