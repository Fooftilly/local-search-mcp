# Free-only provider trials (#258)

This is an **operator-run, opt-in external comparison**, not a new tool in the
self-hosted MCP bridge or a fallback for its existing guarded SearXNG/Crawl4AI
search/fetch. No provider connections or account configuration happen on
installation or during the default preview.

## What this implements

Three independently hosted **keyless, ongoing-free** MCP search offerings:

| Provider | Fixed HTTPS endpoint | Strictly permitted remote tool | Published allowance |
| --- | --- | --- | --- |
| [You.com free profile](https://you.com/docs/agents/mcp-server) | `https://api.you.com/mcp?profile=free&tools=you-search` | `you-search` only | 100 search/discover queries per day, no account/card/key |
| [Firecrawl keyless](https://github.com/firecrawl/firecrawl-mcp-server) | `https://mcp.firecrawl.dev/v2/mcp` | `firecrawl_search` only | Ongoing keyless **rate-limited** free search, no account/card/key |
| [Exa anonymous MCP](https://github.com/exa-labs/exa-mcp-server) | `https://mcp.exa.ai/mcp?tools=web_search_exa` | `web_search_exa` only | Free anonymous search with rate limits; remaining quota/renewal amount not published |

**Do not conflate** Firecrawl's separate [signed-up Free plan](https://www.firecrawl.dev/pricing)
with keyless access: the **1,000 credits/month** allocation belongs to
the account Free plan, and the keyless endpoint has its own daily
server-managed limits. The script does not know how much of the external
free allowance remains and does **not** claim it can enforce provider-wide
quotas across multiple clients or machines. No billing credentials are sent
or accepted, so this exact keyless trial cannot initiate a charge against
a paid account; the provider controls free-tier refusals.

## Commands

Run from the repository root with the existing project Python environment.

```sh
# Zero provider traffic; lists exact keyless servers, documented limits
# and available fixed, non-sensitive query profiles.
./.venv/bin/python -m scripts.free_provider_pilot

# Zero provider traffic or file writes; produces a manual, standalone
# MCP-client JSON snippet for review. Do not merge this with a privileged
# or authenticated MCP entry unintentionally.
./.venv/bin/python -m scripts.free_provider_pilot --client-config

# An interactive non-root TTY is required; type the exact word FREE
# after reading the third-party data-transfer warning.
./.venv/bin/python -m scripts.free_provider_pilot --sample --provider you --profile docs
./.venv/bin/python -m scripts.free_provider_pilot --sample --provider firecrawl --profile docs
./.venv/bin/python -m scripts.free_provider_pilot --sample --provider exa --profile docs
```

The same hardcoded, nonsensitive profiles used by
[`scripts.engine_live_comparison`](SEARCH.md) are `docs`, `science`
and `montenegro`. No `--query`, arbitrary endpoint URL, output URL,
authentication token, API key, OAuth, private filename or user-supplied
document path is accepted. The command does not install MCP clients,
modify Cursor/Grok, inspect secrets, edit SearXNG YAML, enable new engine
routing, start Docker services, or alter DNS/firewall or Cloudflare.

For **each attended invocation**, there is exactly **one** fixed public
`tools/call` (after MCP initialization and `tools/list`) to one
explicitly selected provider. The request asks for at most three search
results: You.com `query` and `count=3` (also
`extraction=none` **only when the discovered input schema supports it**);
Firecrawl `query` and `limit=3` with **no scrape options**; Exa
`web_search_exa` with `query` and `numResults=3`, **no page fetch or
agent research tool**. The tool name and
schema are verified against the provider's narrow keyless allowlist
before the request. Unknown tool sets/required arguments fail closed;
the script never tries an alternative billable provider, searches with
Google, or retries failed requests. One operator session should not
automate more calls. No new background jobs or telemetry are written.

The response is intentionally limited to coarse status, verified
bounded search-result count, distinct source-host count and
**local tool-call latency**. FastMCP's default log and progress handlers
can emit **server-controlled** notification contents through Python logging;
this pilot replaces both with asynchronous discard handlers so upstream
notifications cannot bypass its redacted output policy. It never prints raw results, snippets,
URLs, provider exception text, HTTP bodies, or the public query. An
unrecognized provider response reports `unverified_response`, **not**
zero results or success. If transport fails after consent, the script marks
remote request accounting **unknown** rather than asserting that no search
was sent. The official provider APIs may evolve; validate
the live contracts on an attended test host before relying on the
reported metrics. No actual external request occurs in CI.

Exa MCP's anonymous rate limits can be reported as text with **MCP success**
instead of a structured error. This pilot recognizes the documented
free-rate-limit notice, reports `rate_limited` with one definite tool call,
and never mistakes it for an empty result. Exa's normal search output is
**plaintext** containing both result headers and arbitrary page highlights.
Highlight text can imitate `Title:`, `URL:` and markdown result separators,
so **an exact result count or distinct-host count cannot be trusted** from
this text. When the first result header looks valid, the pilot reports
`text_results_unverified` and a `null` count, not an invented number.
It reports exact counts only for verified structured result objects.
Unknown formats, malformed first headers, and extra required tool arguments
fail closed. No URLs, titles or highlights are ever printed. The anonymous
quota has no reliably verified remaining-credit interface, so this is
**not** a fixed monthly allowance.

## Important security boundary

These are **separate third-party MCP servers**, not providers installed
behind local-search-mcp. A request to them leaves the host, sends the
fixed public query to the vendor and **does not go through** the local
bridge's domain-approval queue, SearXNG routing, protected crawler,
host-root egress guard or private search history. Use **only** the fixed
public presets in this trial; do not use corporate/internal data,
individual names, secret URLs or approved internal fetch destinations.
The command never invokes `firecrawl_scrape`, `firecrawl_parse`,
`web_fetch_exa`, or Exa's paid `agent_run`
and never sends a target URL. It does not expose your local bridge.

The client uses the pinned HTTPS endpoints, normal TLS validation,
disabled HTTP redirects and `trust_env=False` (no inherited HTTP
proxy or proxy credentials); it refuses Authorization, Cookie,
X-API-Key and payment headers. No OAuth flow is initiated. A
30-second absolute invocation deadline bounds the Python call.
A provider may still enforce stricter daily limits or refuse
anonymous traffic; this is reported as a failure, not a signal to
upgrade or attach a payment method.

## Follow-on work / not part of this PR

[Exa Starter](https://exa.ai/pricing) advertises $10 **recurring**
monthly free credits without a card; its **key-based SearXNG API** integration
requires an operator-owned account and key, but the **anonymous MCP search**
in this pilot does not. [Tavily](https://www.tavily.com/pricing) advertises
1,000 monthly free credits without a card and requires an operator-owned key. Exa has an
[upstream SearXNG `exaapi` engine](https://docs.searxng.org/dev/engines/online/exaapi.html),
but installed-image support, secret installation, egress and actual
account balance are **unverified**. Do not add a custom `engines:` YAML
writer or send billable API calls until the owner explicitly approves
a credential-handling implementation with an account-based free-credit
hard stop and rollback. Brave requires a card for its advertised
monthly credit and is not implemented. Jina's one-time API-key signup
grant is not treated as a recurring free monthly quota.

See [issue #258](https://github.com/Fooftilly/local-search-mcp/issues/258)
for further opt-in provider acceptance and comparison work.
