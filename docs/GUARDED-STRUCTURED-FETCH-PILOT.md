# Guarded opt-in structured fetch pilot (#300)

This draft implementation routes declarative CSS/XPath extraction through the **existing authorized `web_fetch` request**. It adds the optional parameter `structured_spec` to that tool, not a thirteenth tool. It does not change existing fetch defaults or batch behavior.

**Not deployed or accepted on the installed host yet.** The added `structured_spec` option is **operator-disabled by default**: the running bridge must explicitly set `LOCAL_SEARCH_STRUCTURED_EXTRACT_PILOT=1` before accepting it. Do not enable it in production until the open server-side selector CPU/time and host-safety gates below have been reviewed. This is a gate, **not** a substitute for upstream runtime isolation. Do not use this draft as evidence of end-to-end security or extraction fidelity before CI, review, and attended host tests.

## Example specification (after installation and existing site approval)

Use an approved public HTML page containing a heading, and select `raw` mode:

```bash
./.venv/bin/fastmcp call http://127.0.0.1:8790/mcp web_fetch \
  url='https://www.peroniko.xyz/' fresh=true \
  structured_spec='{"strategy":"css","base_selector":"body","fields":[{"name":"heading","selector":"h1","type":"text"}]}' \
  --auth none
```

This example is **not an authorization grant**. Run it only after confirming that the site is currently approved, the crawler is verified, and the existing egress guard is active. The new parameter is understood only by a bridge running the version with this PR merged/deployed; it has no effect on an older installed bridge. A `fastmcp` client may require quoting/JSON option adjustments depending on its version; never paste tokens into CLI arguments.

Equivalent XPath schema:

```
{"strategy":"xpath","base_selector":"//body","fields":[{"name":"heading","selector":".//h1","type":"text"}]}
```

The opt-in is deliberately bounded by `structured_schema.validate_spec()`: 1–8 fields, up to four simple selector steps of at most 96 characters, `text` or one of four benign attributes. Unsupported selectors and `mode=fit` raise a validation error **before domain authorization or network I/O**. No arbitrary Crawl4AI config, JS, regex, transforms, computed fields, functions or LLM settings are forwarded.

## Request and response security

The request uses the existing `web_fetch` policy checks, guarded `on_demand.lease("crawl4ai")`, version-verified local `/crawl` endpoint, source/final URL and redirect checks, one-use grant proof, policy epoch recheck, domain-expiry rules, and Docker egress guard. No independent downloader is added. The only opt-in addition to `crawler_config.params` is an application-owned typed `extraction_strategy` plus a **20-second browser page timeout**. It does not claim to bound server-side selector-evaluation CPU after navigation.

Opt-in crawls request **identity-only HTTP encoding** and reject encoded responses, stream raw (not transparently decompressed) HTTP bytes with a **4 MiB cap** before JSON deserialization, and apply a 90-second wall-clock response deadline checked during streaming (with at most one further 30-second blocking read). HTTP transport failures return a sanitized `upstream_unavailable` status; malformed or unsupported compressed bodies return `invalid_response`. They never retry an ambiguous POST failure. The parsed extraction has a 64 KiB input cap, at most 20 records, 240 characters per cell, and 12 KiB maximum serialized extraction envelope. Missing, malformed, oversized or extra-field `extracted_content` returns a sanitized `invalid_extraction` status with **no page content or metadata**. Valid structured records may be returned even when the same successful crawl's Markdown is blank, but an empty list and blank Markdown remain an `empty_extraction` failure. Ordinary Markdown-only calls keep their existing request path and caching behavior.

On success the response includes `structured_extraction` with `records`, `rows_observed_in_response`, `rows_returned`, `truncated`, `extraction_method`, `site_completeness=unknown` and `authorization_verified=false`. The last flag belongs to the pure parser; the surrounding bridge performs the actual domain/one-use permission checks before returning the response. The full `web_fetch` envelope has additional fields and is not limited to 12 KiB in total. All returned page text remains **untrusted source content**, not verified facts or executable instructions.

The opt-in bypasses the reusable default Markdown-only cache to prevent schema/result mismatch or replay under changed authorization. It does not persist structured extraction results. No source-discovered URL or attribute value is independently fetched.

## Acceptance gates still open

- [ ] CI and review findings clean on the exact PR head, including no changes to the 12-tool count.
- [ ] Installed Crawl4AI v0.9.5 backend actually returns `extracted_content` for the fixed reviewed CSS and XPath examples, not just a successful `/config/dump` echo. Tests should be run only on **already approved** public pages.
- [ ] Three distinct approved layouts (table, cards/list, JSON-LD) compared against existing `web_fetch(include_tables=true)`, links, Markdown, and `include_jsonld=true`. Record matched field completeness, false positives, latency, memory/CPU use, and truncation.
- [ ] Host-level negative tests including redirect to unapproved host, DNS pin/rebind/SSRF, policy revocation during in-flight extraction, once-grant expiry, oversize upstream HTTP, malformed or missing `extracted_content`, high concurrency, cancellation and selector CPU/time behavior. Operator must verify live egress guard, not infer isolation from HTTP 200.
- [ ] Define and enforce an actual server-side selector evaluation/CPU budget before regarding this as production-safe. HTTP page timeout and output byte bounds **do not** guarantee selector runtime limits.

This is a pilot continuation of [#300](https://github.com/Fooftilly/local-search-mcp/issues/300) under [#297](https://github.com/Fooftilly/local-search-mcp/issues/297). Keep the issue open until full host acceptance. The preexisting offline parser is in merged #308 and the installed config echo preflight is merged #307.
