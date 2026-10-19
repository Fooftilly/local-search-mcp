# Opt-in SearXNG overlay managed by local-search-mcp

Canonical, nonsecret Compose overlay: config/crawler/searxng.compose.yaml.
Canonical settings template: config/crawler/searxng-settings.yml. The
installer generates a fresh secret key outside Git (mode 0600).

Create the crawler scaffold first if needed with:
`./start.sh --setup-only --generate-crawler-compose`

Preview: `python3 scripts/install_searxng_compose.py`
Install: `python3 scripts/install_searxng_compose.py --install`

**Existing working installs:** do not run the installer to replace local
searxng.compose.yaml or searxng-settings.yml. It intentionally fails on
existing files. Back up and compare your existing private files against
Git, preserve your private server key and settings, then manually adopt
only the changes you approve.

Use both files:
```sh
DIR="$HOME/.config/local-search-mcp/crawler-compose"
docker compose -f "$DIR/compose.yaml" -f "$DIR/searxng.compose.yaml" config --quiet
docker compose -f "$DIR/compose.yaml" -f "$DIR/searxng.compose.yaml" up -d --pull never
```

The SearXNG bridge is separate from crawler-egress. Neither the installer
nor this overlay alters firewall rules, services or already-running Docker
containers. The public latest image tag is mutable; pin a reviewed image
digest before production use. Do not use --remove-orphans with only one
Compose file.
