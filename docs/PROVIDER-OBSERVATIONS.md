# Private provider observations (issue #211)

The dashboard's **Provider observations** page at
`http://127.0.0.1:8791/provider-observations` is an explicitly requested,
read-only summary of metadata **already present in existing private rotating
JSONL history**. It makes no additional SearXNG queries, crawler requests,
engine selection changes, DNS lookups, domain approval changes, or Docker
operations. It does not alter the SQLite history index or save provider
statistics to any new persistent file/database. It is intentionally not part
of the ten-second live dashboard polling workflow.

The report offers bounded rolling **1-, 24-, or 168-hour** UTC windows. It
uses at most the latest **2 MiB of each of four existing history files** and
examines at most 20,000 events; oversized/malformed events are ignored.
Symlinks and nonregular inputs fail closed, with no file paths disclosed in
the UI. Source rotation, clipped file tails, malformed input and group limits
are visibly reported. Samples may be incomplete even when no scan limit is
triggered because older events may have rotated away. An unavailable history
is **not** shown as a healthy/empty provider report.

Only explicitly observed result-engine names in the **first five audit
result rows** contribute to engine-group counts. A single search call may
contribute to multiple engines; these counts are not the number of provider
requests and **cannot** be converted into provider uptime or reliability
rates. Engines with no retained hits or failed before returning results will
not appear in those groups. Error calls have no safe per-provider failure
attribution under the existing audit schema; the dashboard explicitly
reports non-success calls separately. An event with no valid duration
contributes to sample counts but not to the duration mean.

The visible duration is **the complete MCP search call duration**, not a
measured provider latency. It may include the local cache, preflight and
SearXNG wait time; several engine groups may display the same end-to-end
call duration. Requested categories are taken only from explicitly selected
well-known category names in the old audit, not inferred from engine
capabilities. Default searches and unrecognized user-provided categories are
grouped under **unspecified**. Historical cache hits, per-engine failures,
provider-specific duration and category-to-engine attribution were **not**
retained; those data cannot be reconstructed from the current history.

The parser extracts only strictly bounded public-ish engine labels, safe
counts and aggregate durations; it never returns query text, page titles,
result URLs, snippets, raw errors, secrets, domain rules, or source file
paths. The page uses the operator dashboard's loopback listener, exact
Host check, browser Origin/Fetch Metadata controls, no-store headers, CSP,
HTML escaping and same-origin stylesheet. It is read-only with no forms
that mutate server state. Rendering requires no JavaScript.

## Evidence and follow-up gate

Run `python -m unittest -v tests.test_provider_observations` from the
project's configured Python environment. The suite covers rotated history,
unattributed failures, missing timing, privacy, symlink refusal, bounded
files, invalid windows and actual local HTTP Host/Origin checks. This is
repository-level acceptance only. The installed operator host and current
history rotation have not been measured.

This is a **first useful reporting slice**, not the full #211 reliability
dashboard. Completing #211 would need a separate opt-in design for
capturing safe provider-failure observations and reliable provider timing
without adding queries or sensitive telemetry, plus host acceptance.
Historical provider reliability must not be fabricated.
