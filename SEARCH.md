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

A category or engine name can contain letters, digits, dots, underscores, hyphens and spaces (at most 40 characters). Dotted engine names such as `brave.news` and `crates.io` are valid. Empty names, duplicate names and arbitrary URL syntax are rejected before contacting the upstream. Language tags follow SearXNG's current validator: `en-US` is accepted, while numeric-region `es-419`, underscores (`en_US`) and script tags (`zh-Hant-TW`) are rejected by the upstream parser. Invalid locales, ranges, or page numbers are also rejected. The backend may reject unsupported filters with a descriptive error. Passing an unsupported engine name or requesting paging/time filters against engines without those capabilities can return few or no results; the MCP bridge cannot guarantee which engines an operator has enabled.

**Examples**

```bash
fastmcp call http://127.0.0.1:8790/mcp web_search query='recent science' max_results=5 language=en categories=science time_range=month --auth none
fastmcp call http://127.0.0.1:8790/mcp web_search query='linux' max_results=8 engines=qwant page=2 --auth none
```

**Operator configuration:** SearXNG needs `search.formats: [html, json]` (or equivalent) enabled. Search categories and engines are defined by the local instance's `settings.yml`, may differ by installation, and some individual engines do not support language, pagination or time filters. Prefer selecting only engines visibly enabled on the local SearXNG preferences page. Results are not additionally ranked, deduplicated or re-fetched by this change. Check the [official SearXNG Search API](https://docs.searxng.org/dev/search_api.html) for parameters and capabilities.

Search queries, effective page numbers, supplied filters and limited result metadata are logged in private local JSONL history; avoid including secrets in queries or filters.
