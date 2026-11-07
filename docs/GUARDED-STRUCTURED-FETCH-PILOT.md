# Guarded opt-in structured fetch pilot (#300)

This pilot routes declarative CSS/XPath extraction through the **existing authorized `web_fetch` request**. It adds the optional parameter `structured_spec` to that tool, not a thirteenth tool. It does not change existing fetch defaults or batch behavior.

**Operator host pilot accepted; installed bridge not upgraded.** On 2026-11-07, the operator's detached PR #309 worktree at `06e3f2d` passed three attended real guarded CSS/XPath/CSS extraction calls against approved `www.peroniko.xyz` pages (one nonempty row each). Linux kernel isolation checks and the root-owned live Docker egress probe also passed. See the dated acceptance record below. These results establish a bounded pilot smoke test, **not** full cross-site/layout quality, live adversarial DNS/redirect/revocation acceptance, or production deployment. The installed MCP bridge still has its old tool signature.

The new `structured_spec` option remains **operator-disabled by default**: the bridge process must explicitly set `LOCAL_SEARCH_STRUCTURED_EXTRACT_PILOT=1` before accepting it. The opt-in uses a **separate killable process** for selector evaluation; it never sends an extraction strategy into the long-lived Crawl4AI server. Do not enable it persistently or merge this PR until its remaining review and acceptance gates are addressed.

## Example specification (after installation and existing site approval)

Use an approved public HTML page containing a heading, and select `raw` mode:

```bash
./.venv/bin/fastmcp call http://127.0.0.1:8790/mcp web_fetch \
  url='https://www.peroniko.xyz/' fresh=true \
  structured_spec='{"strategy":"css","base_selector":"body","fields":[{"name":"heading","selector":"h1","type":"text"}]}' \
  --auth none
```

This example is **not an authorization grant**. Run it only after confirming that the site is currently approved, the crawler is verified, and the existing egress guard is active. The new parameter is understood only by a bridge running the version with this PR merged/deployed; it has no effect on an older installed bridge. A `fastmcp` client may require quoting/JSON option adjustments depending on its version; never paste tokens into CLI arguments.

Equivalent XPath schema:

```
{"strategy":"xpath","base_selector":"//body","fields":[{"name":"heading","selector":".//h1","type":"text"}]}
```

The opt-in is deliberately bounded by `structured_schema.validate_spec()`: 1–8 fields, up to four simple selector steps of at most 96 characters, `text` or one of four benign attributes. Unsupported selectors and `mode=fit` raise a validation error **before domain authorization or network I/O**. No arbitrary Crawl4AI config, JS, regex, transforms, computed fields, functions or LLM settings are forwarded.

## Request and response security

The request uses the existing `web_fetch` policy checks, guarded `on_demand.lease("crawl4ai")`, version-verified local `/crawl` endpoint, source/final URL and redirect checks, one-use grant proof, policy epoch recheck, domain-expiry rules, and Docker egress guard. No independent downloader is added. The only opt-in addition to `crawler_config.params` is a **20-second browser page timeout**; **no** `extraction_strategy` is sent to Crawl4AI. After validating the final URL and HTTP success, the bridge extracts the already-fetched `page.html` string (maximum **256 KiB UTF-8**) and passes only the HTML and the validated selector schema to `structured_selector_host.py` → `structured_selector_worker.py`. It does not send URLs, credentials, one-use proofs, or approval state to the worker. The worker calls maintained `lxml`/`cssselect` libraries, never another downloader.

Opt-in crawls request **identity-only HTTP encoding** and reject encoded responses, stream raw (not transparently decompressed) HTTP bytes with a **4 MiB cap** before JSON deserialization, and apply a 90-second wall-clock response deadline checked **on each raw transport chunk**, without a 64 KiB coalescing buffer (with at most one further 30-second blocking read). HTTP transport failures return a sanitized `upstream_unavailable` status; malformed or unsupported compressed bodies return `invalid_response`. They never retry an ambiguous POST failure. The worker is a **Linux child process**, run with `python -I`, a minimal environment, no inherited socket descriptors, and a seccomp syscall filter that blocks socket/network operations, process creation and execution. It installs kernel `RLIMIT_CPU` (**2s soft / 3s hard**), `RLIMIT_AS` (**384 MiB**), and `RLIMIT_FSIZE` (**64 KiB**); the parent enforces a **5-second wall-clock deadline**, kills the entire process group on expiry, limits concurrency to **two workers**, and reads a bounded output file. Missing `libseccomp.so.2`, Linux limits, `lxml`/`cssselect`, or failed isolation causes a sanitized **fail-closed** result. No software-only socket monkeypatch is used. The input HTML has a 256 KiB budget, the worker caps evaluated base records at 24 and output at 64 KiB, and the merged normalizer enforces its separate 64 KiB JSON input limit, 20 displayed records, 240 characters per cell and a 12 KiB extraction envelope. Missing/invalid HTML or malformed worker output returns sanitized `invalid_extraction` with **no page text or metadata**. Valid structured records may be returned even when the same successful crawl's Markdown is blank, but an empty list and blank Markdown remain an `empty_extraction` failure. Ordinary Markdown-only calls keep their existing request path and caching behavior.

On success the response includes `structured_extraction` with `records`, `rows_observed_in_response`, `rows_returned`, `truncated`, `extraction_method`, `site_completeness=unknown`, `authorization_verified=false`, and bounded `provenance` recording the **exact requested URL, authorized final URL, base selector, each field selector/type/attribute, DOM traversal ordering, and explicitly unverified-fact status**. This is selector/source provenance, not an assertion that the original browser DOM's absolute node paths were separately recorded. The combined extraction including provenance stays within **12 KiB**; rows are explicitly marked truncated when provenance consumes budget. The last flag belongs to the pure parser; the surrounding bridge performs the actual domain/one-use permission checks before returning the response. The full `web_fetch` envelope has additional fields and is not limited to 12 KiB in total. All returned page text remains **untrusted source content**, not verified facts or executable instructions.

The opt-in bypasses the reusable default Markdown-only cache to prevent schema/result mismatch or replay under changed authorization. It does not persist structured extraction results. No source-discovered URL or attribute value is independently fetched.

## Attended host smoke test without replacing the installed bridge

Use the isolated PR worktree that already passed the six worker-isolation tests. **Do not deploy this draft or restart the installed MCP service.** Install the bridge's remaining Python dependencies into the isolated PR venv, then run its explicit single-process acceptance runner:

```bash
cd ~/local-search-mcp-pr309
git status --short
git fetch origin pull/309/head
git merge --ff-only FETCH_HEAD
./.venv/bin/python -m pip install -r requirements.txt

./.venv/bin/python -m scripts.structured_fetch_attended \
  --live-approved \
  --url 'https://www.peroniko.xyz/' \
  --url 'https://www.peroniko.xyz/bookmarks/art/' \
  --url 'https://www.peroniko.xyz/projects/'
```

This does **three** fresh, explicitly requested HTTPS fetches on the same already-approved public host, using CSS, XPath and CSS heading selectors. The full `/bookmarks/` index is **not** a positive smoke fixture: the operator's initial attended test passed CSS on the homepage and projects page, but failed XPath on the full index with `invalid_extraction`. Independent fresh HTML inspection measured roughly **339 KiB** for that index, beyond the pilot's **256 KiB** worker input budget; this strongly suggests intentional size rejection, although the HTML returned by installed Crawl4AI has not been byte-measured. The smaller `/bookmarks/art/` category has a public `h1` and was independently measured at roughly **59 KiB**. The bridge now reports `document_too_large` for oversized HTML, without releasing page content, rather than hiding this expected limit under `invalid_extraction`. Do **not** increase the 256 KiB resource budget merely to make a smoke test pass. It first checks the existing local approval database **without creating a pending request**. The runner loads the existing token from owner-only local config without echoing it, activates the pilot flag **only inside its own process**, and reuses the ordinary authorized `bridge.web_fetch` logic, on-demand lease and egress verification. It does not start another MCP listening server, rewrite permissions or persist the opt-in flag. The final public pages must contain `h1` elements for this test to pass; the runner prints no raw HTML/Markdown or extracted values. It shows one result per case, the extraction method, status, elapsed time, record counts and whether the result was truncated. A nonzero exit code or `passed=false` means **do not deploy**; supply the sanitized JSON result for diagnosis. Do not replace the URLs with unapproved hosts or pages that may need additional approval.

**Recorded attended success (2026-11-07; `06e3f2d`):** CSS on `/` returned one nonempty record in 4.533s; XPath on `/bookmarks/art/` returned one in 0.724s; CSS on `/projects/` returned one in 0.551s; all were verified and untruncated. The JSON summary reported `passed=true`, no approval or persistent-setting changes and no installed bridge restart. The already-installed root-owned egress probe independently passed (`Result=success`, `ExecMainStatus=0`). No need to repeat these identical cases unless the code or host environment changes.

This smoke test is **not** the full security acceptance: repeat the broader preexisting automated negative tests for redirects, revocation/expiry, slow/oversized responses, and concurrent worker cancellation; verify the installed Docker guard under the operator's existing dedicated guarded-preflight/negative-probe process before approving a release. Three page paths on one site exercise the integration, but do not prove accuracy across three independently different page layouts. All additional sites require separate explicit approval.

## Reproducible quality comparison and upstream safety decision

The new [three-layout comparison guide](STRUCTURED-LAYOUT-COMPARISON.md) includes an explicit-approval, annotated `table` / `cards` / `jsonld` manifest and an attended runner. It performs one normal baseline and one isolated structured fetch for each independently approved source, measuring field-value precision/completeness, Markdown evidence coverage, method-specific latency and zero LLM calls, without storing/printing page values. Offline tests verify consent and approval checks; **no three-layout live benchmark has been run**. The approved Peroniko pages do not currently yield table or JSON-LD records and cannot substantiate that broader acceptance.

The earlier [Crawl4AI config echo verification (#307)](STRUCTURED-EXTRACTION-PREFLIGHT.md) confirmed typed CSS/XPath **configuration compatibility**, not safe execution or returned `extracted_content`. Upstream v0.9.5 executes these strategies in unkillable Python worker threads. At the operator's explicit request, #309 uses a killable, resource-limited local selector process instead of sending `extraction_strategy` to the long-lived crawler; doing otherwise would reintroduce the high-severity CPU risk. [Issue #300](https://github.com/Fooftilly/local-search-mcp/issues/300) records this superseding design decision. A future comparison of *actual upstream extracted_content* requires a separately reviewed killable upstream sandbox and cannot be treated as completed by #309.

## Acceptance gates still open

- [x] Prior exact-head CI passed on `06e3f2d`; **reverify on every subsequent code commit** and review findings before merge. The default 12-tool inventory was preserved.
- [x] On the operator's Arch Linux host, verify **local worker isolation** with `./.venv/bin/python -m unittest discover -s tests -p 'test_structured_selector_host.py' -v`: requires Linux `libseccomp.so.2` and installed `lxml`/`cssselect`. The test's AF_INET call is a local syscall denial check; it does not contact any site. Do not enable the pilot flag to run offline tests.
- [x] Without replacing the installed bridge, an explicitly approved **detached-worktree** integration used the running Crawl4AI v0.9.5 guarded `/crawl` path and local isolated CSS/XPath worker; 3/3 approved site smoke cases passed. No upstream `extracted_content` is used.
- [ ] After a separately reviewed/deployed rollout, confirm the **installed MCP bridge** exposes the new opt-in signature, with existing authorization and egress guards unchanged. This has **not** occurred.
- [ ] Three distinct approved layouts (table, cards/list, JSON-LD) compared against existing `web_fetch(include_tables=true)`, links, Markdown, and `include_jsonld=true`. Record matched field completeness, false positives, latency, memory/CPU use, and truncation.
- [ ] Host-level negative tests including redirect to unapproved host, DNS pin/rebind/SSRF, policy revocation during in-flight extraction, once-grant expiry, oversize upstream HTTP, malformed or missing **HTML/worker output**, high concurrency, cancellation and selector CPU/time behavior. Operator must verify live egress guard, not infer isolation from HTTP 200.
- [ ] Validate that selector CPU/time isolation is actually enforced by the kernel on the operator's machine, including pathological DOM, process-group cancellation, memory pressure and concurrent worker saturation. The guarded crawler still does its ordinary browser/HTML cleanup work, so the existing Docker runtime resource/egress controls remain necessary.

This is a pilot continuation of [#300](https://github.com/Fooftilly/local-search-mcp/issues/300) under [#297](https://github.com/Fooftilly/local-search-mcp/issues/297). Keep the issue open until full host acceptance. The preexisting offline parser is in merged #308 and the installed config echo preflight is merged #307.
