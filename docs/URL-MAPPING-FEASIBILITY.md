# URL mapping feasibility gate (issue #298)

**Status (2026-11-05):** installed-host API compatibility and an exploratory comparison on three distinct, operator-approved public sites have been observed. The comparison has partial pagination and XML-semantic limitations; it is **not a production URL-map acceptance**, nor a complete-site inventory. No `web_map` tool is implemented. The existing 12 tools, permissions, and defaults remain unchanged.

## Verified upstream source and operator host evidence

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

**Remaining:** full pagination/structured-XML coverage (especially Peroniko 2/5 and Django 6/8), XML index/child-sitemap safety and semantics (especially MDN), independent live firewall-negative/public probes, reboot acceptance, and any production mapper. The running container was observed with image unclecode/crawl4ai:latest, image ID sha256:065f475c90ac75fe388141597b0cdba118ebf7e03f59e2a37456c89e6b864c59 (not signature/provenance verification). The installed 0.9.5 API exposed 33 authenticated OpenAPI paths, with no mapping-named route.


## Observed acceptance — 2026-11-05

- The operator verified two sleeping, owner-controlled Docker backends, restart policy no, and zero unverified project containers. The root firewall service was active and the boot audit passed all static checks, but reported ready_for_manual_reboot_test: a live negative/public egress probe and actual reboot recovery remain unproven.
- An approved public fetch triggered guarded Crawl4AI cold wake. The installed health endpoint returned version 0.9.5. The authenticated read-only preflight reported advertised_path_count=33 and route_status=no_advertised_mapping_route. This establishes no *advertised mapping-named route*, not absence of all hidden or differently named API operations.
- The first representative approved public host, www.peroniko.xyz, yielded 32 safe homepage link candidates (24 initially returned, eight in continuation; 4.719 seconds shell wall time). Its sitemap.xml was retrieved successfully (1.848 seconds) as *flattened Markdown*, split over five 24,000-character pages. The second page was fetched successfully. URL strings are split at page boundaries by the existing generic document paginator; this is not a data-loss defect, but XML location tags/metadata are no longer preserved in the emitted Markdown. The sitemap contains visibly more candidate URLs than the homepage, but these are not validated XML locations.
- The earlier example.com/sitemap.xml returned HTTP 404 and is only an error-path smoke test. It is not a representative sitemap benchmark.

## Bounded offline comparison helper

Use scripts/url_mapping_benchmark.py on **JSON captured from the existing guarded** web_fetch, web_links_page, and web_fetch_page tools. It performs no network calls, checks no live permissions, writes no approvals, and registers no MCP tools. It only reports exploratory unique same-host URL-looking strings, cross-source overlap, page completeness and truncated continuation status; it never calls the heuristic results XML-validated locations.

From an approved public homepage and its separately verified sitemap URL, capture the full JSON responses as private files. Use the real URLs, not placeholders. Before any capture, verify approval and the publisher's robots/access rules in the operator dashboard. A wrongly chosen unapproved URL can create a normal pending request; the offline analysis script cannot prevent that when you capture the responses manually.

For each site, save the first homepage response in home.json and the first sitemap response in sitemap-1.json. If the first sitemap response returns a document_id and page_count greater than 1, use that **fresh temporary handle** to capture each web_fetch_page(page=2,3,...) response in consecutive sitemap-2.json etc. Retrieve any remaining homepage link results using web_links_page with the real link_id. Do not save bearer handles to a repository or publish the capture files. A handle expires after 15 minutes.

Run from the PR #305 checkout. The operator's shell selects the capture files,
while the Python comparison script accepts **only bounded newline-delimited JSON
on standard input**; it cannot open a caller-selected filesystem path:

~~~sh
{
  jq -c . /path/to/private/home.json
  jq -c . /path/to/private/sitemap-1.json
  jq -c . /path/to/private/sitemap-2.json
  jq -c . /path/to/private/links-0.json
} | python3 -m scripts.url_mapping_benchmark \
      --sitemap-pages 2 --link-pages 1
~~~

Supply one homepage object, followed by exactly the declared number of
consecutive sitemap page objects and then the declared number of link
continuation objects. This example uses two sitemap pages and one link page;
adjust both counts and the shell-side inputs to the actual captures. Omit
--link-pages when there are none. Inputs must each be a single compact JSON
line and the total input is capped. The script reports a structured failure
instead of echoing private content when the input is invalid. No source path
is accepted as a script argument.

Repeat on operator-approved public sites as needed to validate completeness and parser behavior. The offline script cannot
infer fetch latency: record the wall times separately. Use an owner-only
directory and delete the capture files afterward. A shell reading private
operator captures remains responsible for verifying the chosen files; do not
feed it untrusted file paths.

The analysis caps record count, bytes per input record, aggregate input bytes, and total extracted Markdown characters. It joins consecutive document pages with no inserted separators and excludes a potentially incomplete final URL token from partial captures; delimiters include spaces, tabs, and line breaks. It preserves trailing URL punctuation when comparing homepage and sitemap candidates, since punctuation may be a valid URL path character. These comparisons remain heuristic, not XML parsing. It never retrieves or authorizes discovered URLs. Input lines are validated and the helper never opens filesystem paths. The original XML structure has already been flattened by the existing Crawl4AI response; restoring exact XML sitemap semantics needs a separately reviewed, bounded upstream-source extraction approach, after the three-site benchmark.


## Three-site exploratory benchmark — operator measurements (2026-11-05)

The operator ran the existing guarded MCP `web_fetch(fresh=true,include_links=true)` homepage and `web_fetch(fresh=true)` sitemap requests, then used this PR's **offline NDJSON benchmark**. Both new benchmark runs exited with code **0**. Times below are shell/subprocess wall times including client overhead; they are one-off observations, **not controlled warm/cold latency distributions or total multi-page benchmark time**.

| Site | Homepage fetch | Sitemap fetch | Observed unique same-host homepage links | Heuristic sitemap URL candidates | Sitemap candidates absent from observed homepage links | Sitemap pages supplied | Homepage link continuation |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `www.peroniko.xyz` | 4.719 s | 1.848 s | 12 **initial only** | 589 | 580 | **2/5** | **not supplied**; eight retained links previously reported |
| `www.djangoproject.com` | 5.827 s | 1.958 s | 21 | 647 | 644 | **6/8** | Three pages supplied; all retained continuation entries received |
| `developer.mozilla.org` | 4.364 s | 1.349 s | 98 | 11 | 11 | **1/1** | Ten pages supplied; all retained continuation entries received |

**Interpretation and limitations**

- **Django**: The 647 same-host URL-looking candidates in the **first six of eight** flattened sitemap pages exceed the 21 observed homepage links; 644 are absent from that homepage set. Full pagination and original XML validation were not performed. The homepage's structured link extractor reported 46 safe candidates examined before output/continuation limits, not 46 same-host links.
- **Peroniko**: 589 unique same-host URL-looking strings across the **first two of five** sitemap pages, versus 12 *initial-response* internal homepage links; 580 were absent from those initial links. Because homepage continuation entries were not supplied, the reported difference is **not** the final homepage-versus-sitemap set difference. The homepage extractor previously reported 32 safe unique candidates examined (including out-of-scope links).
- **MDN**: Its `/sitemap.xml` is a **sitemap index** with child `.xml.gz` sitemap references. The eleven extracted URLs are index references, **not eleven site-page URLs** or evidence of improved page coverage. No child sitemap was fetched; compressed/decompression and XML semantics were **not** exercised. The homepage extractor reported 118 safe candidates examined, with 98 distinct same-host links observed after all retained continuations.
- All counts are **heuristic** strings in already-authorized Crawl4AI-flattened Markdown. They do not prove which XML `<loc>` elements are valid, robots-permitted, unique after normalization, or actually accessible. Sitemap source completeness remains **unknown** even where all extracted Markdown pages were supplied.
- The operator's run did not crawl any URLs discovered within the sitemaps, grant extra permissions through the scripts, or bypass the root crawler egress. Domain approval was performed separately via the local dashboard before this benchmark.

**Decision:** The installed Crawl4AI 0.9.5 authenticated OpenAPI inventory advertised no dedicated mapping route (33 routes inspected), and the three-site exploration shows that the current single-homepage links path misses substantial sitemap-discoverable URL-looking content on at least two sites. This supports **further design of an opt-in, bounded, guarded sitemap-XML extraction path** or a verified upstream-equivalent feature; it does **not** justify a generic crawler or an immediate unreviewed `web_map` addition. The downstream implementation still needs direct guarded XML bytes/structure, same-host/path approval and revocation checks, DTD/entity and compression-bomb protection, bounded sitemap indexes/depth/bytes/URLs, redirect and DNS/SSRF controls, and adversarial tests before deployment. The MDN compressed-child case must remain distinct from a page inventory. PR #305 remains research/benchmark infrastructure and does not close #298.
