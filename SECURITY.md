# Security model

**This bridge should only be used with a trusted local operator.** The MCP endpoint has no built-in authentication and binds to `127.0.0.1`; the separate OpenAI tunnel handles remote connectivity. Never forward ports 8790/8791/8888/11235 from an Internet-facing interface.

## Crawl4AI and SSRF

An approved domain is not proof that the crawler's subsequent browser navigations will stay on that domain. Initial DNS resolution in the bridge is only defense in depth, not enforcement of final browser egress, redirects, images, scripts or DNS changes. **The network isolation/egress policy belongs at the crawler process/container boundary.**

The bridge requires a healthy **Crawl4AI 0.9.4+** HTTP server. Version 0.9.4 is a security release that fixes multiple SSRF paths. Leave its hardened Docker egress controls enabled, and do not enable `CRAWL4AI_ALLOW_INTERNAL_URLS`, unsafe proxy rules, or `CRAWL4AI_ALLOW_INSECURE_TLS` for this integration. Use OS/container firewalling to block private, loopback, link-local, metadata, and LAN egress from the browser container as an additional control; this repository does **not** install such a firewall. Version checks alone cannot verify the container's runtime network policy. If you cannot verify isolation, do not grant access to untrusted agents or crawl hostile websites.

Operator validation should include inspecting the running Crawl4AI Docker configuration, reviewing current upstream security advisories, and verifying that redirects to private/network-local targets are refused **from inside the isolated crawler environment**. Never turn on unsafe flags to pass a test.

### Optional operator-managed host firewall\n\n[EGRESS-ISOLATION.md](EGRESS-ISOLATION.md) documents a Linux/nftables profile that restricts new crawler-to-host connections and non-global IPv4 destinations for a single dedicated Docker bridge, and blocks IPv6 forwarding. It is opt-in, not a complete browser sandbox: it does not block the crawler's own container loopback, is not persistent across reboot, and requires a live verification on the operator's host. Do **not** treat the script's presence in the repository or a passing unit test as proof of runtime containment.\n\n## Domain approval

The dashboard uses a Public Suffix List through `libpsl`, including privately operated suffixes (e.g., `github.io`). Rules applying to entire public suffixes are not permitted; prior broad rules are removed during migration. Parent-domain rules apply to all subdomains beneath that registrable parent. Approve only necessary hosts; use explicit denials for exceptions. Agents cannot approve their own requests. CSRF, Origin/Referer/Fetch Metadata and Host checks mitigate browser-based attacks on the local dashboard.

## Secrets and history

Keep keys in the private launcher configuration, not repository files. Treat shell history, Docker `inspect`, debug logs and backups as possible sensitive surfaces. Search queries and fetched URLs can be sensitive even without credentials. Private state lives under the user's XDG state directory; history rotates but is not automatically deleted after a fixed date.

## Reporting a vulnerability

Open a private GitHub security advisory to notify the repository maintainer. Do not publish active exploit details in a public issue.
