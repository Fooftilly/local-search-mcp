# Bounded declarative structured-extraction schema (#300)

This is an **offline validation and result-normalization component**, not an MCP tool, runtime crawler integration, or permission system. It builds fixed upstream Crawl4AI configurations only after validating every field and selector, then checks a separately obtained `extracted_content` JSON string before any caller could receive it. **Do not expose its returned records until the bridge independently rechecks all existing live authorization rules.**

## Operator-proven capability

Merged [PR #307](https://github.com/Fooftilly/local-search-mcp/pull/307) established that the installed Crawl4AI Docker v0.9.5 server, while running, accepts fixed `JsonCssExtractionStrategy` and `JsonXPathExtractionStrategy` typed configurations via authenticated loopback `POST /config/dump`. That confirms config deserialization/serialization, **not** browser execution, response availability, extraction correctness, or safe runtime bounds.

This follow-up module `structured_schema.py` introduces a **strict application-owned subset**; it does not accept arbitrary upstream CrawlerRunConfig JSON or arbitrary CSS/XPath.

## Scope and limits

`validate_spec()` accepts exactly three top-level properties: `strategy` (`css` or `xpath`), `base_selector`, and `fields` (1–8). Each field has a unique ASCII-safe `name`, a limited `selector` and a `type` of `text` or `attribute`. Attribute fields must name one of `title`, `alt`, `aria-label` or `datetime`. No user-supplied regex, code, scripting, transformations, executable expression, variable list/nested extraction, dynamic URL, arbitrary attribute, or upstream kwargs are accepted.

CSS accepts up to four simple tag/class/id compounds with one space as a descendant combinator. XPath accepts only a relative/descendant start `./`, `.//` or `//`, followed by up to four simple element names. Both have a 96-character limit. This deliberately excludes positional predicates, wildcard matching, XPath functions/axes, browser pseudo-selectors, bracket attribute filters, commas and unions. These restrictions are a **pilot**: they may omit valid real-world selectors; extend only after evidence and tests demonstrate the safety and value of a broader grammar.

`to_crawl4ai_config()` always **revalidates** the spec and constructs one non-LLM `CrawlerRunConfig` with one `JsonCssExtractionStrategy` or `JsonXPathExtractionStrategy`. This is a *data constructor*, not a network call.

`normalize_extracted_content(raw, spec)` accepts only JSON text up to **64 KiB**, strictly requires a list of row objects whose keys match the declared fields, rejects duplicate JSON keys, nested objects, arrays, non-text values, nonfinite values and invalid Unicode. It validates **every decoded row**, including those omitted because of row or output-size limits; malformed late rows fail the entire response rather than being hidden by truncation. It rejects mappings with excessive key counts before constructing key sets, and rejects strings longer than the 64 KiB input cap before making a UTF-8 copy; the byte-length check still rejects oversized multibyte text. It returns at most **20 rows**, with at most **240 characters per cell** and a complete JSON output budget of **12 KiB** (including metadata, measured with Python's default `json.dumps(..., ensure_ascii=True)` separators and the longer `truncated=false` literal). Arbitrary pretty-printing, transport wrappers, or extra indentation can increase the eventual wire size, so the network integration must perform its own final payload limit check. It marks row/cell/output truncation rather than claiming full content coverage; it marks `site_completeness=unknown` and `authorization_verified=false`. Raw page strings remain **untrusted data**; they are not executed or treated as instructions. The call does not perform network, file, process or permission changes.

## Future integration and remaining security gates

The module intentionally is **not imported by `bridge.py`**, does not register new MCP tools, and does not change caching, history or the existing 12-tool API. Integrating it requires explicit review and tests for:

1. Validate the schema **before** making any network request. Reuse the existing guarded `web_fetch` source/final URL checks, approval and one-use grant proofs, DNS/redirect/SSRF containment, epoch and permission expiration/revocation rechecks and on-demand lease.
2. Verify `extracted_content` actually appears from the installed Docker `/crawl` path with a fixed schema, then measure three approved public layouts versus existing Markdown/tables/JSON-LD results. Do not use `/config/dump` compatibility as proof of extraction.
3. Enforce **runtime**, not only output, limits: selector evaluation CPU/time and page size, concurrent requests, result bytes, cancellation and backend resource behavior. The current offline validator caps only schema shape and a returned JSON string; it does not cap work already done inside Crawl4AI.
4. Validate source and final URL permissions **again before releasing any record**. Do not attach `authorization_verified=true` based solely on this parser. Never automatically fetch attribute values or discovered URLs.
5. Add negative host tests for redirects, private IP/DNS changes, XSS-like text, malformed HTML, revoked/expired permissions and oversized/concurrent crawls. Preserve existing cache/pagination behavior and all defaults.

This implements a separately reviewable part of [issue #300](https://github.com/Fooftilly/local-search-mcp/issues/300) under [roadmap #297](https://github.com/Fooftilly/local-search-mcp/issues/297). Neither issue should close because of this offline component.
