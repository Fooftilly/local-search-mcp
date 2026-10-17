# Crawl4AI Docker egress isolation (experimental Linux/nftables profile)

This document describes **an optional operator-installed host firewall layer** for [issue #2](https://github.com/Fooftilly/local-search-mcp/issues/2). It is **not** enabled by `./start.sh`, does **not** manage Crawl4AI or Docker, and is **not a complete sandbox**. It supplements Crawl4AI 0.9.5+'s own URL pinning and SSRF protections. Do not expose the dashboard or the MCP endpoint on the LAN.

## Supported setup / prerequisites

- Crawl4AI **0.9.5+ (stable)** must be installed and verified via `/health` before enabling remote-agent fetches; 0.9.4 retains an SSRF bypass through cache validation.
- Linux host with Docker bridge networking, Docker access for the operator, and `nft` (`sudo pacman -S --needed nftables` on Arch).
- Crawl4AI running **alone on a dedicated user-defined Docker bridge network**. No additional Docker networks, host networking, `--privileged` or `CAP_NET_ADMIN`. IPv6 must be disabled for that bridge.
- Provide at least one **public IPv4 DNS resolver explicitly** when creating Crawl4AI (for example `docker run --dns 1.1.1.1` or Compose `dns: [1.1.1.1]`). Docker's embedded DNS (`127.0.0.11`) forwards to the configured upstream; host-derived router/Pi-hole DNS on private LAN addresses is deliberately blocked by the egress guard. The preflight now rejects missing, private, or IPv6-only Docker DNS entries rather than allowing silently broken crawls. Choose a public resolver you trust; no LAN-DNS firewall exception is installed.
- Publish **only** `127.0.0.1:11235:11235/tcp` from Crawl4AI, with no additional published container ports. The guard refuses public/LAN/wildcard bindings and `PublishAllPorts`. Keep API authentication enabled, and keep the dashboard and MCP server on loopback too.
- The firewall rules live on the **Docker host**, not in the crawler container, and require root to install. Never grant the untrusted browser process firewall control.
- Docker's own iptables/nftables rules remain untouched. This tool creates only the `inet local_search_mcp_crawler` nftables table with `forward` and `input` hooks on the crawler's bridge interface. It is intended for a single local Crawl4AI instance.

For a *new* deployment, create a dedicated network:

```sh
docker network create --driver bridge crawler-egress
```

Then **recreate** your Crawl4AI container with that sole network, preserving the volume mounts, API token, and other safe settings from your existing deployment. Docker Compose users can give the crawler service its own user-defined bridge network and restrict the published port to `127.0.0.1:11235:11235`. **Do not merely `docker network connect` a second network to the existing container**: the audit refuses multihomed containers. Be sure you know how to restore your existing Compose/run configuration before changing it.

## Preview, install and validate

Stop agent-facing MCP/tunnel connections before adjusting a running crawler. The script defaults to container name `crawl4ai`; set `--container <name>` if yours differs.

```sh
python scripts/crawler_egress_guard.py --container crawl4ai --print
sudo python scripts/crawler_egress_guard.py --container crawl4ai --apply
sudo python scripts/crawler_egress_guard.py --container crawl4ai --status
sudo python scripts/crawler_egress_guard.py --container crawl4ai --probe
```

`--print` performs read-only Docker inspection. `--apply` validates the Docker topology, checks the nft syntax, and atomically replaces **only this tool's own table**. The nftables hooks:

- Reject new crawler-to-host connections, preserving established response packets.
- Reject crawler bridge traffic to RFC1918, localhost, link-local, cloud metadata (`169.254/16`), carrier NAT, non-global/reserved IPv4 and multicast ranges.
- Reject **all outbound IPv6** from this bridge (intentional first-version limitation).

The optional `--probe` starts a temporary listener on the bridge gateway, verifies it is reachable from the host, and checks that the **crawler cannot connect**. It then sends TCP probes to six representative non-global IPv4 destinations (10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10, and 203.0.113/24). The probe selects targets outside the active Docker bridge subnet to avoid on-link routing false alarms; if a category cannot be tested off-link, it fails closed. For **each** target it requires an explicit connection refusal **and** an increase in this guard's named kernel nftables IPv4-denial counter. A timeout or unreachable route is *not* counted as a pass. Finally, a hostname-based DNS lookup and TCP connection to `example.com:443` must succeed from inside the crawler. This detects unusable upstream resolvers that an IP-literal probe would miss. The probe fails closed if the kernel counters cannot be read or do not increase. The crawler should be otherwise idle during the probe to reduce counter noise. These tests cover only the specified paths, not browser redirects, DNS rebinding, container-local loopback or every possible interface; also run real approved public-page crawls before rollout.

To remove only this tool's rules:

```sh
sudo python scripts/crawler_egress_guard.py --remove
```

**Reapply after every host reboot or Docker-network recreation, and re-run `--status`/`--probe` before enabling untrusted agent traffic.** The nftables rules are not installed permanently by this utility; automatic reinstallation belongs to the supervised-service work in [issue #3](https://github.com/Fooftilly/local-search-mcp/issues/3). Do not enable agents without the guard in place. If other firewall software modifies the ruleset, re-check it.

## Critical limitations

- Host filtering does **not** block loopback within the same container/network namespace. Crawl4AI's own secure defaults must remain enabled. Its internal proxy/egress controls still handle redirect and DNS-rebinding cases at the URL layer.
- A host firewall cannot prevent an approved public website from acting as an application-level proxy to private services on another network; treat untrusted public proxies as a separate threat.
- Only one container is allowed on the dedicated bridge at install time. Future additional peers or networks may bypass the intended topology; re-audit/reinstall after every configuration change.
- Docker installations using unsupported network drivers, host mode, IPv6-enabled bridges, or atypical firewall/netfilter configuration need a separate security design. No automatic unsafe fallback is provided.
- An exit-zero probe is a **bounded test** of host-gateway denial, representative forwarded IPv4-deny counters and public TCP success, not certification of all container interfaces, routes or browser behavior. Host-level nftables rules cannot block container-local loopback and this script does not test HTTP redirects or JavaScript subresources; issue #2 remains open until those paths are addressed and verified. Docker's default bridge and unmanaged installs are deliberately unsupported.

For Linux Docker firewall behavior, refer to [Docker's nftables guide](https://docs.docker.com/engine/network/firewall-nftables/) and [iptables guide](https://docs.docker.com/engine/network/firewall-iptables/).
