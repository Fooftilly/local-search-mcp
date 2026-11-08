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

## Read-only installed container identity audit

From a checkout including the follow-up research slice, on the operator's Docker host:

```sh
./.venv/bin/python -m scripts.deep_crawl_host_audit
```

The audit executes one fixed `docker inspect --type=container --format ... crawl4ai` read with an eight-second timeout. It never starts/stops Docker, accesses a source URL, enables deep crawling, or changes any approval. The Docker response contains an environment array that **may include credentials**, but the audit parses it only in memory, never prints environment values, Docker stderr, or raw inspect responses, and exposes only a recognized `CRAWL4AI_ALLOW_DEEP_CRAWL` flag state. Duplicate or malformed flag values are reported as ambiguous/unverified.

The sanitized result identifies the configured image reference, Docker **local image ID** and container state. A local image ID is not a verified registry digest or proof of official image provenance. The report does **not** inspect the live crawler's effective server config, prove DNS/redirect containment, test root-owned egress rules, or verify any per-discovered-URL permission check: these remain explicitly `not_verified`, while `deep_crawl_ready=false` regardless of flag state. A reported flag enabled in the container configuration is a risk signal, **not** permission to initiate an upstream deep crawl.

This is deliberately separate from the existing default-health and opt-in `/config/dump` preflight. Neither check should be used as a substitute for the attended guard and negative-navigation tests.

## Acceptance gate before a proposed implementation

1. Record the installed immutable container image/digest, the opt-in flag's actual state, effective max pages/depth/wall-clock/queue settings, root-owned network egress probe and available navigation hooks. Verify specifically whether a discovered URL can be checked against the original bridge permissions *before* a browser request; assume no until proven.
2. For two separately operator-approved public sites, compare the existing link + explicit batch baseline with any proposed strictly bounded coordinator or safely isolated upstream strategy. Count navigated URLs, precision/coverage over approved scope, wall time, memory/CPU, and unauthorized attempts. No unapproved links may be fetched in a benchmark.
3. Exercise external links, subdomains, cross-host redirects, private DNS/DNS rebinding, expired/revoked one-use grants, exhausted page/depth budgets, paginated loops, offline/asleep backends, and cancellation during an active crawl. A cancelled job must actually stop scheduling and release crawler resources; dropping the response alone is insufficient.
4. Review bounds before enabling any feature: proposed pilot ceiling is **10 pages, depth 1, 2 concurrent jobs, 60 seconds**, with explicit path scope, source/final URL reauthorization, no persistent archive, and opaque temporary results. These are design targets, **not verified enforced limits**.
5. Operator must approve actual flag/config and host-level testing separately. Do not add a 13th MCP tool or alter existing fetch defaults merely because `/config/dump` accepts a strategy.

Related tracking: [#298](https://github.com/Fooftilly/local-search-mcp/issues/298) (bounded URL discovery) and [#297](https://github.com/Fooftilly/local-search-mcp/issues/297) (capability roadmap). This research is an initial gate for [#299](https://github.com/Fooftilly/local-search-mcp/issues/299), **not issue completion**.

## Operator-host evidence and bridge guard (2026-11-07)

The operator's existing Arch Linux installation reported Crawl4AI health version
`0.9.5`, Docker local image ID `sha256:065f475c90ac75fe388141597b0cdba118ebf7e03f59e2a37456c89e6b864c59`,
and `CRAWL4AI_ALLOW_DEEP_CRAWL` **not set**. The fixed authenticated
`/config/dump` probe returned HTTP 400 with **unknown reason**; this is not
evidence that per-discovered-URL checks exist. The installed `/app/config.yml`
reported max pages 100, depth 5, deadline 300 seconds, queue 1,000,
four workers, and no per-principal queue cap. Docker had no CPU, memory or
PID cap. These are an operator-observed configuration snapshot, not tested
bounds for multi-page jobs.

The root-owned nftables live probe passed blocked host-gateway and six reserved
IPv4 destinations plus permitted public DNS/HTTPS. The static on-demand boot
audit passed; the operator also observed ordinary idle sleep and an approved
fresh `web_fetch` waking the crawler without manual Docker startup. This is
**not** evidence of safe full reboot, crash recovery, continuous firewall
enforcement or cancellation of active requests.

As a baseline for a possible coordinator, two previously approved public
homepages were fetched with `include_links=true, fresh=true`:

| Site | Safe link candidates examined | Initially returned | Observation |
| --- | ---: | ---: | --- |
| www.djangoproject.com | 46 | 24 | More links available through existing continuation |
| developer.mozilla.org | 118 | 24 | Input scan capped; not complete discovery |

A separately authorized `web_fetch_batch` returned two child pages for
each site successfully. Those were **explicit** caller-selected URLs; no
autonomous navigation occurred. Single-run elapsed times and the returned
candidate counts are not a quality/latency comparison against safe automatic
deep crawling.

While #299 remains unaccepted, `crawler_security.local_container_warning()`
now rejects a running container with an enabled, malformed or duplicated
`CRAWL4AI_ALLOW_DEEP_CRAWL` setting. An ambiguous set of containers
publishing port 11235 is also refused. The on-demand crawler health gate
rechecks this warning after wake, so normal MCP fetches fail closed rather
than treating the native Docker flag as permission to traverse unknown
links. This guard does not control calls made directly to the Docker API,
make a sleeping container provably safe, or provide per-link authorization.
Do not enable native deep crawling; operator acceptance of a bounded
coordinator, per-URL checks and real cancellation is still outstanding.

## Runtime flag revalidation after container replacement

The earlier import-time `CRAWLER_SECURITY_WARNING` check and on-demand
cold-wake health gate do not cover an always-running bridge with on-demand
mode disabled if the operator replaces the crawler container later.
A bridge initialized while the flag was off must not silently trust that
historical Docker configuration for subsequent fetches.

A follow-up guard checks `local_container_warning()` **after** the
existing version/health gate but **before** each real `/crawl` POST.
Every uncached ordinary or structured fetch passes through that point.
Short-page cache hits do not start a navigation and are unchanged.
For an available **one-use grant**, the bridge also checks current flags
before the irreversible SQLite grant claim; if flags are unsafe, no grant
is consumed or browser navigation started. The pre-POST check remains for
changes after that earlier check. These are independent observations, not
an atomic transaction across Docker and the approval database. The guard
never changes Docker configuration or approvals.

Trade-off: for a Docker-backed bridge this adds two bounded local Docker
queries per actual crawl; a one-use grant attempt performs another two
before consuming the grant. Existing on-demand wake checks remain in place,
since they additionally verify backend identity and live firewall
containment before releasing a request. When Docker is absent or its
query fails, the existing `local_container_warning()` helper returns
`None`: this check is **not** proof of Docker ownership, external server
safety, continuous egress enforcement, or a substitute for the root-owned
firewall probe. An upstream replacement between the final check and
navigation still needs host-side containment.
