# local-search-mcp

A **local, read-only MCP bridge** for [SearXNG](https://docs.searxng.org/) search and [Crawl4AI](https://docs.crawl4ai.com/) page extraction, with a local domain-approval dashboard and private request history. Connect it to ChatGPT using an OpenAI Secure MCP Tunnel (where available). Linux-first; tested locally with Python, SearXNG and Crawl4AI 0.9.4; 0.9.5+ is now required for remote crawling. **Experimental software**, not a network firewall.

## What it provides

- MCP `web_search(query, max_results=8)` — SearXNG-backed public web search.
- MCP `web_fetch(url)` — Crawl4AI-backed extraction of an approved public page. Unknown domains enter the dashboard approval queue. Agents cannot approve domains.
- Local approvals, deny rules, domain history and search/fetch audit trail.
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

For a read-only report of effective setting sources with credentials redacted, run `python3 bootstrap.py --show-config`. For the source-precedence rules, hardening requirements and migration notes, see [CONFIGURATION.md](CONFIGURATION.md). This report does not query Docker, print private tokens or modify domain approvals.

For setup without starting processes:

```bash
./start.sh --setup-only
```

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

## Domains and approvals

The local dashboard is the **only** supported interface for allowing/denying domains. The initial list comes from `ALLOWED_CRAWL_DOMAINS`; after initialization, SQLite is authoritative. Approved parent domains also permit subdomains; the application blocks public suffixes such as `co.uk` and privately hosted suffixes such as `github.io`. Unsafe legacy rules are removed automatically on first launch with the new code. Use a narrow hostname to approve one hosted site.

The dashboard stores permissions in `~/.local/state/local-search-mcp/domains.sqlite3`. History is stored in `~/.local/state/local-search-mcp/history.jsonl`, rotated at 8 MiB by default with three backups; configure the per-file limit via `LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES`. Rotate/archive or delete old history as needed. URL query strings, page contents and credentials are not logged; **search terms can be sensitive**.

History commands:

```bash
python history_cli.py --last 50
python history_cli.py --stats
python history_cli.py --csv --last 5000 > ~/search-history.csv
```

## Optional Docker egress guard (Linux)

For operators using a dedicated Crawl4AI Docker bridge, see [EGRESS-ISOLATION.md](EGRESS-ISOLATION.md) for an **opt-in nftables containment tool**, an example workflow, and live negative/positive probes. This host firewall is **not installed by the launcher**, does not replace Crawl4AI's own protections, and must be re-applied after reboot or Docker network changes. It does not isolate loopback inside the crawler container.

## Optional systemd user services

For independently supervised bridge/tunnel processes and periodic health diagnostics, see [SYSTEMD.md](SYSTEMD.md). These are opt-in and not installed by `./start.sh`; run `./start.sh --setup-only` before installing. The foreground launcher remains available.

## Connecting through OpenAI tunnel

Read `./tunnel-client help quickstart` and configure a profile referencing `http://127.0.0.1:8790/mcp`. Store runtime key outside Git. See the [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels). The tunnel does not expose the dashboard or native Crawl4AI endpoints.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q .
```

GitHub Actions runs the Python tests. See [SECURITY.md](SECURITY.md) for the deployment threat model and [CONTRIBUTING.md](CONTRIBUTING.md) for contributions. The project does not claim ownership of the upstream SearXNG, Crawl4AI or tunnel-client code; they are downloaded/run separately.

Licensed under Apache-2.0. See [LICENSE](LICENSE).
