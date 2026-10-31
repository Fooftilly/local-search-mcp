# Documentation index

Stable operator and feature guides live in this folder. Start with the [project README](../README.md) for installation and quick usage.

## Installation and operations

- [Configuration and private settings](CONFIGURATION.md)
- [Systemd supervision](SYSTEMD.md)
- [Attended cold-boot and failure-recovery acceptance](ON-DEMAND-COLD-BOOT-ACCEPTANCE.md)
- [Stopping project-owned components safely](STOP.md)
- [Crawler Compose deployment](CRAWLER-COMPOSE.md)
- [SearXNG Compose overlay](SEARXNG-COMPOSE.md)
- [Crawler versions](CRAWLER-VERSIONS.md)
- [Docker image pinning](COMPOSE-IMAGE-PINS.md)
- [Dependency update checks](DEPENDENCY-UPDATES.md)

## Security and permissions

- [Security model](../SECURITY.md)
- [Crawler egress isolation](EGRESS-ISOLATION.md)
- [Systemd firewall-before-tunnel setup](EGRESS-SYSTEMD.md)
- [Domain permission rules](PERMISSIONS.md)
- [Rule export and import](RULE_TRANSFER.md)

## Research and dashboard features

- [Search](SEARCH.md)
- [Extraction and long documents](EXTRACTION.md)
- [Batch fetching](BATCH.md)
- [Caching](CACHE.md)
- [Cache lifetime settings](CACHE-SETTINGS.md)
- [History index and metrics](HISTORY-INDEX.md)
- [Private provider observations](PROVIDER-OBSERVATIONS.md)
- [History retention settings](HISTORY-SETTINGS.md)
- [Live dashboard and notifications](DASHBOARD-LIVE.md)

## Quality and contributing

- [Testing and deployment checks](TESTING.md)
- [Contributing](../CONTRIBUTING.md)

The root retains files commonly expected by GitHub and contributors:
`README.md`, `AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md`, and `LICENSE`.

**Additional guides retained at the repository root:** [Backup and recovery](../BACKUP.md), [setup and maintenance wizard](../SETUP-WIZARD.md), [on-demand backend preflight](../ON-DEMAND.md), and [dashboard port status](../PORT-STATUS.md). These areas are still evolving; keeping their current paths avoids unnecessary churn while leaving stable feature and operator documentation organized here.

All commands in these guides are intended to run from the **repository root** unless stated otherwise; moving a Markdown file does not change its working directory.
