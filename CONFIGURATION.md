# Runtime configuration and secrets

This is the first independently reviewable slice of [roadmap issue #5](https://github.com/Fooftilly/local-search-mcp/issues/5). It retains existing environment variables, the private JSON file, the tunnel profile, and persisted domain approvals. Nothing in this PR automatically migrates or rewrites existing data.

## Inspect effective sources safely

Run:

```sh
python3 bootstrap.py --show-config
```

The command does not start services, connect to Docker, inspect tunnel credentials, modify SQLite, or update the configuration file. It prints JSON indicating which setting source is available. Credentials are always shown as `<redacted>`; unknown or non-loopback upstream endpoint URLs are also redacted rather than revealing any URL-embedded credentials. Endpoint validation is diagnostic only in this initial slice, not a silent migration.

If the private JSON is malformed, symlinked, not owned by the current user, or accessible to group/other users, the command fails with an actionable error. The private directory may not be a symlink. The launcher refuses to replace directory/non-regular config files. To restore normal ownership and permissions, inspect files manually and set `chmod 700 ~/.config/local-search-mcp` and `chmod 600 ~/.config/local-search-mcp/config.json` **only after confirming the correct owner and content**. Do not copy secrets into issues, logs or public chats.

## Which source takes precedence?

| Setting | Interactive `./start.sh` / `--setup-only` | systemd `--service` | Runtime owner |
| --- | --- | --- | --- |
| Crawl4AI token | Docker discovery (if available), then process env, saved JSON, legacy `.env`/`.tunnel-env`, prompt | Saved private JSON | Bridge process credential |
| OpenAI tunnel runtime key | Process env, saved JSON, legacy env, prompt | Saved private JSON | Tunnel client credential |
| Initial allowed domains | `--domains`, process env, saved JSON, legacy env, interactive/default | Saved JSON is provided to bridge; SQLite rules are authoritative | SQLite after first initialization |
| Tunnel profile | Existing `local-research.yaml` is reused; missing profile is created through the tunnel tool | Existing profile is required, never rewritten | Tunnel client |
| Backend URLs / dashboard port | Runtime environment overrides with documented localhost defaults | Inherited process environment overrides where supported | Bridge/dashboard processes |

Legacy env files are parsed as literals, never executed as shell scripts. The launcher only seeds domain approvals from a configured list on **first** database initialization; subsequent changes in environment/JSON do not overwrite the local dashboard decisions.

`--show-config` intentionally does **not** inspect Docker to determine whether a Crawl4AI token would be discovered. It reports the source after excluding this discovery step and makes the limitation explicit. Similarly, domain rules are not read or mutated: the report distinguishes an existing SQLite database from an uninitialized one.

## Diagnostics and compatibility

Configuration remains at `~/.config/local-search-mcp/config.json`, with owner-only permissions. Existing tokens are retained unless an operator explicitly refreshes them; the launcher continues saving data atomically. The existing tool signatures `web_search` and `web_fetch` are unchanged.

The diagnostic checks that SearXNG and Crawl4AI endpoint overrides use parseable loopback HTTP URLs and that the dashboard port is within 1–65535. **These diagnostic warnings do not yet change the runtime network behavior.** When the dashboard port differs from its default, the health monitor currently probes 8791 and may report a false failure; this needs a separate coordinated configuration change.

### Still needed for issue #5

A full typed runtime settings layer, ownership/perms policy for all legacy and tunnel profile paths, conflict tests across independently launched services, and an operator-tested migration are follow-up slices. Do not close issue #5 based only on redacted inspection and private-JSON hardening.
