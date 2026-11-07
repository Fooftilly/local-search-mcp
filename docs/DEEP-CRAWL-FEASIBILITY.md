# Deep-crawl Docker compatibility and security gate (#299)

**Status: research/preflight only.** No deep-crawl feature, MCP endpoint, scheduler, queue, authorization expansion, Docker setting, or remote crawl is added by this work. In particular, do **not** set `CRAWL4AI_ALLOW_DEEP_CRAWL=true` on the active crawler. The installed bridge has not been tested for safe per-discovered-URL authorization.

## Evidence from fixed upstream v0.9.5 source

- [v0.9.5 Docker self-hosting documentation](https://docs.crawl4ai.com/core/self-hosting/) and [v0.9.5 release notes](https://github.com/unclecode/crawl4ai/blob/v0.9.5/CHANGELOG.md): `deep_crawl_strategy` on the Docker API is disabled by default. An explicit `CRAWL4AI_ALLOW_DEEP_CRAWL=true` permits the bounded allowlisted BFS/DFS/BestFirst configurations; it is **not** a per-client permission grant.
- [Docker `deploy/docker/governor.py`](https://github.com/unclecode/crawl4ai/blob/v0.9.5/deploy/docker/governor.py): `clamp_deep_crawl` clamps attached strategy page and depth budgets and adds a URL-length filter, but its failure paths log warnings when they cannot set a limit. It does **not** call this project's local approval database, policy epoch or one-use grant checker for every selected link.
- [Docker `deploy/docker/config.yml`](https://github.com/unclecode/crawl4ai/blob/v0.9.5/deploy/docker/config.yml): bundled defaults are `limits.max_pages=100`, `max_depth=5` and `wall_clock_s=300`; the numerical caps must be verified on the **installed** container. A zero-valued page or wall limit is potentially unbounded.
- [Docker `deploy/docker/api.py`](https://github.com/unclecode/crawl4ai/blob/v0.9.5/deploy/docker/api.py): validates one starting URL for a deep crawl, applies server clamps and wraps crawl completion in a deadline. These checks are useful but do not establish a per-discovered-URL pre-navigation approval hook or proof that cancellation immediately kills browser work.
- [`BFSDeepCrawlStrategy` v0.9.5](https://github.com/unclecode/crawl4ai/blob/v0.9.5/crawl4ai/deep_crawling/bfs_strategy.py): selected level URLs are handed to `crawler.arun_many(urls=...)`. `include_external=False`, domain filters, glob filters, and downstream egress pinning limit parts of the risk but do not implement this bridge's expiring host/path approvals, policy-epoch verification or one-use semantics before each newly discovered navigation. The SDK has `cancel()` and `should_cancel`, but `cancel()` documents that an in-flight URL can finish; `should_cancel` errors also fail open in the tagged source. The unrestricted SDK callback is not an acceptable untrusted Docker parameter.

**Decision:** Native Docker BFS is **not yet safe enough to enable** under this MCP's approval model. A seed URL is not authorization for its children. A safer implementation would need a demonstrated server-side pre-navigation hook that applies the same checks to every URL (including final redirects and DNS), or a small bounded bridge coordinator that approves each exact URL before scheduling an existing guarded `web_fetch` call. A coordinator is a fallback only if baseline tests demonstrate value over the current `web_fetch(include_links=true)` plus `web_fetch_batch`.

## Installed environment: what is and is not known

The earlier [URL-mapping host evidence](URL-MAPPING-FEASIBILITY.md) recorded an operator-observed `unclecode/crawl4ai:latest` image exposing health version **0.9.5**. That is historical evidence, not proof of the **current** image digest, feature flag, effective server limits, runtime navigator behavior or a fresh guard test. The following preflight only confirms local server health and optionally the fixed configuration serializer contract.

### Read-only operator preflight

Run from a checkout containing this change with the local crawler **already running**. This command does not start Docker, accept a public URL or issue a crawl:

```sh
./.venv/bin/python -m scripts.deep_crawl_preflight
```

To inspect the authenticated config serializer, load the normal **local private** `CRAWL4AI_API_TOKEN` into the process environment (never paste it into CLI arguments, issue comments or reports), then explicitly run:

```sh
./.venv/bin/python -m scripts.deep_crawl_preflight --probe-config
```

The opt-in probe sends a single hardcoded `BFSDeepCrawlStrategy` (`max_depth=1`, `max_pages=2`, `include_external=false`) to loopback `127.0.0.1:11235/config/dump`, using the installed token. **It does not send a URL or call `/crawl`.** The output is bounded, redacted, and does not include tokens or upstream response bodies.

Interpret `config_status` precisely:

- `fixed_config_echo_verified`: the installed serializer echoed the fixed bounded config. This proves neither actual deep-crawl execution nor any discovered-URL policy enforcement.
- `rejected_400_reason_unknown`: the local serializer refused the config; this could be a default-off flag or incompatible syntax. The diagnostic intentionally does not infer which.
- `operator_token_required`, `unexpected_config_echo`, `unavailable`: incomplete evidence; investigate without enabling the feature.

Every result always reports `deep_crawl_ready=false` and `discovered_url_authorization=not_verified`. A successful exit from this preflight **never** authorizes enabling the Docker flag.

## Acceptance gate before a proposed implementation

1. Record the installed immutable container image/digest, the opt-in flag's actual state, effective max pages/depth/wall-clock/queue settings, root-owned network egress probe and available navigation hooks. Verify specifically whether a discovered URL can be checked against the original bridge permissions *before* a browser request; assume no until proven.
2. For two separately operator-approved public sites, compare the existing link + explicit batch baseline with any proposed strictly bounded coordinator or safely isolated upstream strategy. Count navigated URLs, precision/coverage over approved scope, wall time, memory/CPU, and unauthorized attempts. No unapproved links may be fetched in a benchmark.
3. Exercise external links, subdomains, cross-host redirects, private DNS/DNS rebinding, expired/revoked one-use grants, exhausted page/depth budgets, paginated loops, offline/asleep backends, and cancellation during an active crawl. A cancelled job must actually stop scheduling and release crawler resources; dropping the response alone is insufficient.
4. Review bounds before enabling any feature: proposed pilot ceiling is **10 pages, depth 1, 2 concurrent jobs, 60 seconds**, with explicit path scope, source/final URL reauthorization, no persistent archive, and opaque temporary results. These are design targets, **not verified enforced limits**.
5. Operator must approve actual flag/config and host-level testing separately. Do not add a 13th MCP tool or alter existing fetch defaults merely because `/config/dump` accepts a strategy.

Related tracking: [#298](https://github.com/Fooftilly/local-search-mcp/issues/298) (bounded URL discovery) and [#297](https://github.com/Fooftilly/local-search-mcp/issues/297) (capability roadmap). This research is an initial gate for [#299](https://github.com/Fooftilly/local-search-mcp/issues/299), **not issue completion**.
