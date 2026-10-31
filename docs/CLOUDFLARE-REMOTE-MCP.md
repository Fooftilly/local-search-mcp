# Cloudflare Tunnel transport for hosted MCP (#230, #231)

**Decision: operator intends to use a Cloudflare-owned-domain subdomain and `cloudflared`. Status: configuration example and review gates only; NOT DEPLOYED.** No actual domain, tunnel UUID, Cloudflare account, credentials, client authentication mode, endpoint, DNS or service has been provided or created. The existing OpenAI Secure MCP Tunnel is independent and stays unchanged.

This is a *transport* choice, not approval to publish the gateway yet. Hosted Grok.com Custom MCP authentication remains unverified. The [ingress security decision](REMOTE-AGENT-INGRESS.md) and [Grok client acceptance](GROK-MCP-ACCEPTANCE.md) remain authoritative for fail-closed requirements.

## Required routing boundary

```text
Hosted Grok / xAI API (when separately authenticated and accepted)
  -> HTTPS mcp.<operator-domain> /mcp
  -> Cloudflare edge, exact hostname/path policy and optional supported Access
  -> cloudflared (outbound-only, operated by owner)
  -> authenticated 127.0.0.1:18790/mcp (per-client bearer verifier + root guard)
  -> original 127.0.0.1:8790/mcp (12-tool FastMCP)
```

Never point Cloudflare to the original unauthenticated `8790` bridge, approval dashboard `8791`, SearXNG `8888`, Crawl4AI `11235`, or tunnel diagnostics. No wildcard hostname, public TCP port, URL-selected upstream, query-string token or shared OpenAI control-plane key is needed. Cloudflare does **not** remove the requirement for the local gateway to authenticate every request.

## Locally-managed named-tunnel example (review only)

Replace `mcp.example.com`, `YOUR-TUNNEL-UUID` and `/private/path/to/tunnel-credentials.json` **locally after operator approval**. This example is intentionally not executable without real operator-provided details:

```yaml
tunnel: YOUR-TUNNEL-UUID
credentials-file: /private/path/to/tunnel-credentials.json

ingress:
  - hostname: mcp.example.com
    path: "^/mcp$"
    service: http://127.0.0.1:18790
    originRequest:
      httpHostHeader: 127.0.0.1:18790
      connectTimeout: 5s
  - service: http_status:404
```

**Why the Host rewrite is essential:** the hardened local gateway accepts a loopback HTTP Host, not the Internet-facing subdomain. Cloudflare's documented `httpHostHeader` option replaces the Host header that `cloudflared` sends to the origin. This does **not** relax the gateway's Host/Origin checks. Cloudflare must separately enforce the original public hostname at the edge and tunnel ingress rule. The exact `^/mcp$` path and 404 catch-all reduce accidental routing but are **not** substitutes for gateway-level method, raw-path, query-string, Host/Origin and authentication validation. Cloudflare matches path rules without rewriting the forwarded path. Test path normalization and encoded path variants against the installed `cloudflared` version before deployment.

For a **remotely managed tunnel** (configured through the Cloudflare dashboard), create the analogous published application route only after approval: exact hostname; restricted `/mcp` path if the UI supports it; service `http://127.0.0.1:18790`; HTTP Host Header `127.0.0.1:18790`; explicit unmatched 404. Confirm that the dashboard generates the intended ingress routing; do not assume locally managed YAML is in effect for a remotely managed tunnel.

## Read-only checks before deployment approval

Run these checks on the **owner host** with the reviewed checkout, after the operator has deliberately provisioned the private credential store:

```sh
python -m remote_agent_server --check
python -m remote_agent_server --preflight
```

The first validates private grants without the root probe; the second invokes the already-authorized fixed read-only root egress guard and the original local MCP `tools/list` with a bounded request. Neither starts a gateway nor publishes a tunnel. Do not interpret a missing grants file as a reason to auto-create or weaken permissions.

For a locally managed draft config, once the operator supplies a real local config path and **before** publishing DNS, use the installed `cloudflared` binary's non-deploying ingress checks (the exact `--config` form may depend on installed version):

```sh
cloudflared tunnel --config /path/to/reviewed-config.yml ingress validate
cloudflared tunnel --config /path/to/reviewed-config.yml ingress rule https://mcp.example.com/mcp
cloudflared tunnel --config /path/to/reviewed-config.yml ingress rule https://mcp.example.com/dashboard
cloudflared tunnel --config /path/to/reviewed-config.yml ingress rule https://unrelated.example.com/mcp
```

Expected: only the first example URL maps to `127.0.0.1:18790`; every other path/hostname matches the 404 fallback. Do not paste the private tunnel credential JSON or any actual bearer tokens into tickets or ChatGPT. Record only redacted rule matches and exit statuses.

## Authentication and deployment gates

1. **Hosted Grok authentication is an unknown:** documentation confirms a custom HTTPS MCP URL, but does not establish whether this account's Grok.com connector can send the existing gateway's `Authorization: Bearer` token, perform OAuth, or use Cloudflare Access JWT/service tokens. Verify the actual provider and account UI **before** publicly routing traffic. xAI API bearer support must not be generalized to Grok.com.
2. **Cloudflare Access is optional but must be client-compatible:** browser login redirects or service tokens requiring headers Grok cannot send will break MCP. If using Cloudflare Access, implement a *validated* service/OAuth identity model (for example `cloudflared` Protect with Access using a verified audience) and confirm the client handshake. A Cloudflare Tunnel alone is **not client authentication**, and an Access edge rule alone is not sufficient if origin token verification is missing.
3. **Gateway bearer is still mandatory** in the current implementation: issue only separate revocable per-client tokens through the trusted interactive local operator CLI, store no raw token in repository/tunnel config and never forward the user's Authorization header to FastMCP. A public route should not be published until real-provider compatibility, credential lifecycle, edge rate limiting and operator rollback are accepted.
4. **Operator authorization before any live action:** choose the actual hostname/zone, Cloudflare tunnel identity and credential location, authentication and WAF/Access model, startup/restart unit ownership, private grant IDs/TTL and safe rollback plan. The gateway must bind only to `127.0.0.1:18790` with one worker, and original `8790/8791` must remain unpublished. Never merge this document and assume it installs or enables Cloudflare.
5. **Attended negative and positive acceptance:** verify unknown/bad/revoked/expired tokens are rejected before upstream or crawler wake; host/path/Origin spoofing and malformed/encoded routes denied; 12 tools and JSON/SSE bodies function; already-approved public fetch works; private-IP fetch fails; slow or disconnected streams close; root-guard loss fails closed; per-client audit has no token/query leakage; old OpenAI tunnel remains usable. Only after these pass can #230 move toward closure and #231 hosted Grok tests proceed.

**Rollback when eventually deployed:** remove/disable the dedicated Cloudflare published hostname/DNS route and stop the **separate** Cloudflare connector and authenticated gateway as approved, then revoke its independent per-client grants. Do **not** stop, reuse or reconfigure the existing OpenAI `local-research` tunnel, change root-owned nftables, remove backend approval rules, or expose another local port as a workaround.

## Current vendor references (checked October 31, 2026)

- [Cloudflare named-tunnel ingress, regex paths, Host handling, 404 catch-all and validation](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/)
- [Cloudflare `httpHostHeader` origin setting](https://developers.cloudflare.com/tunnel/reference/origin-parameters/)
- [Cloudflare Access and origin JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
- [Grok custom MCP connector](https://docs.x.ai/grok/connectors)
- [Grok MCP tunneling: reachability is not authentication](https://docs.x.ai/grok/connectors/custom-mcp-tunneling)
