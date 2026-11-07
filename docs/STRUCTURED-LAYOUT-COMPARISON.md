# Structured extraction: annotated layout comparison (#300)

This is a **separate quality-evidence gate**, not an automatic endpoint or permission change. [PR #309](https://github.com/Fooftilly/local-search-mcp/pull/309) has a successful three-page CSS/XPath heading smoke test on the operator's previously approved Peroniko site. That smoke test is **not** a three-layout benchmark: the already-approved homepage, art bookmarks and projects pages returned no table or JSON-LD records. Do not present the smoke result as table/cards/JSON-LD coverage.

`scripts/structured_layout_comparison.py` makes this evaluation reproducible without approving domains, bypassing the existing guarded fetch path, sending data to an LLM, or logging source text. It requires an **operator-annotated** local manifest with exactly one each of `table`, `cards`, `jsonld`; each needs an individually preapproved HTTPS URL, one bounded canonical CSS/XPath spec, and 1–20 known expected rows. No other URL is fetched. URLs cannot contain a query, fragment, credential or custom port, and all three host approvals are checked **before any fetch**. The operator must ensure the source pages are public and the annotation does not contain secrets.

The manifest must be a JSON file with this structure, replacing all **illustrative, deliberately unapproved** `.example` domains and illustrative values with real operator-reviewed and already-approved sources:

```json
{
  "cases": [
    {
      "kind": "table",
      "url": "https://approved-table.example/",
      "spec": {"strategy": "css", "base_selector": "tr", "fields": [
        {"name": "title", "selector": "td", "type": "text"}
      ]},
      "expected": [{"title": "Known table cell text"}]
    },
    {
      "kind": "cards",
      "url": "https://approved-cards.example/",
      "spec": {"strategy": "css", "base_selector": "article.card", "fields": [
        {"name": "title", "selector": "h2", "type": "text"}
      ]},
      "expected": [{"title": "Known card title"}]
    },
    {
      "kind": "jsonld",
      "url": "https://approved-jsonld.example/",
      "spec": {"strategy": "css", "base_selector": "article", "fields": [
        {"name": "headline", "selector": "h1", "type": "text"}
      ]},
      "expected": [{"headline": "Known JSON-LD article headline"}]
    }
  ]
}
```

The CSS/XPath schema is intentionally restrictive; **the example table mapping is not a guaranteed match** for an arbitrary site. Inspect the approved public page and choose selectors/field names and ground-truth values that match actual content before running.

For typed baselines, the optional `baseline_fields` mapping declares **canonical schema field name → raw baseline key**. This avoids falsely counting a JSON-LD date as missing when the safe schema uses `date_published` but JSON-LD exposes `datePublished`. For example, on the `jsonld` case set `"baseline_fields": {"date_published": "datePublished"}`; the corresponding `spec.fields[].name` and `expected` must use `date_published`. The input validator rejects undeclared, duplicate or unsafe aliases; `baseline_fields` is unnecessary when names are already identical. Typed table headers are normalized to lowercase with spaces replaced by underscores before applying optional aliases. Do not add baseline aliases to the `cards` case, which uses Markdown evidence rather than inventing a typed card API.

From the existing detached PR #309 worktree, save the annotated manifest to the **one fixed local operator config path** `~/.config/local-search-mcp/structured-layouts.json` (owner-controlled file; not a URL and never a CLI argument). Do not use symlinks or store sensitive source values there. Any `--manifest` or other arbitrary-path argument is rejected to prevent filesystem traversal.

```bash
cd ~/local-search-mcp-pr309
git fetch origin pull/309/head
git merge --ff-only FETCH_HEAD

mkdir -p ~/.config/local-search-mcp
# Create/edit your reviewed public-only JSON using your normal editor:
${EDITOR:-vi} ~/.config/local-search-mcp/structured-layouts.json

./.venv/bin/python -m scripts.structured_layout_comparison --preview

# After preview confirms approvals, and only with your explicit consent to
# six fresh guarded crawls (baseline + isolated worker for each of 3 URLs):
./.venv/bin/python -m scripts.structured_layout_comparison --live-approved
```

**Metric definitions and limitations.** The script compares exact normalized field/value occurrences against operator annotations, with duplicates counted. `structured.precision_pct` is the fraction of extracted field/value pairs matching the annotated corpus; `structured.completeness_pct` is the fraction of annotated pairs recovered. This **field-level** measure cannot tell when two card records have swapped their date/title associations. The additional `structured_record_alignment` uses maximum one-to-one matching of whole annotated records against extracted records (requiring each annotated field/value pair to be present **in the same record**), so swapped fields are not scored as correct records. The typed table and JSON-LD baselines include `baseline_record_alignment` and `baseline_typed_values` with the same caveats about source fields and manual annotation. These comparisons do **not** imply verified publisher claims.

For cards there is no typed baseline, so its typed precision is **null**; `markdown_evidence.evidence_coverage_pct` counts expected text values observed in Markdown, and **never claims Markdown precision**. `baseline_limits` explicitly reports whether the Markdown preview was truncated, table candidates/rows/columns were capped, or the JSON-LD parser reached its scan/output budget. A missing value in a bounded baseline may be due to these preview limits, not genuinely absent page information. These are sample-level comparisons over annotated records, **not** site-wide precision or recall.

Each case uses **two fresh existing guarded crawls** (6 total), one baseline and one isolated structured extraction, and reports independent wall-clock latency, crawl count and zero LLM calls. This does **not** provide a monetary cloud API price estimate or the upstream crawler's total CPU/memory usage; self-hosted processing consumes local resources and electricity. All outputs contain **counts, percentages, timings and statuses only**, no URLs, extracted values, page HTML, token, or annotation text. The existing authorization and egress checks apply to both fetches; no new MCP tool or persistent feature flag is enabled.

**Status:** The harness and offline tests can pass CI before a public corpus exists. The true three-layout approval/evaluation remains **not executed** until three suitable public examples are separately approved and annotated. Upstream Crawl4AI typed strategy execution is deliberately excluded from the long-lived crawler, because v0.9.5 launches synchronous extraction in a thread that cannot be killed by its Docker wall timeout; [issue #300](https://github.com/Fooftilly/local-search-mcp/issues/300) now records this accepted isolation design and the optional separate upstream-in-a-sandbox research gate.
