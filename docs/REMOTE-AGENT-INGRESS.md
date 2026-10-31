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

| Gate | Evidence required | State |
| --- | --- | --- |
| Confirm Grok API bearer propagation and HTTPS Streamable HTTP | Actual remote client request accepted by authenticated test gateway, with secrets redacted | Not tested |
| Confirm Grok.com custom connector auth options and callback/OAuth support | Actual connector UI flow and endpoint request, no speculative assumptions | Not tested |
| Pin owned provider hostname and authentication mode | Operator-approved URL, tunnel account and per-client auth choice | Not selected |
| Loopback boundary and HTTP Host/Origin integrity | Unauthorized public and invalid Host/Origin requests rejected; bridge still accepts only loopback | Not tested |
| Rejected public traffic does **not** wake Docker | Test missing, expired, revoked, wrong-audience tokens; no change to lease/container count | Not tested |
| Authenticated `tools/list`, read-only capabilities, bounded search/fetch | Same 12 application tools as #215; denied/private-IP and unknown-domain flow unaffected | Not tested |
| Statefulness, SSE, streaming cancellation, rate/body/timeout caps | Live client and chaos-free synthetic tests with precise limits | Not tested |
| Guard outage and endpoint outage fail closed | Isolated unit/integration tests; attended guard evidence without destructive firewall change | Not tested |
| Credential rotation/revocation, clean rollback | Fresh connection denied after revocation; public URL disabled without touching OpenAI tunnel | Not tested |

### Deployment decision

**Do not deploy or expose any new ingress based on this document alone.** The next technical work in #230 is a provider-neutral gateway proof of concept exercised entirely on loopback (synthetic credentials, isolated temporary ports) plus fault/Host/Origin/streaming tests. After the operator chooses an authenticated tunnel provider and verifies which Grok client authentication mode is available, add an explicit opt-in installation/rollback workflow. #231 tracks actual Grok acceptance; #215 is already closed for ordinary MCP-client protocol compatibility.

## Source and maintenance notes

- OpenAI private tunnel: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- Grok hosted custom connector and tunneling: https://docs.x.ai/grok/connectors and https://docs.x.ai/grok/connectors/custom-mcp-tunneling
- xAI API remote MCP: https://docs.x.ai/developers/tools/remote-mcp
- Cloudflare Access MCP/OAuth: https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/secure-mcp-servers/ and https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/
- Existing local security policy: [SECURITY.md](../SECURITY.md), [EGRESS-SYSTEMD.md](EGRESS-SYSTEMD.md), [MCP-INTEROPERABILITY.md](MCP-INTEROPERABILITY.md)

Revalidate external provider documentation before implementation: availability, supported auth modes and hosted connector options may change.
