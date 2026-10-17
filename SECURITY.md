# Security model

**This bridge should only be used with a trusted local operator.** The MCP endpoint has no built-in authentication and binds to `127.0.0.1`; the separate OpenAI tunnel handles remote connectivity. Never forward ports 8790/8791/8888/11235 from an Internet-facing interface.

The FastMCP Streamable HTTP endpoint now explicitly **enforces Host/Origin validation**. Only `127.0.0.1:8790` and `localhost:8790` are accepted as HTTP authorities; browser `Origin` values must likewise use `http://127.0.0.1:8790` or `http://localhost:8790`. Missing `Origin` is allowed for non-browser local clients (including the loopback tunnel). Foreign Host returns HTTP 421; foreign Origin returns HTTP 403. A loopback bind alone would not prevent DNS-rebinding attacks. Do not disable the guard, configure wildcard hosts/origins, or proxy arbitrary external Host headers to the local MCP endpoint. If changing the tunnel architecture, verify that its local HTTP hop preserves one of the allowed Host headers. This request guard is not authentication.

## Crawl4AI and SSRF

An approved domain is not proof that the crawler's subsequent browser navigations will stay on that domain. The bridge now inspects Crawl4AI's `redirected_url` (not only `url`, which represents the requested URL) and refuses to return content from unapproved final destinations. Initial DNS resolution and the final-URL check in the bridge are only defense in depth, not enforcement of browser egress, nested redirects, images, scripts or DNS changes. **The network isolation/egress policy belongs at the crawler process/container boundary.**

The bridge requires a healthy **Crawl4AI 0.9.5+ (stable)** HTTP server. Version 0.9.4 fixes some SSRF paths but retains the `CacheValidator.validate` freshness-check SSRF bypass (patched in 0.9.5; upstream GHSA-w4xc-px5p-c32w). Leave its hardened Docker egress controls enabled, and do not enable `CRAWL4AI_ALLOW_INTERNAL_URLS`, unsafe proxy rules, or `CRAWL4AI_ALLOW_INSECURE_TLS` for this integration. Use OS/container firewalling to block private, loopback, link-local, metadata, and LAN egress from the browser container as an additional control; this repository does **not** install such a firewall. Version checks alone cannot verify the container's runtime network policy. If you cannot verify isolation, do not grant access to untrusted agents or crawl hostile websites.

Operator validation should include inspecting the running Crawl4AI Docker configuration, reviewing current upstream security advisories, and verifying that redirects to private/network-local targets are refused **from inside the isolated crawler environment**. Never turn on unsafe flags to pass a test.

### Optional operator-managed host firewall

[EGRESS-ISOLATION.md](EGRESS-ISOLATION.md) documents a Linux/nftables profile that restricts new crawler-to-host connections and non-global IPv4 destinations for a single dedicated Docker bridge, and blocks IPv6 forwarding. It is opt-in, not a complete browser sandbox: it does not block the crawler's own container loopback, is not persistent across reboot, and requires a live verification on the operator's host. Do **not** treat the script's presence in the repository or a passing unit test as proof of runtime containment.

## Domain approval

The dashboard uses a Public Suffix List through `libpsl`, including privately operated suffixes (e.g., `github.io`). Rules applying to entire public suffixes are not permitted; prior broad rules are removed during migration. Parent-domain rules apply to all subdomains beneath that registrable parent. Approve only necessary hosts; use explicit denials for exceptions. Agents cannot approve their own requests. CSRF, Origin/Referer/Fetch Metadata and Host checks mitigate browser-based attacks on the local dashboard.

## Secrets and history

Keep keys in the private launcher configuration, not repository files. Treat shell history, Docker `inspect`, debug logs and backups as possible sensitive surfaces. Search queries and fetched URLs can be sensitive even without credentials. Private state lives under the user's XDG state directory; history rotates but is not automatically deleted after a fixed date.

## Reporting a vulnerability

Open a private GitHub security advisory to notify the repository maintainer. Do not publish active exploit details in a public issue.
