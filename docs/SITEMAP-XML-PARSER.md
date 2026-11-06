# Bounded sitemap XML parser (issue #298)

This is a **parser-only** security slice after the exploratory three-site URL-mapping benchmark merged in #305. It does **not** register a web_map tool, fetch a URL, inspect operator approvals, start Crawl4AI, bypass the crawler firewall, parse compressed sitemaps, or follow sitemap references. **Do not call it on remote-agent input or expose its results over MCP until source provenance and live authorization are integrated and tested.**

## What is included

The parse_sitemap_xml(data, source_url=..., scope_host=..., path_prefix="/", max_urls=100) function parses at most 1 MiB of *original UTF-8 XML bytes* using [defusedxml](https://github.com/tiran/defusedxml) with DTD, internal-entity and external-entity processing explicitly forbidden. It recognizes standard sitemap urlset and sitemapindex roots (default sitemap namespace or no namespace), and only the direct loc element of each url or sitemap entry. It does not confuse image/extension loc elements with webpage URLs. A sitemap index yields entry_type=sitemap_reference, not page URLs, and references are **never followed**.

The parser rejects malformed XML, compressed bytes, unsupported encodings, overly deep/complex documents, and excessive source entries. It rejects cross-host, credential-bearing, relative, fragmented, malformed-path, out-of-prefix, and non-HTTP(S) entries, deduplicates accepted same-host strings, and returns at most 100 URL records. It reports both the number of accepted candidates and whether the **output** was truncated. all_supplied_xml_parsed=true means only that this one input document passed the XML parser; source_completeness=unknown does not claim that the publisher's website is fully indexed. Parsing a URL **does not constitute permission to fetch or release it**. The fetch_permissions_verified=false field is intentional.

The optional parse_sitemap_documents([...], scope_host=..., path_prefix="/", max_urls=100) helper accepts at most **two caller-supplied original XML byte documents**, enforces a **2 MiB aggregate raw-byte budget** and a **100 returned-URL budget across both**, and retains the distinction between page URLs and child-sitemap references. It never fetches the second document, and does not infer that it belongs to the first. It fails without returning partial results if either document is unsafe. Aggregated observations are still not proof of complete site coverage or current authorization.

The caller-supplied host and prefix are *filters*, not proof of local dashboard decisions. The existing domain-policy and crawler egress guards remain the only authorization and network boundaries.

## Existing upstream compatibility and three-site evidence

The authoritative source is the [merged #305 feasibility and operator benchmark](URL-MAPPING-FEASIBILITY.md). On the operator's installed **Crawl4AI Docker 0.9.5**, the authenticated OpenAPI inventory advertised **33 paths** and **no dedicated mapping, sitemap-seeding, or domain-mapping route**. The upstream Python SDK has AsyncUrlSeeder and DomainMapper, but these were not shown to be exposed by the installed Docker REST service; the hosted Crawl4AI scan API is a different service. This supports an independently bounded parser, **not** a second unguarded downloader.

The operator compared the existing guarded homepage links with sitemap Markdown using three separately approved public hosts (single-run wall times, **not** full end-to-end multi-page timings):

| Host | Homepage fetch | Sitemap fetch | Observed same-host homepage links | Heuristic sitemap URL strings not in observed homepage links | Sitemap text pages analyzed |
| --- | ---: | ---: | ---: | ---: | --- |
| Peroniko | 4.719 s | 1.848 s | 12 initial only | 580 | 2 of 5; no link continuation analyzed |
| Django | 5.827 s | 1.958 s | 21 | 644 | 6 of 8; link continuation supplied |
| MDN | 4.364 s | 1.349 s | 98 | 11 | 1 of 1; child sitemap references, **not page URLs** |

The observations support the existence of a real discovery gap for Peroniko and Django, but their URL counts are **not schema-validated XML loc records** and pagination/coverage is partial. MDN demonstrates that a sitemap *index* must never be misrepresented as individual website pages. These are exploratory results, not accuracy guarantees, permission grants, production-map validation, or evidence that compressed child sitemaps can be processed safely.

## Deliberate integration gate

The existing Crawl4AI web_fetch path returns Markdown for sitemap URLs; XML tags and source semantics may already have been lost before the bridge creates web_fetch output. Crawl4AI documents CrawlResult.html as original HTML, but that is **not proof** that XML sitemap content survives the installed Docker 0.9.5 browser crawler unchanged. Do not rebuild XML from flattened Markdown or add a second raw HTTP client as an undocumented bypass.

Before enabling a network-facing sitemap parser, demonstrate on the actual guarded Crawl4AI Docker instance that the original XML *bytes/structure* and a verified final URL can be obtained from the **existing authorized crawl** without a fresh downloader. Then wire it through the same public-target DNS checks, operator approval, redirect validation, one-use grant proof, policy epoch and expiry/revocation checks that protect web_fetch before emitting any URL list. No child sitemap may be requested without a **new separately approved and guarded fetch**. The pure multi-document helper now enforces two supplied documents, 2 MiB aggregate XML bytes and 100 returned URLs, but the network-facing orchestrator must **independently** enforce request count, response/decompression budgets, timeout/depth, source-to-child provenance, and effective permissions at every fetch and result release. An offline parser budget cannot authorize or bound network activity.

Compressed .xml.gz references are reported as references only. Decompression must be reviewed separately with compressed-byte and inflated-byte caps and no trust of Content-Encoding or filename alone. A sitemap-index path can differ from a page path; parent document scope cannot silently grant access to a child. Source-set completeness, robots restrictions, request count and publisher availability remain unknown unless independently established.

## Testing

Run python -m unittest discover -s tests -v and python -m compileall -q . to cover the pure parser. The tests include nested sitemap/index discrimination, DTD/entities, malformed encodings, compressed bytes, oversized XML, relative/cross-host URLs, unauthorized subdomains, path traversal/escaping, duplicate links, output truncation, image extension locations and XML nesting limits. This is **offline parser coverage**, not an assertion of DNS/redirect SSRF, permission revocation, cache, live crawler or compressed-child testing.
