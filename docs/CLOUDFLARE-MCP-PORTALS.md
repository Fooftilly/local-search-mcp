# Cloudflare MCP Server Portals for self-hosted local-search-mcp (#230, #231, #246)

**Status: architecture investigation only; no Cloudflare resource, hostname, token, DNS, tunnel, connector or service has been created or changed.** Each operator uses their own Cloudflare account, private tunnel, domain and credentials; never put a real deployment hostname or secret in this repository.

Cloudflare's [MCP Server Portals](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/) reached general availability on September 24, 2026. The service provides a Cloudflare Access-authenticated MCP endpoint, upstream tool discovery, tool allowlisting and portal activity logging. Unlike a raw published application tunnel, the portal can expose only a **Cloudflare-protected portal endpoint**, while Cloudflare Gateway privately reaches an origin MCP service over a private hostname/CIDR route (released September 22, 2026).

## Proposed separate trust boundaries

```text
Hosted MCP client (Grok if its actual OAuth flow is compatible)
  -> operator-owned Cloudflare Access MCP Portal HTTPS /mcp
     OAuth2 sign-in + Access user/group/service-token policy
  -> portal's private upstream credentials
  -> Cloudflare Gateway private-server routing over operator Cloudflare Tunnel
  -> fixed authenticated local-search-mcp gateway (127.0.0.1:18790)
  -> existing loopback FastMCP (127.0.0.1:8790; 12 research tools)
```

This is **not yet a deployable path**. A private hostname/CIDR service reachable by Cloudflare Gateway must first be mapped **safely to the loopback-only gateway**. The Cloudflare cloud cannot directly reach the user's `127.0.0.1`; do not solve this by binding port 18790 to `0.0.0.0`, opening arbitrary LAN access, publishing 8790, or changing the egress guard. Validate Cloudflare private routing, proxy/Host handling and root-guard enforcement on an actual host before activation.

## Relevant verified Cloudflare behavior

- **Private origins:** add an MCP server in Zero Trust > Access controls > MCP Portals, enable **Route traffic through Cloudflare Gateway**, and connect the host using a **private** Cloudflare Tunnel hostname or CIDR route. This avoids publicly publishing the origin MCP service. An upstream that requires OAuth still needs publicly accessible authorization/token endpoints; a *static bearer* upstream does not need this OAuth infrastructure.
- **Static bearer upstream:** Cloudflare's MCP-server API supports `auth_type: bearer` with `auth_credentials`; Cloudflare will send `Authorization: Bearer ...` to the upstream. This potentially matches the existing agent gateway's private, revocable client grant. Provision a **portal-specific** credential via the trusted, local operator TTY; disclose it only to the authorized Cloudflare dashboard/API flow, never argv, GitHub, application logs or repo files. Keep the existing OpenAI tunnel unrelated.
- **Client auth:** Cloudflare's portal supports a managed OAuth2 authorization-code flow with `401 WWW-Authenticate` for MCP clients and Access service tokens for supported machine clients. These are **independent** of the upstream bearer. Hosted Grok.com custom connector OAuth compatibility and account permissions remain **unverified**; do not claim an accepted Grok connection from documentation alone.
- **Policies:** configure Access policies on **both** the portal and upstream server. Per-user identity and authorization reside at Cloudflare Access. A shared upstream bearer will make the gateway observe **one portal client ID** rather than distinct Grok/other end users; `X-Forwarded-User-Agent` is client-controlled and must not be treated as authorization identity. For service-token automation, both portal and upstream server require matching Service Auth policies, and per-user upstream OAuth is not supported.
- **Tool contract changes:** the portal namespaces upstream tool names with the server ID and adds portal-native `portal_*` management tools. The 12 *upstream* definitions are not necessarily exposed as exactly the same 12 *client-visible* names. Disable Code Mode initially and verify the actual renamed tool names, `inputSchema`, `structuredContent`, `outputSchema`, `isError` and SSE behavior via a real client. Portal namespace and management tools should be accepted explicitly rather than weakening original FastMCP schemas.
- **Audit and privacy:** Cloudflare portal logs record capability, server, status, duration and time. Optional Gateway HTTP logging/DLP may inspect HTTP content. Logpush export is marked Enterprise-only. Review Cloudflare logging, data processing and retention settings before allowing sensitive research; retain the gateway's local query/token avoidance.
- **Cautions:** portal homepage and endpoint hostname may be discoverable publicly; this is a protected endpoint, **not a hidden URL**. Blocked users may still access an upstream's *direct* URL if it is separately exposed; keep upstream entirely private. Policy limitations include independent MFA, purpose justification and temporary authentication not being enforced at the **upstream server authorization** step through a portal. Access portal policies still apply. Cloudflare documents that both the portal and individual server need proper policies.
- **Availability:** announced generally available to Cloudflare customers; confirm the actual account's entitlement, Cloudflare Gateway routing availability/limits and any charges in the operator dashboard before provisioning. Do not assume private-network Gateway support is free for every usage pattern.

## Setup wizard recommendation (#246)

Do **not** conflate these separate modes:

1. **Cloudflare MCP Portal — preferred evaluation first for OAuth-capable hosted clients:** read-only prerequisites and an operator checklist for an active Cloudflare zone, Zero Trust identity provider, tunnel/private routing, Access policies, upstream bearer and actual client OAuth; avoid automatic cloud writes until API permissions, secret injection, idempotency and rollback are reviewed. A future attended setup can register a portal/server via Cloudflare API using a *scoped* operator token without logging it. Any dashboard-managed configuration must not be overwritten by a YAML editor.
2. **Direct named-tunnel published route — fallback:** for clients without compatible portal OAuth but with verified support for the local bearer. The [generic tunnel example](CLOUDFLARE-REMOTE-MCP.md) provides exact `/mcp` routing to 18790 with 404 fallback and explicit gateway auth. This makes the gateway URL Internet-reachable and requires stricter public-edge security review; it does not replace a Cloudflare portal.
3. **No tunnel:** keep the original ChatGPT secure tunnel and local-only access unchanged.

Always default to **read-only preview**. Any local grant creation, tunnel edit, Cloudflare API write, DNS update, systemd service startup or public exposure must be a separately and explicitly authorized action with auditable rollback. Store provider configuration only under the user's private local directory, not repo source/tests/issues.

## Acceptance before deployment

- Run existing `python -m remote_agent_server --check` and `--preflight` locally.
- Check the private routing reaches the **guarded** gateway and never 8790/8791/8888, without creating public reachability or opening LAN-facing listeners.
- Authenticate actual Grok custom connector via Cloudflare Access OAuth **without** bypassing policy; reject unauthenticated/unauthorized clients and ungranted tools.
- Verify the portal can load/upstream-authenticate the research tool inventory; test actual client tool-name namespace, optional parameters and error/result shapes.
- Revoke Cloudflare portal access and portal-specific upstream grant separately; ensure old OpenAI tunnel stays usable.
- Confirm account entitlements, logging/retention, operational rollback and sleeping-backend behavior.

## Sources (Cloudflare official)

- [MCP Server Portals](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/) — managed OAuth, bearer upstream API, tool names, Access policies and known limitations (updated October 2026).
- [Private MCP server support](https://developers.cloudflare.com/changelog/post/2026-09-22-private-mcp-servers/) — Gateway routing over private Cloudflare Tunnel.
- [General availability](https://developers.cloudflare.com/changelog/post/2026-09-24-mcp-portals-ga/) — released to Cloudflare customers.
