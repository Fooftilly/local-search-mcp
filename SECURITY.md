# Security model

**This bridge should only be used with a trusted local operator.** The MCP endpoint has no built-in authentication and binds to `127.0.0.1`; the separate OpenAI tunnel handles remote connectivity. Never forward ports 8790/8791/8888/11235 from an Internet-facing interface.

The FastMCP Streamable HTTP endpoint explicitly **enforces Host/Origin validation** for DNS-rebinding defense. Non-loopback `Host` headers are rejected (HTTP 421); non-loopback or `null` browser `Origin` headers are rejected (HTTP 403). In FastMCP 4.1, the built-in loopback allowlist also accepts `127.0.0.1`, `localhost`, and `::1` regardless of port, including browser origins on other loopback ports; the additional configured lists **do not pin Host/Origin to port 8790**. Missing `Origin` is accepted for non-browser local clients (including the loopback tunnel). This is a request guard, not authentication or a substitute for browser CORS protections. A loopback bind alone would not prevent DNS-rebinding attacks. Do not disable the guard, configure wildcard hosts/origins, or proxy arbitrary external Host headers to the MCP endpoint. If changing tunnel architecture, verify that its local HTTP hop uses a loopback Host.

## Crawl4AI and SSRF

Domain approvals must match the host the browser actually navigates to. For
Unicode hostnames the bridge compares Python's legacy IDNA2003 output with
browser-compatible UTS #46 non-transitional output. Any divergence is rejected
before DNS, one-use grants and approval queue creation, including less familiar
Unicode 3.2 normalization differences (not only `faß.de` vs `fass.de`).
ASCII hostnames retain the original validation. The `idna` dependency is
required to authorize Unicode names; if unavailable, Unicode input fails
closed, rather than silently relying on IDNA2003. During first-run setup
Unicode domain settings are validated by the prepared project virtualenv,
not the system Python interpreter.

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
