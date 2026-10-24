# local-search-mcp

A **local, read-only MCP bridge** for [SearXNG](https://docs.searxng.org/) search and [Crawl4AI](https://docs.crawl4ai.com/) page extraction, with a local domain-approval dashboard and private request history. Connect it to ChatGPT using an OpenAI Secure MCP Tunnel (where available). Linux-first; tested locally with Python, SearXNG and Crawl4AI 0.9.4; 0.9.5+ is now required for remote crawling. **Experimental software**, not a network firewall.

For organized operator documentation, see the [documentation index](docs/README.md).\n\n## What it provides

- MCP `web_search(query, max_results=8)` — SearXNG-backed public web search, with optional language, category, time range, engine and page filters, plus opt-in URL deduplication and deterministic source-count ranking. See [SEARCH.md](docs/SEARCH.md).
- MCP `web_fetch(url, mode="raw")` — Crawl4AI-backed extraction of an approved public page, with optional fit Markdown and short metadata. Long approved pages expose `web_fetch_page(document_id, page=2)` for bounded in-memory continuation without recrawling. See [EXTRACTION.md](docs/EXTRACTION.md). Unknown domains enter the dashboard approval queue. Agents cannot approve domains.
- MCP `web_fetch_batch(urls, mode="raw", fresh=false)` — fetch up to eight public URLs in one call, with three concurrent workers, per-URL permission checks and ordered individual outcomes. See [BATCH.md](docs/BATCH.md).
- Local approvals, deny rules, domain history and search/fetch audit trail.
- Per-rule exact-host vs subdomain scope and optional 1-hour/24-hour/7-day expiry in the local dashboard. See [PERMISSIONS.md](docs/PERMISSIONS.md).
- Same-origin dashboard live pending/history refresh every ten seconds, with optional browser-powered KDE desktop notifications for new domain requests. See [DASHBOARD-LIVE.md](docs/DASHBOARD-LIVE.md).
- Process-only, bounded search and public-page response cache with in-flight request deduplication; two-attempt upstream retry bounds and explicit structured error responses. Add `fresh=true` to bypass.
- A launcher which checks services, sets up a Python venv, discovers a local Crawl4AI token where permitted, and starts the bridge plus tunnel.

## Requirements

- Linux with Python 3.11+, **libpsl** (Arch: `sudo pacman -S libpsl`) and `python-venv` support.
- SearXNG running at `http://127.0.0.1:8888` with JSON search enabled.
- **Crawl4AI Docker server 0.9.5 or newer (stable)** at `http://127.0.0.1:11235`, with a locally configured API token and normal hardened settings. This bridge rejects older/unknown versions, but **does not itself enforce network isolation**: read [SECURITY.md](SECURITY.md).
- Optional: OpenAI Secure MCP Tunnel and the account permissions to create and use a tunnel.

## Start

**Upgrade note (October 2026):** Crawl4AI 0.9.4 contains a cache-freshness SSRF bypass corrected in 0.9.5. This bridge now blocks `web_fetch` and launcher startup against 0.9.4 and prerelease builds. Upgrade the Crawl4AI Docker image to a verified 0.9.5+ build, preserve your container's private configuration/volumes and confirm `/health` reports the updated version. If a suitable image is not yet available for your platform, leave remote fetching disabled; do not weaken the gate.

```bash
./start.sh
```

The first run provisions a virtual environment and prompts securely for secrets it cannot retrieve. The launcher stores configuration in `~/.config/local-search-mcp/config.json` (mode `0600`). It reuses `~/.config/tunnel-client/local-research.yaml` if present and downloads the verified official Linux x86_64 `tunnel-client` release if the binary is missing. It does **not** install or reconfigure SearXNG/Crawl4AI itself.

For read-only configuration-source diagnostics with credentials redacted, run `python3 bootstrap.py --show-config`. For the source-precedence rules, hardening requirements and migration notes, see [CONFIGURATION.md](docs/CONFIGURATION.md). This report does not query Docker, print private tokens or modify domain approvals.

For a non-destructive shutdown of verified local-search-mcp-managed user
services and private Compose backends, run `./stop.sh`. It disables automatic
restart of those existing containers but leaves credentials, history, volumes,
and firewall intact. The external Hermes Web Compose deployment is never
stopped automatically. See [STOP.md](docs/STOP.md) for ownership checks and the
optional root-owned firewall-service procedure.

For the ChatGPT-first interactive setup and maintenance menu, run
`./setup.sh`. For safe read-only diagnostics, use `./setup.sh --check` or
`./setup.sh --status`; `./setup.sh --help` lists all currently implemented
features and mutation boundaries. See [SETUP-WIZARD.md](SETUP-WIZARD.md).
Existing `./start.sh` and `./stop.sh` still work independently.

For setup without starting processes:

```bash
./start.sh --setup-only
```

For a **separate, opt-in** Crawl4AI Docker Compose scaffold with explicit
public DNS, loopback-only port binding and a dedicated bridge network:

```bash
./start.sh --setup-only --generate-crawler-compose
```

This creates private files only; it does not start Docker, replace the current
Crawl4AI container or modify an existing Hermes Web Compose installation.
Review required volumes, networking dependencies and firewall activation
before migrating. See [CRAWLER-COMPOSE.md](docs/CRAWLER-COMPOSE.md).


If you do not have OpenAI tunnel access, the bridge can run directly:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export CRAWL4AI_API_TOKEN='...'  # supply through private environment, not Git
.venv/bin/python bridge.py
```

Open the dashboard at **http://127.0.0.1:8791/**. The MCP endpoint is **http://127.0.0.1:8790/mcp**. Both bind to loopback only.

Use a Streamable HTTP MCP client to inspect and test `web_search` and `web_fetch`. Example with the FastMCP CLI installed in your environment:

```bash
fastmcp list http://127.0.0.1:8790/mcp --auth none
fastmcp call http://127.0.0.1:8790/mcp web_search query='Arch Linux news' max_results=3 --auth none
```

For optional search filters, page limits, SearXNG engine support, and examples, see [SEARCH.md](docs/SEARCH.md).

For long-document paging, temporary handle limits, extraction modes, and content safety, see [EXTRACTION.md](docs/EXTRACTION.md).

For bounded memory caching, `fresh` bypass, safe cache keys, retry caps and outage responses, see [CACHE.md](docs/CACHE.md).

For live dashboard polling and opt-in desktop notifications on KDE/Wayland, see [DASHBOARD-LIVE.md](docs/DASHBOARD-LIVE.md).

For batch limits, result format, duplicate URL handling, and per-item audit history, see [BATCH.md](docs/BATCH.md).

## Domains and approvals

The local dashboard is the **only** supported interface for allowing/denying domains. The initial list comes from `ALLOWED_CRAWL_DOMAINS`; after initialization, SQLite is authoritative. Approved parent domains also permit subdomains; the application blocks public suffixes such as `co.uk` and privately hosted suffixes such as `github.io`. Unsafe legacy rules are removed automatically on first launch with the new code. Use a narrow hostname to approve one hosted site. **Initial allowed-domain grants and explicit dashboard decisions are independent rules:** automatically queued child requests never override a later parent allow/deny. An explicit child exception still overrides its parent. On upgrade, obsolete inherited child rows are reclassified or removed; previously saved explicit dashboard decisions remain in effect.

To protect the approval dashboard from agent-generated hostname floods, automatic pending requests are bounded to **512 total**, **128 newly queued hosts globally per rolling hour**, **32 per registrable site** and **8 new hosts per site per rolling hour**. Private public-suffix boundaries (such as separate `github.io` tenants) are respected. Repeated requests for an already pending hostname update its statistics without taking a new slot. If a limit is reached, `web_fetch` responds with `approval_queue_full` and **does not queue** the hostname; it remains blocked until you approve it manually or **retry the request after queue capacity becomes available** (retrying can enqueue it for review, not automatically approve it). Hourly admission counts persist even after a queued hostname is allowed, denied, or removed. Explicit allow/deny rules are unaffected, and existing pending entries are never silently removed. The dashboard lists pending requests separately from allowed and denied rules, preserving rule visibility during a flood. You can filter either list to find individual hosts, or use the local dashboard's confirmation-protected **Clear all pending** action to recover from a flood without deleting allow/deny rules. Clearing pending entries does not reset hourly admission limits.

The dashboard stores permissions in `~/.local/state/local-search-mcp/domains.sqlite3`. History is stored in `~/.local/state/local-search-mcp/history.jsonl`, rotated at 8 MiB by default with three backups; configure the per-file limit via `LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES`. Rotate/archive or delete old history as needed. URL query strings, page contents and credentials are not logged; **search terms can be sensitive**.

The local history dashboard now supports filtered, private indexed SQLite metadata and 30-day default retention. See [HISTORY-INDEX.md](docs/HISTORY-INDEX.md). The original JSONL files remain unchanged. CLI text escapes terminal and bidirectional control characters; CSV export escapes terminal/format controls and prefixes spreadsheet formula-like cells with a literal apostrophe (these cells do not round-trip byte-for-byte), and JSON export escapes Unicode control characters.

MCP `web_metrics(hours=24)` returns aggregate 1/24/168-hour operation outcomes and process-lifetime cache counters without exposing queries, URLs or document content. Details: [HISTORY-INDEX.md](docs/HISTORY-INDEX.md).

History commands:

```bash
python history_cli.py --last 50
python history_cli.py --stats
python history_cli.py --csv --last 5000 > ~/search-history.csv
```

## Optional private state backup and staged restore

For an offline, integrity-checked backup of domain approvals and JSONL history, use `python3 scripts/backup_state.py backup ARCHIVE.zip`. Verify with `python3 scripts/backup_state.py verify ARCHIVE.zip` and stage recovery into a **new directory only** with `python3 scripts/backup_state.py restore ARCHIVE.zip --target-dir DIRECTORY`. Archives are private, may contain sensitive history, exclude credentials, and never overwrite live state. Stop services before backup for coordinated policy/history snapshots. See [BACKUP.md](BACKUP.md) for safety and restoration limitations. Live replacement and rollback are not automated.

## Optional Docker egress guard (Linux)

For operators using a dedicated Crawl4AI Docker bridge, see [EGRESS-ISOLATION.md](docs/EGRESS-ISOLATION.md) for an **opt-in nftables containment tool**, an example workflow, and live negative/positive probes. This host firewall is **not installed by the launcher**, does not replace Crawl4AI's own protections, and must be re-applied after reboot or Docker network changes. It does not isolate loopback inside the crawler container.

## Optional systemd user services

For independently supervised bridge/tunnel processes and periodic health diagnostics, see [SYSTEMD.md](docs/SYSTEMD.md). These are opt-in and not installed by `./start.sh`; run `./start.sh --setup-only` before installing. The foreground launcher remains available.

Optional offline auto-restart after **locally performed** `git pull`/checkout/merge is available with `python3 scripts/install_user_services.py --install --auto-restart`. Enable the installed `local-search-mcp-update.path` and `local-search-mcp-update.timer` user units as described in [SYSTEMD.md](docs/SYSTEMD.md). No Git fetching, downloads or dependency installations occur automatically; runtime code changes restart active services only, documentation-only changes do not, and changed Python requirements require explicit manual preparation.

## Connecting through OpenAI tunnel

Read `./tunnel-client help quickstart` and configure a profile referencing `http://127.0.0.1:8790/mcp`. Store runtime key outside Git. See the [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels). The tunnel does not expose the dashboard or native Crawl4AI endpoints.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

GitHub Actions runs the Python tests. See [SECURITY.md](SECURITY.md) for the deployment threat model and [CONTRIBUTING.md](CONTRIBUTING.md) for contributions. The project does not claim ownership of the upstream SearXNG, Crawl4AI or tunnel-client code; they are downloaded/run separately.

Licensed under Apache-2.0. See [LICENSE](LICENSE).
