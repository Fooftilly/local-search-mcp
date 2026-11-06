# Declarative structured extraction — installed-server compatibility (#300)

This is a **read-only feasibility preflight**, not a production extraction tool. It does not modify the current 12 MCP tools, crawler container, settings, domain rules, firewall, approvals, SearXNG engines or data stores.

## Evidence from upstream stable v0.9.5 source

- The upstream [Docker server](https://github.com/unclecode/crawl4ai/blob/v0.9.5/deploy/docker/server.py) exposes an authenticated `POST /config/dump` serialization endpoint. It accepts a typed `CrawlerRunConfig` and uses the `Provenance.UNTRUSTED` deserialize path. The same server implements `POST /crawl`, but this preflight **never calls it**.
- [Crawl4AI v0.9.5 configuration rules](https://github.com/unclecode/crawl4ai/blob/v0.9.5/crawl4ai/async_configs.py) include both `JsonCssExtractionStrategy` and `JsonXPathExtractionStrategy` in the allowed untrusted strategy types. Dangerous fields, including arbitrary JavaScript, proxies, deep crawls and regex schema transforms, are separately prohibited. This is upstream source evidence, **not** a guarantee that the user's installed image matches or that requests cannot cause unsafe behavior under other configurations.
- The [existing Python crawler execution path](https://github.com/unclecode/crawl4ai/blob/v0.9.5/crawl4ai/async_webcrawler.py) can run the configured extraction strategy on HTML and serialize `extracted_content`. The bridge currently does not configure or consume that result; `web_fetch` still uses authorized Markdown extraction, links, tables and JSON-LD.
- An authenticated local config echo is only evidence of **typed schema deserialize/serialize compatibility**. It is **not** evidence that selectors run, that `extracted_content` is returned and bounded, or that network-fetched HTML is safe.

## Operator preflight

From the **installed and configured** repository checkout on the operator's Arch Linux host, with the *existing* `CRAWL4AI_API_TOKEN` in the environment:

```bash
./.venv/bin/python -m scripts.structured_extraction_preflight
./.venv/bin/python -m scripts.structured_extraction_preflight --probe-configs
```

### On-demand crawler: make the backend available first

A crawler that is **verified sleeping** will return `health=unavailable` in **both** commands above, including `--probe-configs`. No schema probe or token authentication is attempted in that case. This is **inconclusive**, not a CSS/XPath compatibility failure.

On the installed main checkout, first run the project's existing **read-only** operator diagnostics:

```bash
cd ~/local-search-mcp
./setup.sh --on-demand-status
./setup.sh --docker-status
```

If the verified managed `crawl4ai` backend is `sleeping`, wake it through the **existing authorized MCP tool**, not `docker start`, direct /crawl calls, a new scraper, or weakened firewall rules. For example, replace this URL with an **already currently approved** public page and invoke the bridge at its existing local endpoint:

```bash
~/local-search-mcp/.venv/bin/fastmcp call \
  http://127.0.0.1:8790/mcp web_fetch \
  url='https://www.peroniko.xyz/' fresh=true --auth none
```

This is a **real, operator-directed public fetch** with existing approval/egress checks, unlike the preflight. The example host is not preapproved automatically; check current authorization before running. `fresh=true` bypasses process fetch caching. If the bridge reports denied/unavailable or the supervisor reports failed/unverified, diagnose that status first; do not manually bypass the guard.

Immediately after a successful, authorized wake, rerun the preflight in the separate PR worktree. For the authenticated config echo, load the saved token through the existing private configuration reader **without printing it to the terminal**:

```bash
cd ~/local-search-mcp-pr307
CRAWL4AI_API_TOKEN="$(cd ~/local-search-mcp && \
  ./.venv/bin/python -c 'import sys; from bootstrap import read_config; sys.stdout.write(read_config().get("CRAWL4AI_API_TOKEN", ""))')" \
  ~/local-search-mcp/.venv/bin/python -m scripts.structured_extraction_preflight --probe-configs
```

The token is passed only to that process; it is not stored in the PR worktree or printed by the probe. The user-owned config reader rejects insecure private configuration permissions and symlinks. If the result is `operator_token_required`, inspect the redacted source information with `python3 bootstrap.py --show-config` from the installed checkout; **do not paste credentials**.

If `health=unavailable` persists, share the sanitized `--on-demand-status` and `--docker-status` results plus the preflight result, not tokens, raw HTTP headers, or Docker environment dumps. The crawler may have returned to sleep between commands; the preflight itself intentionally does not acquire a wake lease.

The first command checks only the existing fixed-loopback crawler health endpoint. The second command opt-in submits exactly two hardcoded, **non-LLM, no-regex, no-JS** typed configuration objects to the authenticated `http://127.0.0.1:11235/config/dump` endpoint (one CSS, one XPath). It does not make any request to a public URL, does not start containers or grant domains, does not modify persisted server configuration and does not execute selectors against fetched content. It refuses unsupported/unknown crawler versions according to the existing health verifier; if the crawler is sleeping, it does not automatically wake it.

Only fixed outcome labels and the installed version are printed; never paste API tokens, raw response bodies, HTTP headers or unredacted private URLs in reports. Token credentials are read from the existing environment and used only to contact the fixed loopback service. There are no redirects, proxy environment inheritance or external network destinations. Responses are capped at 128 KiB and untrusted raw data is not printed.

**Observed installed-host result (2026-11-06):** the operator verified the managed crawler was sleeping, then successfully used the existing authorized `web_fetch(fresh=true)` for the previously approved Peroniko homepage. The probe then reported `health=supported_and_healthy`, `installed_version=0.9.5`, and **`css=config_echo_not_verified` / `xpath=config_echo_not_verified`**. This proves the probe reached the schema comparison; it did **not** prove whether the backend rejected the strategy or the checker failed to recognize the response. Upstream v0.9.5 `to_serializable_dict()` wraps all plain nested dictionaries as `{"type":"dict","value":{...}}`, including the top-level schema and each field spec. The original preflight compared those against raw dictionaries, a false-negative validation bug. This PR now matches the *exact fixed schema* in either documented wrapped or legacy plain form and rejects added fields, wrong classes, altered selectors, or malformed responses. The user must rerun the updated probe to confirm the installed image.

**Success requires** `health=supported_and_healthy`, `config_status=both_config_echoes_verified`, and an exit code of 0. Treat `config_echo_not_verified` or `config_probe_unavailable` as inconclusive, **not** as proof that upstream extraction is absent. If absent, first verify whether the installed image/API matches stable v0.9.5; do not disable token checks, untrusted deserialization protections or crawler egress rules.

## Next acceptance gate (not implemented)

Before exposing schema extraction through an MCP API:

1. Confirm that the installed backend returns bounded `extracted_content` for a **fixed, reviewed schema** on three *individually approved* public examples covering tables, cards/lists and JSON-LD. Compare quality, missing values, latency and CPU/memory with current `web_fetch(include_tables=true)`, links, JSON-LD and Markdown baselines. Never infer precision from a successfully echoed schema.
2. Define and test a typed caller-facing validator: selector grammar/depth/length caps, limited field types and safe attributes, bounded objects/strings/total serialized JSON, rejection of regex and transformations, and safe errors for malformed/expensive CSS and XPath. Input must be validated **before** the guarded request.
3. Route the eventual optional schema through the **existing** `web_fetch` source and final URL checks, operator approval/one-use proof, DNS/redirect/SSRF egress, policy epoch and revocation checks, lease ownership and cache. Never pass arbitrary user-supplied `CrawlerRunConfig`, code, credentials or URLs to the backend.
4. Cover source HTML with XSS-like text, redirects, revoked grants, hostile selectors, oversized results, stale cache and concurrent requests. An accepted schema alone is not a safe production extraction feature.
5. Maintain unchanged public defaults and the 12-tool API until a separately reviewed feature design warrants a deliberate extension.

This work belongs to [issue #300](https://github.com/Fooftilly/local-search-mcp/issues/300) under [roadmap #297](https://github.com/Fooftilly/local-search-mcp/issues/297). Merged #305 and #306 concern sitemap discovery and offline XML parsing, not structured HTML extraction.
