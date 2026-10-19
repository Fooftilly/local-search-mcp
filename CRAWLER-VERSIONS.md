# Manual Crawl4AI version and public release checks

Under [maintenance issue #15](https://github.com/Fooftilly/local-search-mcp/issues/15),
the bridge can inspect the **running** Crawl4AI server's version via the
fixed loopback endpoint `127.0.0.1:11235/health`. It does not inspect
container environments, pull Docker images, use the Docker socket or
restart the crawler.

Offline local health/version report:

```sh
python3 scripts/check_crawler_versions.py
```

An explicitly authorized lookup of the latest public stable GitHub release:

```sh
python3 scripts/check_crawler_versions.py --check-remote
```

Only the pinned `unclecode/crawl4ai` public GitHub latest-release metadata
is requested. The request has a seven-second timeout, a two-MiB response
limit, no redirects or inherited proxies, and no credentials. On failure,
the tool prints a sanitized, nonzero-status report.

The report preserves two distinct facts:

- `local.supported_and_healthy` uses the **existing** bridge version and
  health gate (currently Crawl4AI **0.9.5 or newer, stable**).
- `published_release.meets_bridge_minimum` reports whether the advertised
  public GitHub release satisfies that minimum. An upstream release may
  be *below* the bridge's minimum and must not be treated as an upgrade
  recommendation. Development and custom Docker images may use versions not
  represented by the public release feed.

A version difference is **not evidence** of a newer, safe, compatible or
verified Docker image. No Docker tag/digest, release signatures, runtime
configuration, installed executable, or active container is modified.
Review image provenance, digest, release notes, dependent Hermes Web services,
backups, and the firewall guard before operator-led migration. Use
[CRAWLER-COMPOSE.md](CRAWLER-COMPOSE.md) for the separate opt-in configuration
scaffold. This command is not part of periodic monitoring, `start.sh` setup,
systemd timers or any update installer.
