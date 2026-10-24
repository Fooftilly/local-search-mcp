# Runtime configuration and secrets

This is the first independently reviewable slice of [roadmap issue #5](https://github.com/Fooftilly/local-search-mcp/issues/5). It retains existing environment variables, the private JSON file, the tunnel profile, and persisted domain approvals. Nothing in this PR automatically migrates or rewrites existing data.

## Inspect configuration sources safely

Run:

```sh
python3 bootstrap.py --show-config
```

The command does not start services, connect to Docker, inspect tunnel credentials, modify SQLite, or update the configuration file. It prints JSON indicating which setting source is available. Credentials are always shown as `<redacted>`; unknown or non-loopback upstream endpoint URLs are also redacted rather than revealing any URL-embedded credentials. Endpoint validation is diagnostic only in this initial slice, not a silent migration.

If the private JSON is malformed, symlinked, not owned by the current user, or accessible to group/other users, the command fails with an actionable error. The private directory must be an owner-owned, mode-0700 directory (not a symlink); the config must be an owner-owned, mode-0600 regular file. The launcher will not replace symlinked or non-regular paths. First inspect the actual locations using `ls -ld "${XDG_CONFIG_HOME:-$HOME/.config}/local-search-mcp"` and `ls -l "${XDG_CONFIG_HOME:-$HOME/.config}/local-search-mcp/config.json"`. **Only if** these are confirmed to be your own real directory and regular file (not symlinks), repair their permissions with `chmod 700 "${XDG_CONFIG_HOME:-$HOME/.config}/local-search-mcp"` and `chmod 600 "${XDG_CONFIG_HOME:-$HOME/.config}/local-search-mcp/config.json"`. Resolve unexpected symlinks, non-regular paths and ownership errors separately without running chmod on them; do not blindly overwrite them. Do not copy secrets into issues, logs or public chats.

## Which source takes precedence?

The read-only report describes candidate sources; it cannot guarantee the effective interactive-setup Crawl4AI token because it deliberately does not query Docker. Under systemd, the service uses saved JSON even when the shell has a different environment override.

| Setting | Interactive `./start.sh` / `--setup-only` | systemd `--service` | Runtime owner |
| --- | --- | --- | --- |
| Crawl4AI token | Docker discovery (if available), then process env, saved JSON, legacy `.env`/`.tunnel-env`, prompt | Saved private JSON | Bridge process credential |
| OpenAI tunnel runtime key | Process env, saved JSON, legacy env, prompt | Saved private JSON | Tunnel client credential |
| Initial allowed domains | `--domains`, process env, saved JSON, legacy env, interactive/default | Saved JSON is provided to bridge; SQLite rules are authoritative | SQLite after first initialization |
| Tunnel profile | Existing `local-research.yaml` is reused; missing profile is created through the tunnel tool | Existing profile is required, never rewritten | Tunnel client |
| Backend URLs / dashboard port | Runtime environment overrides with documented localhost defaults | Inherited process environment overrides where supported | Bridge/dashboard processes |

Legacy env files are parsed as literals, never executed as shell scripts. The launcher only seeds domain approvals from a configured list on **first** database initialization; subsequent changes in environment/JSON do not overwrite the local dashboard decisions.

`--show-config` intentionally does **not** inspect Docker to determine whether a Crawl4AI token would be discovered. It reports the source after excluding this discovery step and makes the limitation explicit. Domain rule values are not shown or modified; the report reads the domain-seed metadata flag through a normally locked, read-only SQLite connection. A rollback journal, WAL sidecar, a lock that blocks the read (such as an exclusive write lock), or an unreadable database produces an explicit uncertain status rather than a misleading answer. Setup seed-source diagnostics and systemd service seed-source diagnostics are reported separately.

## Diagnostics and compatibility

Configuration remains at `${XDG_CONFIG_HOME:-$HOME/.config}/local-search-mcp/config.json`, with owner-only permissions. Interactive setup atomically rewrites the saved JSON with its selected sources each run: Docker discovery can replace the saved Crawl4AI token, and process environment overrides saved credentials; `--refresh-runtime-key` and `--refresh-crawl-token` request replacements. The existing tool signatures `web_search` and `web_fetch` are unchanged.

The diagnostic checks that SearXNG and Crawl4AI endpoint overrides use parseable loopback HTTP URLs and that the dashboard port is within 1–65535. **These diagnostic warnings do not yet change the runtime network behavior.** When the dashboard port differs from its default, the health monitor currently probes 8791 and may report a false failure; this needs a separate coordinated configuration change.

### Still needed for issue #5

A full typed runtime settings layer, ownership/perms policy for all legacy and tunnel profile paths, conflict tests across independently launched services, and an operator-tested migration are follow-up slices. Do not close issue #5 based only on redacted inspection and private-JSON hardening.
