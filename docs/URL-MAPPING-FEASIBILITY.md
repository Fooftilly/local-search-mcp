# URL mapping feasibility gate (issue #298)

**Status:** research and installed-host acceptance are pending. No web_map MCP tool is implemented, and the existing 12 tools, permissions and defaults remain unchanged.

## Verified upstream source, not installed-host evidence

- Crawl4AI [v0.9.5 Docker server.py](https://github.com/unclecode/crawl4ai/blob/v0.9.5/deploy/docker/server.py) declares /crawl and /crawl/stream but no dedicated /map, /seed, /sitemap or /scan endpoint. This finding applies to that versioned public source, not necessarily to the operator's installed image or a future release.
- [AsyncUrlSeeder](https://docs.crawl4ai.com/core/url-seeding/) and [DomainMapper](https://docs.crawl4ai.com/core/domain-mapping/) are in the Python SDK. The library APIs are not implicitly callable through the self-hosted Docker REST API. Their default discovery can request robots files, sitemap indexes, Common Crawl and other hosts beyond the original authorized URL.
- The cloud-hosted Crawl4AI /scan API is a separate paid/credited service, not proof of an existing self-hosted endpoint.
- [Docker auth_gate.py at v0.9.5](https://github.com/unclecode/crawl4ai/blob/v0.9.5/deploy/docker/auth_gate.py) requires a valid credential for all non-public paths, including OpenAPI. A 401, 404 or redirect is not evidence that mapping is unsupported.
- The existing bridge already exposes single-page links via web_fetch(include_links=true) and web_links_page. A link returned by those tools is **not** a fetch authorization.

## Read-only, attended host compatibility check

From the repository root, with the crawler **already running**:

~~~sh
./.venv/bin/python -m scripts.url_mapping_preflight
./.venv/bin/python -m scripts.url_mapping_preflight --inspect-routes
~~~

The second invocation needs the existing CRAWL4AI_API_TOKEN process environment variable. Load it through the private operator configuration used for normal bridge operation. Never put the credential in an argument, history, screenshot, issue comment or PR.

This preflight requests only fixed loopback 127.0.0.1:11235/health and, when explicitly requested with --inspect-routes, the authenticated /openapi.json route inventory. There are no external network requests, remote redirects, inherited proxies, Docker starts, new approvals, environment writes or crawler calls. Responses are size-limited, failures and potentially sensitive upstream bodies are redacted, and an unavailable or unsupported host fails closed.

Reported route candidates must be independently reviewed for their input/security contracts. A reported no_advertised_mapping_route means only that **no route with an explicit discovery-like path segment appeared in the authenticated OpenAPI paths**; it does not prove the absence of all hidden or differently named capabilities. Missing or malformed OpenAPI is reported as unavailable, not as proof of absence.

## Three-site benchmark, required before implementing a mapper

Run only against **three operator-selected, public, already-approved sites** and separately approved exact sitemap URLs/paths; do not create approvals merely to conduct this experiment.

1. Record installed Crawl4AI version/image digest and, for each site, the exact approved start, robots and sitemap URL/path scopes. Verify robots restrictions and publisher access policy.
2. Fetch the starting HTML page through the existing guarded web_fetch(url, include_links=true) operation, then enumerate any additional retained results using web_links_page. Capture verified statuses, unique in-scope URLs, duplicates, truncation, and elapsed wall time.
3. Separately fetch its already-approved sitemap URL using **the same guarded web_fetch route**. Record if the XML survives as trustworthy text/links; do not assume extracted Markdown is raw XML or treat a failed/unsupported MIME response as an empty sitemap. Where available, compare a published HTML or TXT index too. No generic direct HTTP downloader may bypass the crawler's root egress guard.
4. For each site compare useful unique in-scope coverage and latency against existing single-page extraction. Record out-of-scope hosts, missing sitemap, document count, evidence sources, and unknown completeness without inventing counts.
5. Inspect the installed /openapi.json candidate routes and actual Docker image before choosing any integration. If a maintained upstream discovery endpoint is available, assess its own security contract rather than implementing another crawler. If not, examine narrowly bounded XML extraction **only from bytes obtained through the existing guarded, authorized crawler**, with a reviewed safe XML library.

Any later opt-in mapping operation needs caps (initial proposal: 2 sitemap documents, 2 MiB aggregate, 100 returned URLs), time/depth limits, duplicate removal, and explicit distinction between partial and complete results. Reject DTD/entity expansion, malformed/ambiguous encodings, compression bombs, external namespaces/hosts, redirects to unapproved targets and unauthorized subdomains or paths. Check permissions and DNS/SSRF controls for every *requested* sitemap and revalidate grants before exposing cached or paged results. Discovered URL strings are never approved or fetched automatically. The preflight grants no permission and changes no production routing.

**Not completed by this PR:** live installed-host verification, exact image/digest confirmation, a three-site benchmark, any verified improvement in mapping quality, or operator acceptance. The offline tests for the probe do not call remote providers.
