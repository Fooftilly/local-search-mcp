# Optional Cloudflare MCP Server Portal private origin (#230, #246)

**Status:** Locally implementable and independently testable; **not deployed or
end-to-end accepted**. This implementation deliberately does not create DNS,
a public hostname, a Cloudflare Access application, a Gateway route, or an
Internet-facing listener. It neither changes the existing OpenAI secure tunnel
nor authorizes another client to reuse the OpenAI control-plane credentials.

## Security boundaries

```text
Authorized client -> Cloudflare MCP Portal (OAuth/Access allow policy)
  -> Cloudflare MCP server (separate Access allow policy)
  -> Cloudflare Gateway private-hostname route
  -> existing owned cloudflared connector on the SAME network namespace/host
  -> http://<PRIVATE_HOSTNAME>:18791/mcp (route not yet proven)
  -> optional 127.0.0.1:18791 private Host adapter
  -> 127.0.0.1:18790/mcp authenticated gateway (cloudflare-portal bearer)
  -> 127.0.0.1:8790/mcp existing FastMCP bridge
```

The adapter binds **only** to `127.0.0.1:18791`, denies requests from
non-loopback peers and rejects unknown Hosts, Origins, forwarded identity
headers, credentials in cookies, query strings, wrong paths and methods.
It accepts an exact configured Host (with optional fixed `:18791` suffix),
then forwards only to the fixed authenticated gateway. The existing gateway
independently validates its dedicated bearer, expiry, root-owned egress guard
on each admitted request, size/rate limits and revocation. The adapter does not
trust `CF-Access-*` headers or impersonate Cloudflare identity. **The gateway
knows only the `cloudflare-portal` principal; Cloudflare must enforce the
actual end-user identity and keep suitable Access logs.** No request is
forwarded to the operator dashboard, crawler, SearXNG, Docker or LAN.

The standalone adapter config file contains *only* the private hostname (no
token). It is stored under the existing account-owned
`~/.config/local-search-mcp/private/` directory with mode 0600.
It is revalidated for every incoming request; removing it makes an already
running adapter return 503. No bearer is stored in the adapter config, systemd
unit, CLI argument, GitHub, or logs. The independent bearer is issued using
the existing credential manager, which stores only its SHA-256 verifier.

## Local operator workflow (no Cloudflare account changes)

All steps are run on your own Linux machine as its **unprivileged** owner.

1. Inspect the local prerequisites without changes:

   ```sh
   ./setup.sh --cloudflare-portal-preview
   ./.venv/bin/python -m remote_agent_server --preflight
   ./setup.sh --dry-run
   ```

   The preview does **not** assert that the Cloudflare Portal, private
   hostname, tunnel route or gateway listener is reachable.

2. Issue the portal-specific grant through the trusted controlling terminal.
   The one-time token is printed **only to `/dev/tty`**, not stdout, logs or
   process arguments. Store it directly in your password manager; enter
   `COMMIT` on that terminal. Never paste it into a shell command.

   ```sh
   ./.venv/bin/python -m remote_agent_credentials create cloudflare-portal --apply
   ```

   The grant expires (default 24 h; maximum 720 h). Rotation is
   **break-before-make**: the adapter accepts only the active
   `cloudflare-portal` client grant; another client ID cannot serve as an
   overlapping temporary grant. At an attended maintenance window, run
   `./.venv/bin/python -m remote_agent_credentials rotate cloudflare-portal --apply`,
   transfer the new credential through Cloudflare's protected upstream
   credential form, then verify MCP tool calls. Requests with the old token
   fail between rotation and Cloudflare activation of the new token.
   Zero-downtime rotation is **not supported** by this adapter. Revoking
   `cloudflare-portal` denies new requests immediately, and the existing
   gateway interrupts active streams using revocation polling. Never put
   the token in arguments, shell history, URLs, logs or GitHub.

3. Configure a **private** hostname and two **disabled** user-unit files:

   ```sh
   ./setup.sh --cloudflare-portal-prepare
   ```

   Type `PREPARE`, enter your private hostname at the local terminal (no
   URL, scheme or port), then type `INSTALL`. The setup refuses to overwrite
   an existing private configuration, does not enable/start units, and does
   not modify Cloudflare. A partial unit-install failure may leave the
   owner-only config but no active service; inspect before retrying.

4. Verify the new unit templates and existing root guard:

   ```sh
   ./setup.sh --cloudflare-portal-preview
   ./setup.sh --cloudflare-portal-status
   python3 scripts/install_user_services.py --cloudflare-portal
   ./.venv/bin/python -m remote_agent_cloudflare_origin
   ./.venv/bin/python -m remote_agent_server --preflight
   ```

   Start only after reviewing the private routing step below:

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now local-search-mcp-remote-gateway.service
   systemctl --user enable --now local-search-mcp-cloudflare-origin.service
   systemctl --user status local-search-mcp-remote-gateway.service
   systemctl --user status local-search-mcp-cloudflare-origin.service
   ```

   After starting, re-run `./setup.sh --cloudflare-portal-status`.
   This checks the optional systemd unit states and whether fixed loopback
   TCP ports accept connections. It does not prove peer identity, MCP
   authentication, Cloudflare private routing or Grok acceptance.

   Both services use the project `.venv` Python and fail closed if grants,
   root guard or mandatory MCP tools are not ready. They do not configure the
   tunnel and do not wake sleeping backends merely by starting.

   **User-service namespace compatibility:** The optional units intentionally
   run as the existing unprivileged owner without filesystem namespace
   directives (`PrivateTmp=false`, `ProtectSystem=false`,
   `ProtectHome=false`). On Arch Linux, enabling these mount restrictions
   in a systemd *user* unit remaps root-owned ancestors such as `/` and
   `/home` from UID 0 to UID 65534. Our strict private-file validator must
   reject those owners. Moreover, the authenticated gateway's dedicated
   host-root egress guard runs through `sudo -n`, which cannot elevate
   to host root within that unprivileged user namespace. The adapter must
   **not** invoke sudo: it relies on the gateway's startup and per-request
   root-guard enforcement and retains `NoNewPrivileges=true`. Do **not** widen
   the private-file ownership allowlist, loosen 0600/0700 requirements or
   change the sudoers/egress guard to work around the failure.

   This is a conscious trade-off: those two optional services do **not** get
   read-only filesystem mount isolation or a separate temporary directory.
   They retain fixed loopback listening, strict credential/Host checks,
   `UMask=0077`, address-family restrictions and root-guard checks; the
   adapter additionally retains `NoNewPrivileges=true`. The gateway
   must retain `NoNewPrivileges=false` for the guard. Future isolation
   upgrades require a separately verified host-guard delegation design.

   After pulling an update to these templates, regenerate only the two
   optional user units (do not rerun attended hostname preparation):
   
   ```sh
   python3 scripts/install_user_services.py --install --cloudflare-portal
   systemctl --user daemon-reload
   systemctl --user restart local-search-mcp-remote-gateway.service
   systemctl --user restart local-search-mcp-cloudflare-origin.service
   ./setup.sh --cloudflare-portal-status
   ```


## Cloudflare dashboard: separate attended deployment gate

Official references:

- [MCP Server Portals](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/)
- [Private MCP origins](https://developers.cloudflare.com/changelog/post/2026-09-22-private-mcp-servers/)
- [Private hostname routes](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/private-net/cloudflared/connect-private-hostname/)

1. Confirm a Cloudflare-managed domain and Zero Trust identity provider.
   Restrict the Portal Access application **and** linked MCP server Access
   application to your specific identity. Confirm there is no default
   permissive policy. Do not rely on the server visibility policy alone for
   origin protection. Disable Code Mode for initial interoperability tests.
2. In your **existing** tunnel, configure a **private hostname route**, not
   a published public application route. In Gateway, select that route for
   the upstream private origin and restrict network access to the intended
   connector and destination. **Do not overwrite the tunnel's other routes**.
   The private hostname must resolve on the connector host/network namespace
   to a strictly local address that reaches the adapter on port 18791.
   Docker-containerized cloudflared has its *own* loopback; the host's
   `127.0.0.1` is not automatically reachable from that container.
   Cloudflare documentation demonstrates RFC1918 private addresses, not
   loopback private routes. **Do not assume a private hostname that resolves
   to 127.0.0.1 will work**; private-route and loopback delivery must be
   established with a real connection and negative tests. Do not bind the
   adapter on a LAN address or 0.0.0.0 to work around route failure.
3. Create the MCP server with the private URL
   `http://<PRIVATE_HOSTNAME>:18791/mcp`, enable **Route traffic through
   Cloudflare Gateway**, choose upstream authentication **Bearer**, and
   enter the one-time dedicated token *only* into the Cloudflare protected
   credentials UI. Never store it in a GitHub issue, YAML, shell argument,
   URL, or curl history. The API also supports `auth_type: bearer` and
   `auth_credentials`, but API automation needs separately authorized,
   scoped credentials and robust secret input. This slice makes no API writes.
4. Create or edit your Portal, add the server and restrict the portal to your
   identity through Cloudflare Access. Verify the Portal reports server
   `Ready`, then verify that authenticated and unauthenticated clients are
   correctly separated. **Ready only confirms registration/synchronization,
   not successful actual tool calls.**
5. Verify upstream tool names appear as
   `<server_id>_web_search`, etc., for all twelve gateway tools, plus
   Cloudflare `portal_*` tools. Do not demand exactly 12 overall Portal
   tools. Compare each upstream `inputSchema` and `outputSchema`,
   `structuredContent`, result errors and streaming behavior with #215
   and #231; do not assume Grok website OAuth compatibility without testing.
   The Access policy `Personal MCP Only` must be explicitly selected for
   the applicable Portal/server Access applications, not merely created.

### Blockers before real authorization

- Prove the private hostname connector route can reach loopback **without**
  exposing the adapter outside the host. This repository does not claim it.
  If Cloudflare cannot route to loopback, a separately reviewed, isolated
  private network interface plus firewall design is required. Never fall
  back to a public published route automatically.
- Verify negative unauthorized/incorrect-identity, wrong bearer, expired/
  revoked credential, invalid Host/path, root guard loss and sleeping
  backend behavior against the deployed portal.
- Verify HTTPS Portal OAuth2 in the actual Grok client and tool discovery,
  including extra `portal_*` tools and namespaced schemas.

## Local rollback and Cloudflare removal

First disable/delete the Portal's link to the MCP server and the **specific**
private route in Cloudflare Dashboard, leaving unrelated tunnel routes alone.
Then run `./setup.sh --cloudflare-portal-rollback` and type
`REMOVE` to approve stopping the optional services. A second trusted
`/dev/tty` `REMOVE` confirmation authorizes deletion of the private
hostname configuration using the project virtualenv. Both confirmations are
needed. The rollback verifies that each unit is project-owned and really
disabled/stopped **before** removing any local unit files or private config,
then reloads the user systemd manager. It intentionally leaves the existing ChatGPT secure tunnel,
root-owned guard, baseline bridge and Docker backends alone.

The low-level installer refuses `python3 scripts/install_user_services.py
--remove --cloudflare-portal` while either optional unit is active,
enabled, foreign or unverified; it does not stop services itself. Use the
attended rollback workflow instead of deleting a running unit file.

Finally, revoke the dedicated verifier:

```sh
./.venv/bin/python -m remote_agent_credentials revoke cloudflare-portal --apply
./setup.sh --cloudflare-portal-preview
```

If rollback reports an error, inspect the optional unit state and private
config before retrying; do not claim an incomplete rollback succeeded. If
you used the same remote gateway for other authenticated clients, stopping
its optional systemd unit may briefly disconnect them; make a deliberate
decision before rollback. No remote Cloudflare settings or user credentials
are ever removed by this script.
