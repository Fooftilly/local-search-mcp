# Grok MCP client acceptance (#231)

**Status: preparation only.** No Grok client, public endpoint, HTTPS tunnel, credential or deployment has been verified or provisioned by this change. The separate authenticated ingress and its operator approval remain tracked by [#230](https://github.com/Fooftilly/local-search-mcp/issues/230). The repository-level baseline for generic FastMCP clients is already recorded in [MCP interoperability](MCP-INTEROPERABILITY.md) (#215); do not repeat or reopen that work merely to satisfy this issue.

## Three different clients, three different trust boundaries

| Client | Documented mechanism | Safe scope now | Outstanding acceptance |
| --- | --- | --- | --- |
| Grok CLI, **on the same trusted computer** | `grok mcp add --transport http NAME URL`, `grok mcp list --json`, `grok mcp doctor NAME --json`; CLI also documents OAuth and repeatable static headers | Existing loopback `http://127.0.0.1:8790/mcp` only; this is intentionally unauthenticated to same-host software | Whether the CLI is installed, local tools appear, requests/results decode correctly |
| Grok website custom connector | `grok.com/connectors` > New Connector > Custom, public HTTPS MCP URL, authentication as offered by the live UI | Read-only check of connector availability/authentication UI, **without adding a URL** | Account/role permission, exact OAuth/static-token UX, supported redirect/error handling and a fully secured public ingress |
| xAI Responses API remote MCP | A `server_url` plus an optional `authorization` value forwarded in the MCP Authorization header | Documentation review only | A separately approved public HTTPS gateway, client-specific secret and real API acceptance |

These products **must not** be treated as interchangeable: CLI static headers do not establish that the hosted Grok website accepts static bearer tokens. The existing OpenAI Secure MCP Tunnel and `CONTROL_PLANE_API_KEY` are not reusable authorization for Grok.

Provider documentation (revalidate at execution):
- https://docs.x.ai/build/features/mcp-servers
- https://docs.x.ai/grok/connectors
- https://docs.x.ai/grok/connectors/custom-mcp-tunneling
- https://docs.x.ai/developers/tools/remote-mcp

## Phase A: non-deploying local CLI acceptance

Only if Grok CLI is **already installed on the same trusted host** and the original local MCP service is running:

```sh
command -v grok
grok mcp list --json
./.venv/bin/fastmcp list http://127.0.0.1:8790/mcp --auth none
```

Inspect the existing Grok MCP inventory **before** adding anything. `grok mcp list --json` may contain account/server configuration; do not paste raw output into GitHub. The local FastMCP check is the known-good server baseline, not proof of Grok compatibility. No crawler wake, tunnel change, guard bypass, token generation or system service change is needed for discovery.

If the operator approves a **local-only Grok CLI configuration change** and the name is not already in use:

```sh
grok mcp add --transport http local-search-loopback http://127.0.0.1:8790/mcp
grok mcp doctor local-search-loopback --json
grok mcp list --json
```

The add command writes Grok's own configuration and must not be run by CI or by setup/update automation. This loopback URL works only for a CLI on the same machine; it is not the protected gateway at port 18790 and must never be presented as the hosted Grok.com connector URL. Run one **read-only** `web_search_capabilities` call through Grok's MCP UI (`/mcps`). A `status=unavailable` result while the search backend sleeps is acceptable; do not wake Docker merely to make discovery green. Separately, with explicit operator permission, run one bounded public `web_search` and one already-approved public `web_fetch`. Verify result type and error handling, not just the model's summary. For rollback of this newly created test entry:

```sh
grok mcp remove local-search-loopback
```

Do not remove or overwrite any pre-existing Grok server with that name. Never create a project-scoped `.grok/config.toml` containing private credentials or publicly accessible URLs merely for this test.

Operators who choose Cloudflare Tunnel must configure their **own** DNS subdomain and independently authenticated gateway, using private settings kept outside this repository. Follow [Self-hosted Cloudflare remote MCP ingress](CLOUDFLARE-REMOTE-MCP.md) before attempting Phase B. This project provides no shared tunnel or hosted endpoint; do not substitute reachability for an authenticated Grok connector.

## Phase B: hosted Grok and xAI API (blocked by #230)

1. Confirm the actual `grok.com/connectors` **Custom** option and the available authentication options in the account. Business/Enterprise teams may require administrator provisioning. Observe the UI without registering a server or adding credentials yet.
2. Finish #230's independent public ingress security and deployment decision. The only permissible public target is a **separately authenticated** HTTPS MCP gateway with a fixed, local upstream and verified guard, identity/revocation, rate limits, Host/Origin policy and rollback. Operator authorization is mandatory for credentials, DNS, tunnel and service installation.
3. Once an approved gateway exists, use **separate credentials** for each client. Verify that Grok.com really sends a usable auth credential on `initialize`, `tools/list`, `tools/call` and any streaming follow-ups. Do not assume the CLI or xAI API's Authorization support implies hosted-web support.
4. Prefer OAuth 2.1/OIDC where the hosted connector supports it, with actual audience, issuer, expiry and signature checks at the enforcing boundary. Do **not** expose the original `8790` bridge, `8791` dashboard or the local `18790` gateway via an unauthenticated quick tunnel. Do not weaken local `MCP_ALLOWED_HOSTS`, `MCP_ALLOWED_ORIGINS`, nftables or domain approvals.
5. If testing the xAI API separately, verify an actual documented `authorization` header is received at the gateway, with a dedicated revocable client grant. The public HTTPS URL must be obtained from the approved deployment, never guessed.

## Minimal, sanitized client acceptance evidence

Expected **12** registered tool names on the reviewed bridge revision:

`web_search`, `web_search_capabilities`, `web_search_batch`, `web_fetch`, `web_fetch_batch`, `web_fetch_page`, `web_table_page`, `web_links_page`, `web_find`, `web_extract_section`, `web_compare_documents`, `web_metrics`.

For **each** actually tested client, record a small result matrix (PASS / FAIL / NOT RUN), client/version, local vs hosted transport, reviewed Git commit, whether the same deployed revision is in use, the exact *tool-name set*, and sanitized symptoms of any mismatch:

| Check | Acceptance | Safety condition |
| --- | --- | --- |
| Discovery | `tools/list` exposes the 12 expected tools with applicable `inputSchema`/optional defaults | No search or backend wake needed |
| Read-only capabilities | `web_search_capabilities` returns a structured status, including asleep/unavailable | No privileged changes |
| Bounded public search | One approved, small `web_search`; verify default `title,url,snippet,engine` shape and optional bool/array input | Explicit operator approval for active query |
| Rejected private target | `web_fetch` to `http://127.0.0.1/private` is blocked, with no content | Never route the tool into localhost |
| Approved public fetch | One already-approved public URL returns bounded, typed data | No domain-policy edits or unapproved domains |
| Result/error semantics | Record `outputSchema` presence, `structuredContent` shape, `isError`, optional bool/array coercion, HTTP vs tool errors | Capture only schema/field summaries; no private bodies |
| Remote authorization | Missing, invalid, expired and revoked clients are refused *before* any upstream forwarding; active stream revocation works | Hosted tests only after secured endpoint approval |
| Identity/privacy | Distinct per-client sanitized audit identity; no raw token, user query, private URL or approval history in logs | Do not post credential/config captures |
| Original services | Existing ChatGPT/OpenAI tunnel and operator dashboard remain available; domain approvals and crawler guard remain intact | No public dashboard access or guard change |

A failure must be attributable to the **specific client/transport** before changing generic MCP tool schemas, FastMCP defaults or the guard. Do not claim #231 complete until both the supported Grok web path and the requested installed Grok CLI path have real-client evidence, or until an explicitly documented unavailable client is formally scoped out by the maintainer.

**Evidence limitation:** repository CI can verify that all 12 definitions survive forwarding through the isolated authenticated ASGI gateway. CI cannot prove a real Grok account's connector UI, hosted OAuth flow, remote egress, Grok CLI installation or host guard state.
