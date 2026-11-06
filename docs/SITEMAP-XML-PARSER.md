# Bounded sitemap XML parser (issue #298)

This is a **parser-only** security slice after the exploratory three-site URL-mapping benchmark merged in #305. It does **not** register a web_map tool, fetch a URL, inspect operator approvals, start Crawl4AI, bypass the crawler firewall, parse compressed sitemaps, or follow sitemap references. **Do not call it on remote-agent input or expose its results over MCP until source provenance and live authorization are integrated and tested.**

## What is included

The parse_sitemap_xml(xml_bytes, source_url=..., scope_host=..., path_prefix="/", max_urls=100) function parses at most 1 MiB of *original UTF-8 XML bytes* using [defusedxml](https://github.com/tiran/defusedxml) with DTD, internal-entity and external-entity processing explicitly forbidden. It recognizes standard sitemap urlset and sitemapindex roots (default sitemap namespace or no namespace), and only the direct loc element of each url or sitemap entry. It does not confuse image/extension loc elements with webpage URLs. A sitemap index yields entry_type=sitemap_reference, not page URLs, and references are **never followed**.

The parser rejects malformed XML, compressed bytes, unsupported encodings, overly deep/complex documents, and excessive source entries. It rejects cross-host, credential-bearing, relative, fragmented, malformed-path, out-of-prefix, and non-HTTP(S) entries, deduplicates accepted same-host strings, and returns at most 100 URL records. It reports both the number of accepted candidates and whether the **output** was truncated. all_supplied_xml_parsed=true means only that this one input document passed the XML parser; source_completeness=unknown does not claim that the publisher's website is fully indexed. Parsing a URL **does not constitute permission to fetch or release it**. The fetch_permissions_verified=false field is intentional.

The caller-supplied host and prefix are *filters*, not proof of local dashboard decisions. The existing domain-policy and crawler egress guards remain the only authorization and network boundaries.

## Deliberate integration gate

The existing Crawl4AI web_fetch path returns Markdown for sitemap URLs; XML tags and source semantics may already have been lost before the bridge creates web_fetch output. Crawl4AI documents CrawlResult.html as original HTML, but that is **not proof** that XML sitemap content survives the installed Docker 0.9.5 browser crawler unchanged. Do not rebuild XML from flattened Markdown or add a second raw HTTP client as an undocumented bypass.

Before enabling a network-facing sitemap parser, demonstrate on the actual guarded Crawl4AI Docker instance that the original XML *bytes/structure* and a verified final URL can be obtained from the **existing authorized crawl** without a fresh downloader. Then wire it through the same public-target DNS checks, operator approval, redirect validation, one-use grant proof, policy epoch and expiry/revocation checks that protect web_fetch before emitting any URL list. No child sitemap may be requested without a **new separately approved and guarded fetch**; a two-document, 2 MiB aggregate, 100-URL returned limit and time/depth budget must also be enforced at the orchestrator boundary. Do not assume this parser's per-document 1 MiB cap alone enforces the total-documents limit.

Compressed .xml.gz references are reported as references only. Decompression must be reviewed separately with compressed-byte and inflated-byte caps and no trust of Content-Encoding or filename alone. A sitemap-index path can differ from a page path; parent document scope cannot silently grant access to a child. Source-set completeness, robots restrictions, request count and publisher availability remain unknown unless independently established.

## Testing

Run python -m unittest discover -s tests -v and python -m compileall -q . to cover the pure parser. The tests include nested sitemap/index discrimination, DTD/entities, malformed encodings, compressed bytes, oversized XML, relative/cross-host URLs, unauthorized subdomains, path traversal/escaping, duplicate links, output truncation, image extension locations and XML nesting limits. This is **offline parser coverage**, not an assertion of DNS/redirect SSRF, permission revocation, cache, live crawler or compressed-child testing.
