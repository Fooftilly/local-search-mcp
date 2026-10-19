# Opt-in private Crawl4AI Docker Compose scaffold

The bridge normally consumes an **existing** SearXNG service on 127.0.0.1:8888
and an existing Crawl4AI service on 127.0.0.1:11235. You do not need Docker
Compose to use the bridge. This optional setup action generates a **separate,
new, not-running** crawler project for operators who want to migrate to a
network layout compatible with [EGRESS-ISOLATION.md](EGRESS-ISOLATION.md).

## Generate without changing running Docker containers

With your existing local-search-mcp config and Crawl4AI container:

```sh
./start.sh --setup-only --generate-crawler-compose
```

This reads the saved Crawl4AI API token (or asks for one) and **only the image
reference** from the container named `crawl4ai`. It does not inspect Docker
container environment variables, clone the existing Compose project, modify
the Hermes Web project, stop containers, pull images, install firewall rules or
start services. The usual `./start.sh --setup-only` remains unchanged.

For a new installation without a running Crawl4AI container, explicitly supply
a verified image name:

```sh
./start.sh --setup-only --generate-crawler-compose --crawler-image YOUR_VERIFIED_IMAGE
```

Choose an actual published image verified to run Crawl4AI **0.9.5 or newer,
stable**. The generator does **not** resolve or verify the tag, digest, image
contents or upstream security configuration. Do not assume `latest` is safe.
Checking an existing container's image reference does not preserve its
volumes, runtime flags, other environment variables or custom configuration.

Generation creates the new directory:

```text
${XDG_CONFIG_HOME:-~/.config}/local-search-mcp/crawler-compose/
    compose.yaml
    .env
```

The directory is owner-only (0700); both files are owner-only (0600).
The canonical, secret-free `config/crawler/compose.yaml` lives in Git and is
copied unchanged into the private directory. Only the adjacent private
`.env` is generated with installation-specific values. Edit the reviewed
repository file to change networking, rather than a Python string.
The YAML contains **no secrets**, while the adjacent private `.env` contains
the selected image name and Crawl4AI token, in Compose's literal quoted format.
The OpenAI tunnel runtime key is never copied. Keep the .env file out of
repositories, backups for sharing, screenshots and issue comments. Avoid
`docker compose config` without `--quiet` because it can print resolved
environment variables.

Generation **refuses to replace or reuse an existing crawler-compose
directory**, including a symlink, and does not change previously generated
files. Inspect and manually remove an obsolete generated directory only
after saving anything you need from it.

## What the scaffold configures

- Exactly one container, `crawl4ai`, on a dedicated, non-internal
  `crawler-egress` Docker bridge with IPv6 disabled, with the stable
  `lsm-crawler0` interface name for the nftables guard. Recheck interface
  ownership and firewall rules before deploying.
- Only the host-side loopback port `127.0.0.1:11235:11235`.
- Explicit public IPv4 DNS entries `1.1.1.1` and `9.9.9.9`.
- The existing Crawl4AI API token and selected image reference.
- No privileged mode, extra capabilities, host network, other shared
  networks or extra published ports.

It intentionally has **no automatic volumes or additional environment
variables**. Add any documented, required volumes, app settings and resource
limits after comparing the **real** existing deployment. Check the dedicated
Docker network has no other attached containers.

## Inspect first, migrate only when ready

For an existing Docker Compose deployment such as
`/home/nikola/hermes-web/docker-compose.yml`, examine its service definition
and local volumes first. Other Hermes Web services may depend on the crawler's
service name or the current `hermes-web_default` network. Moving Crawl4AI onto
a dedicated network can **break** that connectivity. The generated project
does not migrate those consumers. Decide on a safe access architecture before
replacing any existing service. Back up Docker volumes and record the current
image, persistent configuration and nonsecret settings. Do not paste Docker
inspect output containing environment secrets into public logs.

To validate the new standalone project **without** starting anything:

```sh
docker compose -f "$HOME/.config/local-search-mcp/crawler-compose/compose.yaml" config --quiet
```

Adjust the path if you use a custom `XDG_CONFIG_HOME`. A passing Compose
schema check is not a network-isolation check. Running the new project while
an existing crawler owns port 11235 or the container name `crawl4ai` will
conflict. This conflict is intentional: **do not run both stacks in parallel**.
After an operator-approved migration, bring up only the reviewed project with
an explicit `docker compose -f ... up -d`. Do not use `down -v`, or
blindly delete the old container, networks or volumes.

With the new container running, keep the remote tunnel stopped while applying
the firewall and validating traffic:

```sh
python3 scripts/crawler_egress_guard.py --container crawl4ai --print
sudo python3 scripts/crawler_egress_guard.py --container crawl4ai --apply
sudo python3 scripts/crawler_egress_guard.py --container crawl4ai --status
sudo python3 scripts/crawler_egress_guard.py --container crawl4ai --probe
```

Only permit remote crawler access after the status, negative/private-target
and positive/public-target probes succeed, plus a real approved public-page
crawl. The guard still cannot block container-internal loopback and is **not
persistent after reboot or Docker network recreation**. Ensure the firewall
is reinstalled and probed **before** restoring unattended remote access; this
Compose generator does not configure startup ordering or the host firewall.

This is an opt-in deployment scaffold, not an automatic migration or proof
of production readiness.
