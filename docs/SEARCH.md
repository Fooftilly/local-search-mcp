# Advanced SearXNG search

The MCP tool remains compatible with existing `web_search(query, max_results=8)` calls. All optional filters are forwarded only to the locally configured SearXNG endpoint, normally `http://127.0.0.1:8888/search`. No public search API keys are needed.

| MCP argument | Default | Allowed values | Local SearXNG parameter |
| --- | --- | --- | --- |
| `query` | Required | 1–300 characters | `q` |
| `max_results` | `8` | Integer; clamped to 1–12, as before | Local response limit |
| `language` | Empty (instance default) | `en`, `en-US`, `auto`, `all`; lowercase 2–3-letter language codes with optional `-XX` region | `language` |
| `categories` | Empty (instance default) | Comma-separated names, up to 8 distinct entries | `categories` |
| `time_range` | Empty | `day`, `week`, `month`, `year` | `time_range` |
| `engines` | Empty (instance default) | Comma-separated configured engine names, up to 8 distinct entries | `engines` |
| `page` | `1` | Integer 1–10 | `pageno` (omitted on page 1) |
| `deduplicate` | `false` | Boolean | Local opt-in result processing |
| `ranking` | `searxng` | `searxng` or `source_count` (requires `deduplicate=true`) | Local stable ordering |
| `include_published_date` | `false` | Boolean | Local opt-in upstream date-only metadata |
| `include_image_metadata` | `false` | Boolean | Opt-in, validated image/thumbnail URL metadata from image-category results |
| `include_search_status` | `false` | Boolean | Local opt-in provider-failure status (not forwarded) |
| `allowed_result_domains` | Empty | Up to 8 comma-separated exact public hosts or `*.host` for subdomains only | Local post-result filtering (not forwarded) |

A category or engine name can contain letters, digits, dots, underscores, hyphens and spaces (at most 40 characters). Dotted engine names such as `brave.news` and `crates.io` are valid. Empty names, duplicate names and arbitrary URL syntax are rejected before contacting the upstream.

**Explicit provider-selection safety:** Specify **either** `categories` **or** `engines`, never both. SearXNG combines their selections as a **union**, which can contact unintended search providers. Each nonempty explicit category or engine list is checked against one bounded local SearXNG `/config` inventory before an uncached search. Every requested category must be present on a currently enabled engine; every requested engine must be enabled. Unknown, disabled, unverified, or partially valid lists fail closed before any `/search` request: SearXNG can otherwise silently drop unknown selectors and fall back to default providers. A `!bang` query modifier cannot override either explicit selection. This is a read-only loopback GET with no new provider request, configuration change, approval action, or secrets exposed. An unexpected result from a different explicitly selected **engine** also becomes a structured failure. Searches with neither category nor engine selection retain existing legacy default behavior. Cached category/engine results may reflect a previous inventory until cache expiry; `fresh=true` revalidates against the current inventory.

This does **not** replace the operator's `./setup.sh --search-engines` workflow to disable Google across default categories. Until the operator applies a Google exclusion, an ordinary unfiltered `categories=images` call may still use Google-backed providers. Language tags follow SearXNG's current validator: `en-US` is accepted, while numeric-region `es-419`, underscores (`en_US`) and script tags (`zh-Hant-TW`) are rejected by the upstream parser. Invalid locales, ranges, or page numbers are also rejected. The backend may reject unsupported filters with a descriptive error. Engine and category selectors with no enabled matches in the installed inventory are rejected before searching; requesting paging/time filters against engines that lack those capabilities may still return few or no results.

**Examples**

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='recent science' max_results=5 language=en categories=science time_range=month --auth none
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux' max_results=8 engines=qwant page=2 --auth none
```

## Operator-managed default engine selection

The **Search engine selection** link in the loopback-only management console
(`http://127.0.0.1:8791/engine-settings`) now displays current general
SearXNG engines, their enabled state and categories. GET only reads verified
container ownership, the private bind-mounted settings and bounded local
`/config` inventory; it never wakes a sleeping backend. When SearXNG is
sleeping, use the attended `./setup.sh --search-engines` workflow to wake
and inspect it deliberately.

A protected same-origin, CSRF-checked POST with explicit `APPLY` applies
a nonempty selection through the **existing** `scripts/searxng_engines.py`
operator editor: its project-identity check, cross-process lock, private
checkpoint, same-inode YAML write, verified restart and rollback remain
authoritative. The displayed inventory/YAML revision is rechecked inside the
lock and any stale/invalid selection is rejected before writes. Engine
enabled flags are global across all their categories. This particular
page cannot enable Google-named engines, preserving the operator's
non-Google preference. It does not override agent-supplied explicit
`engines` or `categories` filters, rate-limit engines, or select
providers based on performance. The dashboard supports the full bounded
inventory (up to 512 general engines), with a separately bounded form size.
A successful console update immediately invalidates cached search responses
and detaches pre-update in-flight cache writes in the shared MCP/dashboard
process; existing in-flight callers may still receive their original result.
Edits performed through the separate setup CLI do not share this in-process
invalidation and may require a bridge restart or cache TTL expiry. Dedicated
request pacing and adaptive
routing are tracked separately in the engine-policy roadmap.

## Optional image URLs and thumbnails

The operator's 2026-10-30 read-only SearXNG `/search?categories=images&format=json`
sample confirmed `img_src`, `thumbnail_src`, `resolution`, and `source` are
present as **strings** in Bing Images and Pinterest records. It did not establish
actual dimension values, stable licensing metadata or DuckDuckGo's raw field
availability. This feature is deliberately limited to validated image URLs and
strictly parsed dimensions, not image retrieval or image analysis.

Set `include_image_metadata=true` on `web_search(categories="images")`
(or a deliberately selected image engine) to add these optional fields **only**
to upstream results marked `category="images"`:

- `image_url`: the validated upstream `img_src` image candidate
- `thumbnail_url`: the validated upstream `thumbnail_src` candidate
- `source_page_url`: the validated HTTPS source-page `url` from that **same**
  result, separate from the existing `url` field
- `image_width` / `image_height`: positive integers only when the same
  result reports an exact `WxH` `resolution` (both 1–20,000 pixels)

Each image/thumbnail candidate must be an at-most-2,048-character ASCII
HTTPS URL with a public-looking DNS hostname, no credentials, local/IP host,
nonstandard port, ambiguous IDN, control/whitespace, raw backslash or encoded
control characters. Missing, invalid or ambiguous fields are **omitted**,
never guessed. A valid source-page alias is only added when at least one
validated image or thumbnail candidate exists. Ordinary non-image hits
never acquire image metadata merely because a search filter requests images.
Other upstream image fields (e.g. `source`, `img_format`, `thumbnail`,
`metadata`) are not copied or treated as license evidence.

When `deduplicate=true`, image URL metadata belongs **only to the primary
first-seen result**. Later duplicate search results never add or replace image
URLs, thumbnails or dimensions for another provider. With the flag omitted,
the established four-field response and prior optional search outputs remain
unchanged. The opt-in has its own in-memory cache variant and is not sent to
SearXNG as an API parameter or recorded in private history.

**Security boundary:** These are untrusted, provider-reported URL *strings*.
Listing them neither verifies that an image exists or is copyright-cleared
nor downloads, crawls, approves, DNS-resolves or follows them. If a later
operation needs to fetch the source, it must separately pass existing
operator domain-approval, redirect, and guarded crawler security checks.
The image candidate URLs do **not** grant permission for image downloads.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='Montenegro mountain landscape photography' categories=images include_image_metadata=true --auth none
```

## Optional upstream publication-date metadata

Set `include_published_date=true` to preserve a **calendar date reported by
SearXNG's own `publishedDate` field**. Valid, ISO-formatted upstream values
produce two additional keys, `published_date: "YYYY-MM-DD"` and
`date_source: "searxng.publishedDate"`. Unknown, absent, `null`, malformed,
non-ISO and invalid-calendar values produce **no date keys**. The deprecated
`pubdate`, result snippets, fetch time and any inferred dates are ignored.

The source may report a timestamp **without a timezone** (seen in a real
SearXNG JSON capture); returning only the original calendar day prevents
falsely converting it to UTC. This is a *search-engine-reported* date, not
verification by the original publisher, nor a guarantee that engines implement
`time_range`. When deduplicating, the optional date belongs only to the
**first retained result**; later engines' possibly conflicting dates are not
silently attributed to it. `source_engines` lists reporting search engines,
not original publication sources.

The existing default four fields are unaffected. Date opt-in is independent
of deduplication/ranking, is **never forwarded** as a SearXNG API parameter,
has its own in-memory cache variant and is not persisted in audit-history
result summaries.

A real SearXNG `2026.10.19` JSON capture documented both
`publishedDate=null` and `2024-10-23T12:00:38` with no offset:
[recorded example](https://gannet.dbhq.uk/searxng-search-api/).
These are third-party instance examples, **not yet operator acceptance**
for the configured Yahoo instance. Crawl4AI's generic page `metadata`
does not provide a verified publication-date contract, so the guarded
`web_fetch` response is intentionally unchanged by this slice.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='public technology news' categories=news include_published_date=true --auth none
```

## Optional partial-provider diagnostics

Use `web_search(query, include_search_status=true)` when knowing whether
some attempted providers failed matters more than the smallest response.
This is **opt-in**: ordinary `web_search` retains its established list of
`title/url/snippet/engine` results. The diagnostic variant wraps successful
results as `{success: true, status: "ok" | "degraded" | "unknown",
results: [...], search_status: {...}}`. A degraded status means SearXNG
explicitly returned nonempty `unresponsive_engines` even if other providers
produced results; `unknown` means the upstream failure-report field has
an unrecognized shape.
The existing error response is preserved when search fails completely.

### Bounded recovery after provider failures

When SearXNG returns HTTP 200 with zero results and a nonempty **list** of
`unresponsive_engines`, the bridge makes **at most one additional search**
after 150 ms. Unfiltered default searches can isolate `duckduckgo` when it
was already attempted (named in the initial failure report) and the bounded,
local SearXNG `/config` inventory confirms it is still enabled. This can
recover from a combined-provider failure when DuckDuckGo works individually.
If that evidence/validation is absent, or the caller specified `engines`,
`categories` or a `!bang`, the retry keeps exactly the original search
parameters. There are no new backends, unseen provider choices, silently
enabled engines, or additional search calls beyond the existing recovery
budget. Healthy empty results, malformed responses and HTTP errors do not
trigger a search-level retry. Failed recoveries remain uncached.

Successful recovery keeps the usual result list. In `include_search_status=true`
responses, `search_status.recovery` reports `attempted`, `recovered`, and a
sanitized `first_attempt` status, plus `strategy` (`same_query` or
`isolated_engine`) and `engine` when isolation was attempted. Recovered
searches are labeled `degraded`
because the first attempt had provider failures. An empty second response with
no reported failure is treated as a legitimate empty result. This is bounded
best-effort recovery, not a guarantee that blocked providers become available.


The `search_status` object contains `coverage`
(`partial`, `no_failures_reported`, or `unverifiable`), bounded
`unresponsive_engines` names (up to 16), `reported_count` (first 128
records examined), `unparsed_count` and `truncated`. Only safe provider
names are retained from SearXNG's name or `[name, error]` fields;
potentially sensitive exception details, URL strings, query text and raw
upstream diagnostics are **never copied**. Successful diagnostic wrappers
still produce the usual private, bounded history result count and safe
title/URL/engine summary; `ok`, `degraded` and `unknown` are not
recorded as errors, and diagnostic failure details are not persisted. `no_failures_reported`
does **not** prove complete coverage or that disabled engines were tried.
It also does not distinguish an engine failure from engine coverage
omitted upstream.

Opt-in calls do not use the bridge's response cache: an earlier successful
snapshot must not be presented as current provider health. Diagnostic mode alone does not run more providers or issue additional retries,
wake extra backends, enable Google, or change default result ordering and
engine-selection validation. The one bounded recovery attempt described
above applies equally to ordinary and diagnostic searches.
A search that returned no results because engines failed still returns
`upstream_unavailable`. When `include_search_status=true`, that failure
now also includes the bounded `search_status` field; ordinary failure
responses stay unchanged. Provider-failure metadata is never a verified
statement about original publishers.

### Separately opt in to a private audit of reported provider failures

`web_search(query, include_search_status=true,
record_provider_diagnostics=true)` explicitly permits this **one call** to
record bounded, sanitized SearXNG-reported unresponsive provider names in the
existing private, rotating JSONL history. Recording is **off by default**,
including when `include_search_status=true` is used by itself. The option
requires diagnostic mode, returns the same opt-in response shape and does
not add any provider search or cache layer. It is not exposed on the batch
search tool.

The stored `provider_diagnostics` contains at most 16 strictly validated
engine labels (including parenthesized names), a declared coverage category,
bounded counts and a truncation flag. If bounded recovery was attempted,
opted-in audit merges sanitized provider reports from both requests and counts
observed failures **per tool call**, so a recovered search does not erase the
initial failure. The public `search_status` preserves per-attempt reports. Names containing suspicious secret
markers, URLs, credentials, controls, and upstream exception text are
discarded. The audit already stores normal search metadata, including
bounded query text; this opt-in adds *provider labels* to that private record,
not raw SearXNG errors or page content.

The read-only `/provider-observations` page now groups those explicitly
retained reports separately from the first five observed result engines
and shows how many opt-in calls reported each engine unresponsive. **No
failure rate or uptime percentage is valid:** the log has no per-provider
attempt denominator; `no_failures_reported` does not prove all upstream
engines succeeded. This is aggregate visibility into upstream-reported
diagnostics, not independently measured provider health or latency. Historical
calls made without the separate opt-in cannot be retroactively attributed.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='public research' include_search_status=true --auth none
```

## Optional source-domain filtering

Use `allowed_result_domains="example.org"` in `web_search` to retain
only results from the **exact** host `example.org`. Use
`allowed_result_domains="*.example.org"` for its child subdomains only
(`news.example.org`, but **not** the apex). Include both patterns if
both should match: `example.org,*.example.org`. The filter accepts at
most eight distinct public hosts, validating their DNS/IDN form with
the existing offline public/private suffix list (PSL), including hosted
tenant boundaries. Bare public suffixes such as `co.uk` and shared
host suffixes such as `github.io` are refused.

This option filters **only validated SearXNG result links already
received by the MCP**. It is not sent to SearXNG and does not make any
additional provider requests. Unlike a server-side `site:` query,
this cannot cause more official hits to be retrieved if SearXNG's
initial top results were mostly other sites. No matches yield `[]`;
result order, source attribution, deduplication, search-provider
selection and existing 12-result maximum remain intact. The source
filter becomes part of the in-memory cache key so differently scoped
queries do not share a cached response.

A returned link is **not** automatically approved for crawling:
`web_fetch` still requires the operator's normal domain permission and
public-DNS/egress guards. This option does not enable any search engine,
bypass excluded Google engines, approve or fetch an excluded URL, or
change the configured search categories/engines. Suffix matching is
label-boundary-aware, not substring matching; `evil-example.org`
does not match `*.example.org`.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='government documentation' allowed_result_domains='example.org,*.example.org' --auth none
```

## Optional deduplication and source attribution

By default, `web_search` keeps SearXNG's original order and the existing **four response fields** (`title`, `url`, `snippet`, `engine`). `deduplicate=true` groups conservative HTTP(S) URL matches **before** the result limit. The first result's URL/title/snippet/engine remain the primary fields, with additional opt-in fields:

- `source_engines`: ordered names of distinct engines observed in the merged records (including an upstream `engines` list when supplied)
- `source_count`: the number of those distinct engine names across the full validated upstream `engines` list (not capped at 16), **not** a search relevance score
- `duplicate_count`: the number of SearXNG result entries grouped together
- `source_matches`: up to eight additional `engine`/`title`/`snippet` alternatives *after the primary result*; empty when no duplicates were merged. The `duplicate_count` may exceed the bounded alternatives list.

URL identity lowercases DNS hostnames and IPv6 address hex digits (without changing IPv6 zone identifier case), drops standard HTTP/HTTPS ports, removes fragment identifiers, and treats an empty path as `/`. It preserves HTTP versus HTTPS, non-default ports, path case and trailing slashes, percent-encoding and query parameter values/order. It does **not** remove tracking parameters, recrawl canonical links or infer content equality. Invalid or credential-bearing URLs remain separate rather than being merged. Results spanning different search pages are never automatically combined.

`ranking="source_count"` is an **optional explainable heuristic**: groups with more distinct reporting engines come first; ties keep their original SearXNG order. This does not imply objectively higher-quality results or uniform engine coverage. A result returned by one engine may already carry an upstream multi-engine attribution list, which is counted without inventing additional matches. The unranked default remains `ranking="searxng"`.

Both options are handled entirely by the bridge, never forwarded to SearXNG. The MCP tool schema exposes `ranking` as an enumeration (`searxng`, `source_count`) and the tool description specifies that source-count ranking requires `deduplicate=true`. Cache entries are separated by deduplication and ranking choice; private audit history notes chosen flags but not alternate snippets. With deduplication enabled there may be fewer than `max_results` unique URLs even if SearXNG returned enough rows; the bridge does not invent pages or issue hidden extra upstream requests.

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux containers' max_results=8 deduplicate=true --auth none
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux containers' max_results=8 deduplicate=true ranking=source_count --auth none
```

**Engine wizard with on-demand sleep:** `./setup.sh --search-engines`
inspects the local SearXNG `/config` only when the verified backend is
running. The recommended Yahoo preset (choice 1) retains its documented
offline fallback without waking Docker. Choices 2–4 require the installed
engine list. If the **owned** SearXNG container is sleeping and on-demand
mode is enabled, choosing one of these offers an explicit `WAKE` prompt.
Declining makes **no Docker or settings changes**. Confirming `WAKE` uses
the existing project-ownership/restart-policy verification and shared
cross-process lock; it starts only the verified SearXNG container, waits
for health, and reads the bounded loopback engine list. A stopped backend
with on-demand disabled or a running backend with unavailable `/config`
returns an actionable error instead of silently cancelling.

A confirmed wake is **not** a settings change. You must still separately
type `APPLY` to edit the managed engine block and perform its scoped
restart. Safe parenthesized upstream engine names are retained by the
wizard and serialized as quoted YAML values; unrepresentable Google-named
inventory rows (including missing names) cause a no-change error
instead of a misleading claim that every Google engine was excluded.
For choice 3, the running backend's complete Google-named engine set is
rechecked under the setup lock before writing and after the scoped restart.
A changed inventory or still-enabled Google engine triggers safe rollback
and an error rather than a false success message.
A wake may leave SearXNG running until ordinary managed idle or an
attended stop; the wizard does not force an immediate shutdown, since
another MCP client may now be using it. No engine selection alters domain
approvals or intentionally enables Google providers.

### Read-only search provider discovery

After updating the MCP bridge and reconnecting your client, call
`web_search_capabilities()` before requesting explicit search-engine names.
This lightweight **read-only** MCP tool sends one bounded GET to the
configured local SearXNG `/config` endpoint and returns only:
`enabled_engines` (names with their reported category lists),
`categories`, `enabled_engine_count` and
`omitted_unselectable_engine_count` and
`omitted_unselectable_category_count`. Disabled engines (including
operator-excluded Google entries) are omitted. SearXNG may support engine
names longer than `web_search(engines=...)` accepts, and real
upstream names may also include punctuation (such as `naver (KO)`).
Those valid but unselectable enabled names are counted as omitted, not
advertised as usable or allowed to hide shorter selectable providers.
Likewise, printable upstream category labels outside the explicit
`web_search(categories=...)` grammar are not advertised as selectable;
`omitted_unselectable_category_count` reports that omission. Malformed
inventory records still fail closed. The output is a validated
snapshot of **enabled** providers, not a ranking, guarantee of availability,
proof of default category routing, or proof that an engine supports every
filter. Provider settings and any secret fields in `/config` are never
forwarded to the agent.

When SearXNG is sleeping, unreachable or returns an invalid/oversized
inventory, the tool returns `status=unavailable` without waking Docker,
running a provider search, changing any configuration or modifying domain
approvals. The operator-only `./setup.sh --search-engines` wizard (with
its separate `WAKE` and `APPLY` choices) remains the only engine
configuration workflow. Existing `web_search` behavior and its
explicit-engine fail-closed preflight remain unchanged.

**Operator configuration:** SearXNG needs `search.formats: [html, json]` (or equivalent) enabled. Search categories and engines are defined by the local instance's `settings.yml`, may differ by installation, and some individual engines do not support language, pagination or time filters. Prefer selecting only engines visibly enabled on the local SearXNG preferences page. Results are not re-fetched by the optional deduplication and ranking features; the original default remains unmodified. Check the [official SearXNG Search API](https://docs.searxng.org/dev/search_api.html) for parameters and capabilities.

Search queries, effective page numbers, supplied filters and limited result metadata are logged in private local JSONL history; avoid including secrets in queries or filters.


## Bounded multi-query search (opt-in)

Use `web_search_batch(queries=["first query","second query"], max_results=5)`
to compare **1–4 distinct public queries** in one MCP call. Each query is
validated before any network activity and receives its own existing
`web_search` invocation, including the ordinary cache, on-demand lease,
provider security preflight, per-query private audit, and bounded retry logic.
The batch runs at most **two** searches concurrently and at most **8** returned
results per query. An invalid query or category/engine combination rejects
the entire batch **before** any search request. Search terms must be distinct
case-insensitively; no automatic query expansion or additional providers.
SearXNG bang provider shortcuts (`!engine`) are rejected in this new batch
mode, including when no explicit category/engine filter was supplied.

The new response contains `success` (all queries succeeded), `query_count`,
and ordered `results` entries each containing `query`, `success` and either
`results`/`result_count` or a sanitized `status`/`reason`. A failure of
one provider/search does not discard successful sibling queries. The tool
supports shared `language`, `categories`, `time_range`, `engines`, and
`fresh` selectors; `categories` and `engines` remain mutually exclusive.
Each underlying individual search is already audited through the existing
private search history; no aggregate list of query text is persisted.

A cancelled batch skips queued work but cannot forcibly terminate a
synchronous search already running in a worker thread. The normal
`web_search` tool and its default four-field results are unchanged.

## Compact read-only capabilities (opt-in)

`web_search_capabilities()` with no arguments still returns its exact prior
full inventory shape. Optional `category="images"` or `engine="yahoo"`
filters the **currently enabled and selectable** engine list, never
enabling a provider. `max_engines=5` caps returned entries; `0` means
all (the default). `summary_only=true` omits individual engine entries
while preserving bounded category/count metadata.

Opted-in responses add `matching_engine_count`,
`returned_engine_count`, `details_omitted`, `truncated` and a `filter`
object. `enabled_engine_count` remains the **total** enabled-selectable
count, not the filtered count. A disabled/unknown exact filter returns
`status=not_enabled`; a sleeping/unavailable backend returns the
original bounded `unavailable` response. This discovery does not wake
Docker, contact providers, alter engine settings or approve domains.

## Individually selected search engine observations

The local `/provider-observations` console now includes a separate
**Individually selected local SearXNG searches** table. This tracks up to
three bounded, nonsecret /search operation records per search-tool call, but
**only** when the MCP actually invokes the local SearXNG search endpoint
with exactly one engine. It includes explicit `engines=duckduckgo` requests,
eligible isolated recovery attempts, and opt-in paced default routing.
Ordinary unrestricted default fanout searches, validation rejects and cache hits do **not** create
per-engine attempts.

The private JSONL history retains the full validated search query as before;
the new operation records contain only the engine label, explicit/recovery/
paced-default selection source, coarse upstream-row/empty/upstream-error outcome and measured
local /search operation duration. No response exception text, headers, API
tokens, result contents or provider URLs are added to telemetry. The duration
includes local SearXNG overhead and any transport retries and **is not**
remote engine execution latency. An empty result is different from a
SearXNG-reported unresponsive engine. The aggregate dashboard reads bounded
private history; this evidence is not used to automatically select providers
or rate-limit engines yet. Older records cannot be retroactively attributed.


## Opt-in isolated engine pacing (initial integration)

The bridge now enforces an operator-owned, **disabled-by-default** pacing policy
for individually verified engines, including explicit single-engine requests
and automatically selected engines for unfiltered default general searches.
The private file
`~/.config/local-search-mcp/engine-pacing.json` must contain the exact
`local-search-mcp/engine-pacing/v1` schema and owner-only permissions.
This is an opt-in pilot: the guarded management-console editor below
writes only the bridge's private pacing policy. SearXNG engine flags are
managed separately by the existing setup wizard and engine-selection page.

When the file is absent or `enabled=false`, ordinary requests retain
their previous combined-default behavior, retries/recovery and caching.
With an enabled policy, an **unfiltered default general query** (no
optional language, date-range or page selector) selects
one eligible, enabled general engine from the policy using a bounded local
SearXNG `/config` read and deterministic name ordering. A second candidate
may be used only when the first has not been admitted because of an existing
cooldown/lock. No fallback happens *after* a provider request starts.
Optional language, time-range and page settings on an automatic default
search also fail closed until support can be verified per eligible engine;
such filters remain usable with explicitly selected engines.
Explicit single-engine requests still require exact SearXNG verification
and a policy entry. A **single category** can route only to an enabled,
policy-listed engine whose verified SearXNG inventory declares *exactly*
that category. The outbound request contains the selected engine but
omits categories: SearXNG treats engines+categories as a union. Engines
shared between multiple categories, multiple requested categories, and
category searches with optional language/time/page filters remain
unsupported and fail closed to avoid expanding scope. !bang shortcuts,
multiple explicit engines, and unlisted providers also fail closed.
The bridge returns `pacing_selection_required` for those unsupported
selectors, `rate_limited` with a bounded retry hint for busy/cooling-down
engines, or `pacing_unavailable` for an invalid/unsafe settings file.

Each admitted single-engine search holds its cross-process reservation around
one local SearXNG `/search` request, without a hidden GET retry or recovery
attempt. With pacing enabled, result caching is skipped to prevent reusing
unrestricted responses from before activation. An enabled policy can therefore
reduce search availability; it does not yet automatically choose a provider,
balance multi-engine searches, offer parallelism above one per engine, or prove
third-party provider rate limits. The rate values are operator heuristics,
not documented remote quotas. The guarded editor below is available
for staged policy edits, but live enablement requires attended acceptance
on the installed host and explicit operator confirmation.

## Guarded engine pacing policy editor

The owner-only loopback management console exposes
[`/engine-pacing-settings`](http://127.0.0.1:8791/engine-pacing-settings).
It reads and atomically edits only the nonsecret
`~/.config/local-search-mcp/engine-pacing.json` policy used by the bridge.
The editor does not change SearXNG's own YAML, wake sleeping backends,
approve crawl domains or grant MCP callers configuration privileges.
Engine selection in SearXNG remains a separate guarded workflow.

Enter one `NAME=SECONDS` rule per line, with intervals between 0 and
3600 seconds. Blank lines are ignored. Engine names must be unique,
match the bridge's conservative engine grammar, and never begin with
`google`. Saving an **enabled** policy requires a live, owner-verified
SearXNG engine inventory; each entry must match an enabled engine name exactly, including case.
An unavailable or sleeping inventory blocks enablement without waking
the backend. A disabled policy can be staged or disabled even while the
backend is sleeping. The form uses the existing origin/CSRF protections,
a compare-and-swap revision token, strict confirmation (`ENABLE`,
`DISABLE` or `APPLY`), private owner-only files, a cross-process lock
and an atomic fsynced replacement. Concurrent stale saves fail safely.

**Enabling pacing restricts every outbound search to one verified,
policy-listed engine.** Unfiltered default general queries select
an eligible provider; single-category queries can select dedicated
category-only engines. Multi-category engines and filters, multi-engine
explicit requests and !bangs are rejected to avoid changing scope or
bypassing limits. Confirm the intended change and
attend host acceptance before enabling; this editor does not by itself
make multiple providers eligible, infer provider quotas or select engines.

## Single-category routing under pacing

A single `categories=images`-style request may now use one policy-listed
engine **only when** the installed SearXNG `/config` inventory states that
engine has exactly that category, and the engine is enabled. The request to
SearXNG uses the engine selector without a category selector; combining the
two would create a union and could contact unmetered engines. Category
labels and engine spellings are matched exactly against both the policy
and live inventory, not inferred from names. Mixed-category engines
(e.g. `["general", "images"]`), multi-category requests, optional language,
date-range or pagination filters, Google engines and disabled/unlisted
providers are excluded from automatic category routing. No eligible
category-dedicated engine returns `pacing_selection_required` without
sending a search. Busy engines may be skipped to another eligible,
category-dedicated engine *only before* any network request has started.
Actual admitted calls use the same engine pacing admission, one-shot
network semantics, source-verified result handling and bounded private
`paced_category` observation as paced default searches.

This is deliberately conservative: some search categories may have no
exclusively assigned engines, so they remain unavailable while pacing is
enabled. The operator must not interpret the inventory alone as proof
of a remote provider's current quota, support for every search filter, or
successful attended host acceptance.

## Read-only engine evidence preview

The `/engine-pacing-settings` page displays a read-only, nonsecret
24-hour comparison using the configured policy engine names and the existing
bounded private history. It does **not** wake SearXNG, trigger provider
searches, alter policy, or change the deterministic live engine order.
It uses direct, individually selected local `/search` operation records
(explicit, paced default, paced category); isolated and same-query recovery calls (even for explicitly selected engines)
and fanout-result attribution do not count in the comparison denominator.
Successful-operation mean latency is the **local SearXNG round trip**,
not remote provider time. Observed fraction of operations with
source-verified results is **not uptime, accuracy, quota or guaranteed
search quality**.

A row needs at least 10 direct local operations and one verified result to
qualify for a provisional comparison. If history is unavailable, truncated,
malformed or lacks a verified writer lock, the dashboard refuses to rank
engines. Other configured engines remain visible as insufficiently sampled.
The report is non-authoritative: provider mix, query selection, user-selected
engines, 24-hour retention and small sample sizes may bias comparisons.
The console also offers a distinct `evidence` routing mode, **disabled by
default**, inside the same private pacing policy. Enabling it for the first
time requires typing `ADAPT` in the guarded settings editor, and requires
pacing to be enabled. Legacy policies without a routing field use `fixed`.

When active, evidence mode only reorders **already enabled and policy-listed**
engines meeting existing general or dedicated-category safeguards. The
bounded 24-hour private history must be lock-verified, complete and
untruncated, and *every* eligible candidate must have at least ten direct
isolated requests and at least one source-verified result. Otherwise
the existing deterministic name order is preserved. This is an evidence
ranking pilot, **not** a remote capacity, uptime or rate-limit model;
selection bias can affect results. Explicit selections, pacing cooldown
reservations, no-retry semantics, no Google and no unverified categories or
optional filters are unchanged. A live operator-host comparison is still
required before enabling the mode.


## Read-only local routing acceptance preflight

After updating the repository on your own host, run:

```bash
./.venv/bin/python -m scripts.engine_routing_preflight
```

This emits a bounded JSON readiness assessment, **not a benchmark of provider
quality or a claim that host acceptance passed**. It uses the existing
owner-verified, already-running SearXNG inventory, the exact saved pacing
policy and 24-hour private observations. It reports the eligible general and
single-category engine sets, current fixed vs provisional evidence order,
candidate sample readiness, unavailable policy engines, and blockers
(including a sleeping/unavailable backend). It sends **zero searches**,
does not wake Docker, reserve cooldowns, write policy, modify engine flags,
change approvals or enable adaptive routing.

The preflight uses the same pure candidate selector as the MCP bridge, so
its eligibility rules cannot silently diverge. A missing/incomplete history
leaves fixed order as the effective choice; even a ready preflight does not
measure real network latency, throughput, simultaneous caller behavior,
provider quotas or search relevance. A separate attended nonsensitive
query/cross-process concurrency comparison is still required before you
enable `evidence` routing in the guarded console. Do not treat this
command's result as authorization to switch on the policy.
