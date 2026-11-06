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

The first command checks only the existing fixed-loopback crawler health endpoint. The second command opt-in submits exactly two hardcoded, **non-LLM, no-regex, no-JS** typed configuration objects to the authenticated `http://127.0.0.1:11235/config/dump` endpoint (one CSS, one XPath). It does not make any request to a public URL, does not start containers or grant domains, does not modify persisted server configuration and does not execute selectors against fetched content. It refuses unsupported/unknown crawler versions according to the existing health verifier; if the crawler is sleeping, it does not automatically wake it.

Only fixed outcome labels and the installed version are printed; never paste API tokens, raw response bodies, HTTP headers or unredacted private URLs in reports. Token credentials are read from the existing environment and used only to contact the fixed loopback service. There are no redirects, proxy environment inheritance or external network destinations. Responses are capped at 128 KiB and untrusted raw data is not printed.

**Success requires** `health=supported_and_healthy`, `config_status=both_config_echoes_verified`, and an exit code of 0. Treat `config_echo_not_verified` or `config_probe_unavailable` as inconclusive, **not** as proof that upstream extraction is absent. If absent, first verify whether the installed image/API matches stable v0.9.5; do not disable token checks, untrusted deserialization protections or crawler egress rules.

## Next acceptance gate (not implemented)

Before exposing schema extraction through an MCP API:

1. Confirm that the installed backend returns bounded `extracted_content` for a **fixed, reviewed schema** on three *individually approved* public examples covering tables, cards/lists and JSON-LD. Compare quality, missing values, latency and CPU/memory with current `web_fetch(include_tables=true)`, links, JSON-LD and Markdown baselines. Never infer precision from a successfully echoed schema.
2. Define and test a typed caller-facing validator: selector grammar/depth/length caps, limited field types and safe attributes, bounded objects/strings/total serialized JSON, rejection of regex and transformations, and safe errors for malformed/expensive CSS and XPath. Input must be validated **before** the guarded request.
3. Route the eventual optional schema through the **existing** `web_fetch` source and final URL checks, operator approval/one-use proof, DNS/redirect/SSRF egress, policy epoch and revocation checks, lease ownership and cache. Never pass arbitrary user-supplied `CrawlerRunConfig`, code, credentials or URLs to the backend.
4. Cover source HTML with XSS-like text, redirects, revoked grants, hostile selectors, oversized results, stale cache and concurrent requests. An accepted schema alone is not a safe production extraction feature.
5. Maintain unchanged public defaults and the 12-tool API until a separately reviewed feature design warrants a deliberate extension.

This work belongs to [issue #300](https://github.com/Fooftilly/local-search-mcp/issues/300) under [roadmap #297](https://github.com/Fooftilly/local-search-mcp/issues/297). Merged #305 and #306 concern sitemap discovery and offline XML parsing, not structured HTML extraction.
