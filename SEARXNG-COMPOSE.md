# Opt-in SearXNG overlay managed by local-search-mcp

Canonical, nonsecret Compose overlay: config/crawler/searxng.compose.yaml.
Canonical settings template: config/crawler/searxng-settings.yml. The
installer generates a fresh secret key outside Git. The private parent
configuration directory is mode 0700; the Compose overlay is mode 0600.
The bind-mounted settings file is mode 0644 within this inaccessible
parent so the container's unprivileged SearXNG process can read it.

Create the crawler scaffold first if needed with:
`./start.sh --setup-only --generate-crawler-compose`

Preview: `python3 scripts/install_searxng_compose.py`
Install: `python3 scripts/install_searxng_compose.py --install`

**Existing working installs:** do not run the installer to replace local
searxng.compose.yaml or searxng-settings.yml. It intentionally fails on
existing files. Back up and compare your existing private files against
Git, preserve your private server key and settings, then manually adopt
only the changes you approve.

The start command below uses `--pull never` deliberately, so it **will
not download a missing SearXNG image**. Before starting a new installation,
confirm that the reviewed image referenced in your private
`searxng.compose.yaml` is already available locally. The checked-in
`docker.io/searxng/searxng:latest` reference is a **mutable example, not
a production pin**. If needed, explicitly acquire a verified image using
your usual Docker image-pull procedure, inspect its provenance and digest,
and update the **private** Compose image reference to that reviewed,
immutable digest. Recheck the Compose configuration after editing. These
steps are manual; the installer never pulls or upgrades images. If the
image is missing, `up --pull never` should fail rather than silently
fetching an unreviewed release.

Use both files:
```sh
DIR="$HOME/.config/local-search-mcp/crawler-compose"
docker compose -f "$DIR/compose.yaml" -f "$DIR/searxng.compose.yaml" config --quiet
docker compose -f "$DIR/compose.yaml" -f "$DIR/searxng.compose.yaml" up -d --no-deps --pull never searxng
```

The SearXNG bridge is separate from crawler-egress. Neither the installer
nor this overlay alters firewall rules, services or already-running Docker
containers. The public latest image tag is mutable; pin a reviewed image
digest before production use. Do not use --remove-orphans with only one
Compose file.

Start only the SearXNG service during migration: starting the complete
Compose project could conflict with an existing Crawl4AI container.
Start the entire project only after the old crawler has been decommissioned.
