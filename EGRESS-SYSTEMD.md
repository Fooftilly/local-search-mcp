# Opt-in firewall-before-tunnel boot ordering (Linux host)

**No automatic deployment.** These are optional templates for a Linux host
with Docker and nftables, currently using /usr/bin/python3 and /usr/bin/sudo.
The default user tunnel unit remains unchanged until you install its drop-in.

Security invariant: never run an editable local Git checkout via sudo or root
systemd service. The Python guard is self-contained; explicitly install it
at a root-owned location after reviewing the source:

```sh
sudo install -D -o root -g root -m 0755 scripts/crawler_egress_guard.py /usr/local/libexec/local-search-mcp/crawler_egress_guard.py
sudo install -D -o root -g root -m 0644 config/systemd-system/local-search-mcp-crawler-egress.service /etc/systemd/system/local-search-mcp-crawler-egress.service
sudo systemctl daemon-reload
```

Create a narrowly restricted sudoers rule allowing your normal Linux user
to run only this root-owned read/probe operation, without an interactive
password. Replace **YOUR_USER** with the actual user name. Review the result
with `sudo visudo -cf` before proceeding:

```sudoers
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container crawl4ai --probe
```

Install it manually as /etc/sudoers.d/local-search-mcp-crawler (root-owned,
mode 0440); do not permit arbitrary scripts or arguments. Replacing the
root-owned guard code on Git updates is an *explicit* administrative step.

Stop and disable the user tunnel before applying the new ordering:

```sh
systemctl --user disable --now local-search-mcp-tunnel.service
sudo systemctl enable --now local-search-mcp-crawler-egress.service
sudo systemctl status local-search-mcp-crawler-egress.service
sudo -n /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container crawl4ai --probe
```

Only after BOTH commands pass and the crawler has the required single
dedicated bridge, public IPv4 DNS and loopback binding, install the drop-in:

```sh
install -D -m 0644 config/systemd/guards/10-crawler-egress.conf "$HOME/.config/systemd/user/local-search-mcp-tunnel.service.d/10-crawler-egress.conf"
systemctl --user daemon-reload
systemctl --user enable --now local-search-mcp-tunnel.service
```

The noninteractive `ExecStartPre` runs the **live negative/private and
positive/public probes before each tunnel startup**. Failure or absent
permissions blocks tunnel startup. It cannot create firewall privileges.
The root system unit starts after Docker, applying and probing host nft
rules on boot (retries until the crawler is available). The user tunnel
starts only once the probe has passed. If boot ordering races, systemd may
mark the tunnel failed after limited attempts: check the system firewall
service and then manually restart the tunnel.

IMPORTANT LIMITATION: This protects **startup**. A running tunnel is
NOT automatically stopped if an administrator flushes nftables, Docker
recreates the bridge or the firewall is lost later. Such events require
immediately stopping tunnel access, reapplying/reprobing the guard and
restarting the tunnel. This is not continuous runtime enforcement.
Container restart policy and firewall reinstallation still need monitoring
and operator tests. Do not claim permanent sandboxing from these templates.

Rollback without deleting existing Docker data:

```sh
systemctl --user disable --now local-search-mcp-tunnel.service
rm "$HOME/.config/systemd/user/local-search-mcp-tunnel.service.d/10-crawler-egress.conf"
systemctl --user daemon-reload
sudo systemctl disable --now local-search-mcp-crawler-egress.service
```

With the drop-in removed, leave tunnel auto-start disabled until another
guard is approved. The sudoers permission and root-owned guard installation
are separately removed by the administrator after reviewing dependencies.
