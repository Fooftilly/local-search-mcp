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

A category or engine name can contain letters, digits, dots, underscores, hyphens and spaces (at most 40 characters). Dotted engine names such as `brave.news` and `crates.io` are valid. Empty names, duplicate names and arbitrary URL syntax are rejected before contacting the upstream.

**Engine-selection safety:** Specify **either** `categories` **or** `engines`, never both. The installed SearXNG combines their selections as a **union**, which can contact unintended search providers (including Google CSE Images even when `engines="bing images"` is requested). The bridge now rejects combinations before any search. An explicit `engines` list is checked against the local SearXNG `/config` engine inventory before the request; unknown, disabled or unverifiable names fail closed rather than falling back silently to Yahoo. A matching `!bang` query modifier cannot be combined with explicit `engines` either. The preflight is a bounded read-only local GET, with no agent-visible configuration or approval action. An unexpected result from a different engine also becomes a structured failure, not a misleading search result. Without `engines`, the usual category selection and legacy default behavior stay unchanged.

This does **not** replace the operator's `./setup.sh --search-engines` workflow to disable Google across default categories. Until the operator applies a Google exclusion, an ordinary unfiltered `categories=images` call may still use Google-backed providers. Language tags follow SearXNG's current validator: `en-US` is accepted, while numeric-region `es-419`, underscores (`en_US`) and script tags (`zh-Hant-TW`) are rejected by the upstream parser. Invalid locales, ranges, or page numbers are also rejected. The backend may reject unsupported filters with a descriptive error. Engine names missing from the installed active inventory are rejected before searching; requesting paging/time filters against engines that lack those capabilities may still return few or no results.

**Examples**

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='recent science' max_results=5 language=en categories=science time_range=month --auth none
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux' max_results=8 engines=qwant page=2 --auth none
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

**Operator configuration:** SearXNG needs `search.formats: [html, json]` (or equivalent) enabled. Search categories and engines are defined by the local instance's `settings.yml`, may differ by installation, and some individual engines do not support language, pagination or time filters. Prefer selecting only engines visibly enabled on the local SearXNG preferences page. Results are not re-fetched by the optional deduplication and ranking features; the original default remains unmodified. Check the [official SearXNG Search API](https://docs.searxng.org/dev/search_api.html) for parameters and capabilities.

Search queries, effective page numbers, supplied filters and limited result metadata are logged in private local JSONL history; avoid including secrets in queries or filters.
