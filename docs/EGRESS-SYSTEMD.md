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
with `sudo visudo -cf /etc/sudoers.d/local-search-mcp-crawler` **after**
installing that root-owned file (or `sudo visudo -cf /path/to/staged-file`
before installing), since `-f` requires an explicit pathname:

```sudoers
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container crawl4ai --probe
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container crawl4ai --preflight
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container crawl4ai --tunnel-gate
```

Install it manually as /etc/sudoers.d/local-search-mcp-crawler (root-owned,
mode 0440); do not permit arbitrary scripts or arguments. Replacing the
root-owned guard code on Git updates is an *explicit* administrative step.

Stop and disable the user tunnel before applying the new ordering:

```sh
systemctl --user disable --now local-search-mcp-tunnel.service
sudo systemctl enable --now local-search-mcp-crawler-egress.service
sudo systemctl status local-search-mcp-crawler-egress.service
sudo -n /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container crawl4ai --tunnel-gate
```

Only after BOTH commands pass and the crawler has the required single
dedicated bridge, public IPv4 DNS and loopback binding, install the drop-in:

```sh
install -D -m 0644 config/systemd/guards/10-crawler-egress.conf "$HOME/.config/systemd/user/local-search-mcp-tunnel.service.d/10-crawler-egress.conf"
systemctl --user daemon-reload
systemctl --user enable --now local-search-mcp-tunnel.service
```

The drop-in uses `ExecStartPre=+` so that the **pre-start probe only**
bypasses `NoNewPrivileges=true` and can use the narrowly authorized
`sudo -n` elevation. The long-lived tunnel's `ExecStart` keeps
`NoNewPrivileges=true`. This distinction is essential: removing the plus
prefix leaves `sudo` unable to elevate, blocking every tunnel start.
After installation, inspect the combined user unit with
`systemctl --user cat local-search-mcp-tunnel.service` and verify the
live start and journal; offline tests and systemd-analyze verify alone cannot
prove the host-specific sudo policy and privileges.

For optional #75 on-demand cold wake, also install the fixed
`local-search-mcp-guard-preflight.service` alongside the fixed
`local-search-mcp-guard-probe.service` via
`python3 scripts/install_user_services.py --install --guard-probe`,
followed by `systemctl --user daemon-reload`. The preflight unit calls the
**root-owned** helper with exactly `--container crawl4ai --preflight` while
the crawler is stopped. It verifies installed nftables bridge coverage
without claiming that live negative/public probes succeeded. After Docker
wake, the separate `--probe` oneshot performs the mandatory live tests.
Both commands must be authorized narrowly; a service unit does not grant
privilege on its own. Refresh the operator-installed root-owned guard when
the reviewed helper source changes; never execute the editable checkout
as root.

The noninteractive `ExecStartPre` runs a fixed **root-owned tunnel gate**
before each startup. If Crawl4AI is running, the gate requires the full
negative/private and positive/public live probes. If it is intentionally
stopped, the gate requires the previously installed nftables host/forward
rules on the same verified private Docker bridge; it **does not claim a live
probe passed**. The existing on-demand crawler wake path must separately
run a stopped-container firewall preflight **before** starting Crawl4AI and
a fresh live `--probe` **after** starting it, before any fetching. A
missing/mismatched rule, Docker uncertainty or absent sudo permission blocks
tunnel startup. The gate cannot create firewall privileges.

The root system unit now calls `--startup` after Docker is available. It
atomically installs the scoped firewall rule table for the verified bridge,
then runs a live probe if the crawler is running or a stopped-container
preflight if it is sleeping. It does **not** automatically wake a sleeping
backend, modify Docker restart policies, or expose the crawler API.
With backend restart policy `no`, this permits secure boot without running
the heavyweight containers until their first actual MCP requests.

**Boot-race limitation:** systemd user services do not have reliable direct
ordering against this root system service. If the tunnel starts before the
root guard is installed, its gate fails closed and systemd retries subject to
the existing restart limits; the operator must diagnose any exhausted limit.
The root service itself retries failures. Complete unattended reboot recovery
still needs a live reboot test, including checks that no other startup path
launches Crawl4AI before nftables coverage is installed.

IMPORTANT LIMITATION: This protects **startup**. A running tunnel is
NOT automatically stopped if an administrator flushes nftables, Docker
recreates the bridge or the firewall is lost later. Such events require
immediately stopping tunnel access, reapplying/reprobing the guard and
restarting the tunnel. This is not continuous runtime enforcement.
Container restart policy and firewall reinstallation still need monitoring
and operator tests. Do not claim permanent sandboxing from these templates.

## Updating an existing protected installation

This is a security-sensitive, explicit migration, **not** an automatic
upgrade. Existing root-owned helper/service files, the two-command sudoers
policy and the user tunnel drop-in will deliberately fail the wizard's
same-content checks until the operator reconciles them. Do not delete or
blindly overwrite a modified sudoers file.

Before copying the new templates, inspect the existing installed files and
ownership; back up/compare exact differences to the known project versions.
Stop the tunnel first with
`systemctl --user stop local-search-mcp-tunnel.service`; leave the nftables
table installed during the entire migration. Only then, after review, install
the root helper at the fixed root-owned path with mode 0755 and install the
root systemd service with mode 0644, as illustrated above. Run
`sudo systemctl daemon-reload` and restart the root service. Its
`--startup` command must pass with the verified crawler either running or
sleeping. Do not start the user tunnel until it passes.

Stage the exact three-line sudoers policy for the current operator and check
it with `sudo visudo -cf /path/to/staged-file`. Install it root-owned mode
0440 **only after explicitly confirming any existing file has no unrelated
rules**; then verify the installed file with `sudo visudo -cf
/etc/sudoers.d/local-search-mcp-crawler`. Inspect the effective systemd
tunnel unit, then replace **only the previously reviewed, matching managed
drop-in** with the new `--tunnel-gate` template and run
`systemctl --user daemon-reload`. Start the tunnel only after
`sudo -n /usr/bin/python3
/usr/local/libexec/local-search-mcp/crawler_egress_guard.py --container
crawl4ai --tunnel-gate` succeeds and the effective unit has exactly one
non-ignored `ExecStartPre=+` running the same command.

If any file differs unexpectedly, ownership cannot be proven, or the firewall
gate fails, **leave the tunnel stopped** and inspect before retrying. Do not
install user-writable Python code as root, disable the startup guard or weaken
nftables to recover availability. Re-enable the previous protected design
only with the matching prior root guard, sudoers policy, unit and drop-in.

Rollback without deleting existing Docker data:

```sh
systemctl --user disable --now local-search-mcp-tunnel.service
rm "$HOME/.config/systemd/user/local-search-mcp-tunnel.service.d/10-crawler-egress.conf"
systemctl --user daemon-reload
sudo systemctl disable --now local-search-mcp-crawler-egress.service
```

Stopping the oneshot **does not remove** its nftables rules. If you intend a
full firewall rollback, and only after the tunnel is stopped and disabled
(and no other untrusted crawler access is enabled), explicitly remove only
this guard's table using the reviewed, root-owned copy:

```sh
sudo /usr/bin/python3 /usr/local/libexec/local-search-mcp/crawler_egress_guard.py --remove
```

If you still need the network restrictions, leave the table installed.
With the drop-in removed, leave tunnel auto-start disabled until another
guard is approved. The sudoers permission and root-owned guard installation
are separately removed by the administrator after reviewing dependencies.
