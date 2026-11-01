# Persistent private endpoint for Cloudflare MCP Portal (manual, opt-in)

This is a deployment recipe, NOT a live installation. Tested manually with
`cloudflare-tunnel-2` in host network mode, Tailscale DNS and an existing
loopback MCP adapter. The first Cloudflare tunnel, Tailscale's global DNS,
the root-owned crawler guard and the existing MCP service units are unchanged.

## Boundary and prerequisites

- The dedicated second tunnel must have `network_mode: host` and
  `dns: [127.0.0.1]`, **without** `extra_hosts` for `mcp.home.arpa`.
- Its configured private-hostname route `mcp.home.arpa` must point to that
  second tunnel, and the Portal URL remains `http://mcp.home.arpa:18791/mcp`.
- The tested destination `10.253.91.1/32` must remain unused by all
  machine, Docker and Tailscale networks. Check policy-route tables as well.
- User services `local-search-mcp-remote-gateway` and
  `local-search-mcp-cloudflare-origin` must already be working. Their
  original bearer checks, per-request root guard and Host checks remain.
- The new endpoint is non-public: an nftables input-chain rule drops every
  packet entering the dedicated private address from a non-loopback interface.
  A root-owned helper installs that guard **before** assigning the address.
  The socat process then listens on this address and forwards to the existing
  loopback adapter, running as the intended unprivileged Linux account.

## Attended installation (do not run blindly)

Run from a reviewed checkout on Arch. These are privileged mutations and
require the operator's explicit execution. Do not run the helper directly
from an owner-writable checkout as root.

1. Compare the tested config with `config/dns/cloudflare-portal.conf`, check
   `ip -4 rule show`, `ip -4 route show table all`, and verify that no
   `10.253.91.1/32` alias, listener or conflicting nftables table exists.
   Check that no other local DNS service occupies `127.0.0.1:53`.
2. Stop the temporary DNS service if running:

   ```sh
   sudo systemctl stop local-search-mcp-cf-dns-test.service
   ```

3. Install only the reviewed, dedicated root-owned files:

   ```sh
   sudo install -D -o root -g root -m 0755 scripts/cloudflare_private_endpoint.py \
     /usr/local/libexec/local-search-mcp/cloudflare_private_endpoint.py
   sudo install -D -o root -g root -m 0644 \
     config/systemd-system/local-search-mcp-cf-dns.service \
     /etc/systemd/system/local-search-mcp-cf-dns.service
   sudo install -D -o root -g root -m 0644 \
     config/systemd-system/local-search-mcp-cf-private@.service \
     /etc/systemd/system/local-search-mcp-cf-private@.service
   # Existing /etc/local-search-mcp/cloudflare-dns.conf should match the
   # reviewed template; do not overwrite custom settings without comparing.
   sudo diff -u config/dns/cloudflare-portal.conf \
     /etc/local-search-mcp/cloudflare-dns.conf
   sudo dnsmasq --test --conf-file=/etc/local-search-mcp/cloudflare-dns.conf
   sudo systemctl daemon-reload
   ```

4. Enable the dedicated DNS service first. Replace `$(id -un)` with the
   intended local account if using a different operator:

   ```sh
   sudo systemctl enable --now local-search-mcp-cf-dns.service
   dig @127.0.0.1 mcp.home.arpa A +short
   sudo systemctl enable --now "local-search-mcp-cf-private@$(id -un).service"
   ip -4 route get 10.253.91.1
   sudo nft list table inet lsm_cf_private_endpoint
   sudo ss -lntup '( sport = :18791 or sport = :53 )'
   curl --noproxy '*' -i --max-time 5 \
     -H 'Host: mcp.home.arpa:18791' http://10.253.91.1:18791/mcp
   ```

   DNS must return `10.253.91.1`, the routing lookup must be local on `lo`,
   and the last curl should return HTTP 401 for the missing bearer. Verify the
   nftables DROP before attempting any Gateway synchronization. A 401 does
   not prove the bearer or the Cloudflare Portal is functional.

5. Retry Cloudflare **Sync capabilities** and verify each of the twelve tools
   is discovered; the Portal must also enforce `Personal MCP Only`. Negative
   tests must reject unauthorized identities, expired/incorrect bearer, wrong
   Host and unreachable guard. Tool listing/real calls are separate checks.

## Startup, failure and rollback

System services are separate from the user-managed MCP units. The private
unit uses privileged `ExecStartPre` to install the nftables guard **before**
assigning the private IP; the long-running `socat` process runs as `%i`,
not as root. `ExecStopPost` removes the IP first and then the dedicated
guard, even when the forwarder fails. A root-owned systemd-invocation marker prevents a failed second activation
from deleting the first activation\u0027s resources; an unexpected pre-existing alias/table makes
startup fail rather than adopting foreign state.

For rollback, disable the Portal server route first, then stop the transport
before the DNS service:

```sh
sudo systemctl disable --now "local-search-mcp-cf-private@$(id -un).service"
sudo systemctl disable --now local-search-mcp-cf-dns.service
ip -4 addr show dev lo
sudo nft list table inet lsm_cf_private_endpoint
```

The last nft command should report that the table does not exist after a
successful cleanup. If cleanup fails, keep the firewall rule in place and
inspect the journal; never remove it while the private IP/listener survives.
The existing container tunnel and MCP user services are **not** removed.

These commands have not been executed on the operator's PC by this PR.
Cloudflare acceptance remains contingent on live sync and negative tests.
