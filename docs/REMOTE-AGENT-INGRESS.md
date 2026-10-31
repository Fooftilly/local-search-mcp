# External agent MCP ingress — architecture and security gate (#230)

**Status: design only; not deployable.** This document is the security decision record for a future **separate, opt-in**, authenticated HTTPS ingress serving hosted clients such as Grok. No public listener, tunnel, token, DNS record, service, Docker container, firewall rule or approval is provisioned by this change. The existing OpenAI Secure MCP Tunnel stays installed and unchanged.

## Verified starting point and client requirements

- The FastMCP Streamable HTTP service is local-only at `http://127.0.0.1:8790/mcp`, without built-in authentication. `SECURITY.md` documents its intentionally narrow loopback Host/Origin checks. The domain approval dashboard (`127.0.0.1:8791`), SearXNG and Crawl4AI must remain inaccessible to third parties. It is **unsafe** to run `cloudflared tunnel --url http://127.0.0.1:8790` or `ngrok http 8790` with no authenticated gateway in front.
- Existing `tunnel-client` authenticates outbound to the **OpenAI** control plane; its endpoint is for supported OpenAI products, **not** an independently portable public Grok connector credential. Do not share its `CONTROL_PLANE_API_KEY` or tunnel profile, or reuse it as the new agent identity.
- [Grok custom MCP connectors](https://docs.x.ai/grok/connectors) require a publicly reachable URL. [Grok's tunneling instructions](https://docs.x.ai/grok/connectors/custom-mcp-tunneling) explicitly say a tunnel supplies reachability, *not* authentication. Its quick-tunnel examples are unsuitable against the current unauthenticated loopback bridge.
- [xAI Remote MCP Tools](https://docs.x.ai/developers/tools/remote-mcp) supports Streamable HTTP/SSE and, **for xAI API clients**, an `authorization` value forwarded in the request's Authorization header (plus optional extra headers and tool allowlisting). The Grok **website's** custom connector authorization modes/identity UX must be observed separately; do not assume the same API-level bearer/header support.
- [Cloudflare Access Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/) can adapt OAuth-capable clients, but its authentication requires a supported end-to-end token verification model. In particular, enabling Managed OAuth **without** a validating origin is not a security solution. Avoid browser-only redirect protection for machine MCP clients, which may receive unusable HTTP 302 responses.

## Recommended architecture, contingent on client/auth acceptance

```text
        hosted Grok / remote agent
                |
         HTTPS Streamable HTTP
                |
       stable owned HTTPS hostname
                |
       outbound-only managed tunnel
                |
      [private local auth gateway]
         - deny by default
         - independent agent identity
         - fixed /mcp route only
         - bounded proxy / streaming
                |
      127.0.0.1:8790/mcp
                |
      existing FastMCP + approval policy
       existing guarded Crawl4AI/SearXNG

    OpenAI Secure MCP Tunnel -> existing private MCP path (unchanged)
    127.0.0.1:8791 dashboard -> loopback operator only
```

**Choice 1 — narrow pilot for xAI API:** use an owned stable HTTPS hostname and an outbound-only tunnel terminating at a locally bound, **independently authenticated** MCP gateway. Accept a per-client, random, revocable bearer token only over TLS. Store token verifier material in an operator-owned mode-0600 secret store (not Git or a URL query string); compare securely, reject missing/invalid/revoked credentials **before** any HTTP forwarding or backend lease, and use no token in log output. A public name, tunnel account, and client token are operator-chosen at deployment time. Require a separate auth test confirming xAI API really forwards Authorization end-to-end; do **not** turn on a public tunnel merely because this option exists.

**Choice 2 — preferred for Grok website when supported:** a proper MCP OAuth 2.1/OIDC flow with protected-resource metadata, scoped access tokens, issuer/audience/expiry validation at the enforcing gateway, user-grant revocation, and validated callbacks. Cloudflare Managed OAuth is a candidate **only if** the gateway verifies its Access JWT/token or a trusted auth layer does so on every request. Verify Grok's actual auth flow and interoperability before choosing. Avoid a generic, unauthenticated quick tunnel or reliance on Cloudflare's browser login redirect for non-browser clients.

**Choice 3 — no public ingress:** for Hermes, Cursor or Grok CLI on the owned host, use `127.0.0.1:8790/mcp` directly. For a trusted local/private second machine, use a tightly scoped SSH local-port forward or an authenticated private overlay tunnel. Neither allows hosted Grok's servers to reach localhost, but neither creates a new Internet-facing attack surface.

Do not implement multiple competing public gateways. Select one authenticator and one tunnel after the client acceptance matrix is measured. Keep all choices reversible and separately credentialed from the existing OpenAI tunnel.

## Security requirements for the selected public ingress

1. **Authentication is before forwarding, not a UI permission:** a valid client identity must be required on every request, including `initialize`, `tools/list`, `tools/call`, session follow-ups, and streaming/event endpoints. Never let an authenticated session identifier replace authorization by itself.
2. **Audience and policy separation:** client A's token must not work for B; allow independent rotation/revocation and a short expiry where client capabilities permit. Do not publish private request data, domain approvals or query history in logs. Don't treat `allowed_tools` passed by a client as a security policy; enforce any future server-side tool restrictions in the gateway.
3. **Fixed, limited upstream:** the only allowed upstream is `http://127.0.0.1:8790/mcp`. No dynamic target, alternate path, path traversal, CONNECT, HTTP forward proxy, `8791` dashboard, SearXNG port `8888`, Crawl4AI port `11235`, tunnel diagnostics, or arbitrary localhost URLs. Deny unexpected HTTP methods and upgrades; preserve the MCP-required POST/GET/DELETE and streaming semantics according to the negotiated protocol (avoid restricting them prematurely without client tests).
4. **Host/Origin integrity:** allow only the exact public hostname at the **external ingress**, deny unsolicited browser Origins and invalid Host/SNI. Bind the local auth gateway to loopback only; it must emit an upstream loopback Host/Origin acceptable to the existing bridge without expanding `MCP_ALLOWED_HOSTS` or `MCP_ALLOWED_ORIGINS`. Ignore untrusted forwarded host/client IP headers unless injected by a verified local tunnel; no wildcard origin or permissive CORS.
5. **Resource exhaustion:** bound header/body size, concurrency, requests/minute per client, idle/stream lifetime, request deadline, and open sessions using explicit documented defaults. Bound MCP JSON-RPC and response streaming without buffering unbounded events; no leaked errors, credentials, session IDs, or private query content in audit/health responses.
6. **Runtime containment:** reuse the already-installed root-owned nftables crawler isolation and guarded on-demand preflight/live probe. A **new** externally accessible tunnel must fail closed at startup if the local guard/bridge cannot be proven ready; a healthy OpenAI tunnel does not authorize bypassing these checks. Preserve rollback and the known limit: the installed root gate checks tunnel startup, not continuous protection after firewall rules are removed. Assess runtime guard-loss handling before live public acceptance.
7. **Operator ownership:** preflight/preview is read-only. Any installation, credential provisioning, Cloudflare/ngrok/DNS enrollment, firewall/systemd change, OAuth registration or public exposure requires explicit operator authorization and a reviewed rollback path. Keep the current OpenAI path untouched throughout.

## Acceptance matrix / exact gates

The twelve checks below are independent acceptance gates. **All are pending**; a passing design-document CI run is not proof that any live public endpoint is secure.

| Gate | Evidence required | State |
| --- | --- | --- |
| G01 — xAI API bearer propagation and HTTPS Streamable HTTP | An actual API client forwards the Authorization header to a local test gateway over authenticated HTTPS, with secrets redacted; MCP initialization succeeds | Not tested |
| G02 — Grok.com connector authentication | Operator verifies the custom connector's real OAuth/credential, callback, and refresh behavior; do not assume API bearer semantics also apply to Grok web | Not tested |
| G03 — Operator-owned ingress selection | Operator approves the specific stable hostname, tunnel account, credential/auth model, and rollback plan before provisioning | Not selected |
| G04 — Route and upstream isolation | Both unauthenticated **and authenticated** requests to non-`/mcp` routes, alternate upstreams, operator dashboard, crawler, search and diagnostics fail without forwarding; no open proxy | Not tested |
| G05 — Host/Origin and loopback integrity | Reject invalid external SNI/Host/Origin and spoofed forwarding headers; verify upstream Host stays loopback-only and existing bridge Host/Origin protections remain enabled | Not tested |
| G06 — Rejected requests cannot wake backends | Missing, invalid, expired, revoked, wrong-client and wrong-audience credentials fail **before** proxying; on-demand Docker state and lease counters remain unchanged | Not tested |
| G07 — Authorized MCP behavior and policy enforcement | Authenticated `initialize` / `tools/list` and bounded search/fetch return the existing 12 tools; private-IP rejection and operator-only domain approval stay enforced | Not tested |
| G08 — MCP sessions and streaming | POST/GET/DELETE lifecycle, session IDs, SSE/event streams, cancellation and client disconnect work with authentication checked on every request; session IDs alone grant nothing | Not tested |
| G09 — Request/resource limits | Bounded header/body sizes, per-client rates, concurrency, active sessions, stream lifetimes and deadlines reject excess work safely; test slow and malformed clients | Not tested |
| G10 — Guard and upstream failure modes | Missing/mismatched root egress guard, failed preflight/live probe, absent bridge and tunnel restarts fail closed in isolated tests; attended host check without destructive firewall changes | Not tested |
| G11 — Credential lifecycle and rollback | Independent client identity, rotation and revocation block **new and existing** sessions as designed; disabling public ingress leaves existing OpenAI tunnel and local MCP unaffected | Not tested |
| G12 — Audit and secret confidentiality | Authorized/rejected requests produce bounded client-identity/status audit events without bearer tokens, MCP session IDs, private queries, URLs or response bodies; errors and health pages leak no secrets | Not tested |

### Import-only loopback gateway proof (#230)

The next development slice adds `remote_agent_gateway.py` and `tests/test_remote_agent_gateway.py`. This is **only a testable ASGI class**, not a production or exposed ingress. It has no listener, CLI, systemd service, configuration file, provisioned tunnel or credentials. No third-party agent can connect to it as delivered.

```sh
./.venv/bin/python -m unittest -v tests.test_remote_agent_gateway
```

Using httpx's in-memory ASGI and mock upstream transports, the tests exercise synthetically hashed/expiring bearer credentials, missing and duplicate Authorization headers, rotation, route and Host/Origin restrictions, public-HTTP headers stripped at the MCP hop, size/method limits, GET streaming, session DELETE, redirect rejection, unavailable upstream, and injected guard unavailability. The gateway forwards only fixed `/mcp` to `127.0.0.1:8790`; it does **not** itself certify the host's installed nftables or integrate a real external provider.

**Resource limit and revocation prototype:** `ClientRequestBudget` now enforces 30 admitted requests per rolling 60 seconds and at most two simultaneous requests per client (eight total per gateway process). Requests that exceed a limit receive HTTP 429 **before** upstream work. Slow bodies have a 15-second assembly deadline, and an active response stream has a maximum lifetime of 120 seconds. In-progress streams periodically recheck whether their exact token digest and client ID are still present and unexpired; revocation closes the upstream stream. The rate history retains up to 64 recently seen client identities (independent of the eight currently authorized grant slots) so ordinary token/client rotations do not reset a reinstated client's recent rate quota or block a newly authorized replacement. Very high rotation churn beyond this bounded cache fails with 429 until entries expire. All quotas are per single ASGI event loop/process, not shared across multiple workers. These tests use synthetic identities and mocked network transports.

**Known incomplete gates, preventing deployment:** the standalone gateway still permits test-injected callbacks, while the newer `open_guarded_runtime` factory composes the private credential reader and fixed root-owned guard probe without starting a server. No operator credentials have been provisioned or any runtime deployed. Distributed client quotas, provider identity/audience binding, production audit events, external TLS/tunnel authentication, OAuth, and deployment/rollback remain unimplemented. An upstream that stalls **before response headers** still requires a separately enforced total response-start deadline at the eventual deployment boundary. The response-lifetime check is a maximum duration per response rather than a throughput-aware idle timeout. Do not wrap this class with a public tunnel, add a production `uvicorn` launcher, or declare #230 complete until those remain safe and tested. Host bind must remain loopback-only.

### Read-only credential store and host-guard adapters (#230)

The next slice adds `remote_agent_prerequisites.py` and `tests/test_remote_agent_prerequisites.py`. They are **library-only** components with no startup behavior or installation. The non-serving `open_guarded_runtime` factory now injects both into `LoopbackMCPGateway(grants=..., guard_ready=...)`; **no server runner is implemented or enabled here**.

**Credential file validation:** `load_client_grants(path)` requires an explicitly supplied **absolute path**, a private operator-owned parent directory, a single regular non-symlink mode-0600 file owned by the current effective user, one link, and a maximum of 8 KiB of well-formed UTF-8 JSON. Parent path components cannot be symlinks or untrusted writable directories. Missing, malformed, truncated, unsafe or invalid files fail closed. The strict `local-search-mcp/agent-grants/v1` format is:

```json
{
  "schema": "local-search-mcp/agent-grants/v1",
  "grants": [
    {
      "client_id": "example-client",
      "token_sha256": "<64 lowercase hexadecimal SHA-256 characters>",
      "expires_at": 2000000000
    }
  ]
}
```

This is a **schema illustration, not an installed credential**. Never put a raw bearer token or any real verifier in this repository, an issue, a PR or an operator dashboard URL. A future installation workflow must generate cryptographically strong **per-client random tokens**, present each secret securely once to its authorized owner, store only the digest in the operator-owned file, and atomically replace the file for rotation/revocation. Each request reloads validated digests without an in-process credential cache, so revocation applies without service restart (active streams are independently rechecked by the gateway). The loader rejects duplicate JSON keys, IDs, hashes, invalid timestamps, and unknown fields.

**Existing guard integration boundary:** `fixed_root_tunnel_gate()` uses only the pre-existing fixed sudoers-approved `sudo -n /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container crawl4ai --tunnel-gate`. It cannot install rules, modify services, or choose a target. It requires exit status zero and the expected `PASS:` result; any missing file, missing permission, timeout, mismatched guard or unsuccessful probe yields **False**. It calls a root-owned, operator-installed script, not a mutable checkout. This check is safe and non-mutating, but can be expensive (live probes), so any future ASGI runner must schedule it off the event loop with bounded concurrency, a tested request deadline, and no fail-open cache. The existing guard is **not** a continuously active firewall-loss detector.

Run isolated regression tests with `python -m unittest discover -s tests -p 'test_remote_agent_prerequisites.py' -v`. Tests use a private temporary directory, synthetic digests and mocked root subprocess calls; no real sudo, Docker, nftables, public endpoint or secret changes occur.

**Not yet solved:** public tunnel and provider authentication, secure one-time credential provisioning and protected updates, a bounded production runner, end-to-end secret/audit lifecycle, and live hosted-Grok acceptance. These adapters alone are insufficient to complete gates G01–G12 or to deploy the gateway.

### Bounded, non-serving gateway runtime composition (#230)

`remote_agent_runtime.open_guarded_runtime(credentials_path)` now composes the import-only gateway, strict `load_client_grants` file loader, and existing fixed `fixed_root_tunnel_gate` verifier. It **does not bind any port, launch Uvicorn, register a user service, provision credentials, or start a tunnel**. This factory must not be presented as a completed public ingress.

For every authenticated, quota-admitted request, the gateway now finishes bounded input validation **before** performing a fresh root guard verification. A single-flight task and lock serialize root probes off the event loop with a 32-second caller deadline. Importantly, **if the deadline expires or a caller disconnects, the underlying worker remains tracked until it actually exits**; later requests receive HTTP 503 rather than starting overlapping probes. Failure and timeout return HTTP 503 without starting MCP work. This prevents a root-owned Docker/nftables probe from synchronously blocking the ASGI event loop. It does **not** cache firewall success or consider any previously passing probe permanent. An admitted request still occupies a quota slot during guard verification. (Existing root guard may probe for up to 30 seconds itself.)

The factory owns an `httpx.AsyncClient` with `trust_env=False`, redirects disabled, maximum eight connections and bounded connect/write/read/pool timeouts. The only upstream remains the gateway's hardcoded `http://127.0.0.1:8790/mcp`; arbitrary URLs, dashboard routes or proxy environment variables are never used. The context manager closes its client when the factory context ends. Tests use in-memory ASGI and mock HTTPX transports; a blocking fake root probe demonstrates that the event loop remains responsive while the probe runs, that concurrent probes are serialized, and that missing/invalid/expired credentials are rejected before any root subprocess or upstream work.

**Important remaining boundaries:** this is still **not a runnable service**. The future opt-in server must bind exclusively to an inspected loopback address, enforce one process/worker while using in-memory quotas, implement safe credential generation and atomic rotation, and protect against clients directly addressing the unauthenticated local FastMCP endpoint. A production ingress also needs an authenticated, provider-compatible public tunnel/OAuth choice, independent client identity, bounded audit logging, no raw secrets, live guard-loss handling, deployment and rollback, and full Grok API / Grok.com acceptance. No existing OpenAI tunnel or root-owned firewall configuration was changed.

Run: `python -m unittest discover -s tests -p 'test_remote_agent_runtime.py' -v`.

### Deployment decision

**Do not deploy or expose any new ingress based on this document alone.** The next technical work in #230 is a provider-neutral gateway proof of concept exercised entirely on loopback (synthetic credentials, isolated temporary ports) plus fault/Host/Origin/streaming tests. After the operator chooses an authenticated tunnel provider and verifies which Grok client authentication mode is available, add an explicit opt-in installation/rollback workflow. #231 tracks actual Grok acceptance; #215 is already closed for ordinary MCP-client protocol compatibility.

## Source and maintenance notes

- OpenAI private tunnel: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- Grok hosted custom connector and tunneling: https://docs.x.ai/grok/connectors and https://docs.x.ai/grok/connectors/custom-mcp-tunneling
- xAI API remote MCP: https://docs.x.ai/developers/tools/remote-mcp
- Cloudflare Access MCP/OAuth: https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/ and https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/
- Existing local security policy: [SECURITY.md](../SECURITY.md), [EGRESS-SYSTEMD.md](EGRESS-SYSTEMD.md), [MCP-INTEROPERABILITY.md](MCP-INTEROPERABILITY.md)

Revalidate external provider documentation before implementation: availability, supported auth modes and hosted connector options may change.
