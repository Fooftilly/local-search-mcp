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

A category or engine name can contain letters, digits, dots, underscores, hyphens and spaces (at most 40 characters). Dotted engine names such as `brave.news` and `crates.io` are valid. Empty names, duplicate names and arbitrary URL syntax are rejected before contacting the upstream. Language tags follow SearXNG's current validator: `en-US` is accepted, while numeric-region `es-419`, underscores (`en_US`) and script tags (`zh-Hant-TW`) are rejected by the upstream parser. Invalid locales, ranges, or page numbers are also rejected. The backend may reject unsupported filters with a descriptive error. Passing an unsupported engine name or requesting paging/time filters against engines without those capabilities can return few or no results; the MCP bridge cannot guarantee which engines an operator has enabled.

**Examples**

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='recent science' max_results=5 language=en categories=science time_range=month --auth none
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux' max_results=8 engines=qwant page=2 --auth none
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
