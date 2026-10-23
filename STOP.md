# Controlled shutdown (issue #70)

From the repository checkout, run:

`./stop.sh`

This command stops and disables **only verified local-search-mcp user systemd
units** and **only the containers labeled as belonging to its own private
Crawl4AI/SearXNG Compose project**. The tunnel is shut down first. Docker
restart policies are set to \`no\` before scoped Compose stops, preventing
those existing containers from restarting when Docker or the host restarts.
It does not delete containers, images, volumes, policies, history, credentials,
the Git checkout, or firewall rules.

If no managed service, backend or optional SearXNG overlay is installed, that
part is skipped. Shutdown errors are aggregated; a nonzero exit status means
some part of the shutdown could not be verified and **the stack must not be
assumed offline**. In particular, an unrecognized systemd unit or a Compose
container whose original file ownership labels do not match the private
project is not modified.

**Existing Hermes Web deployment:** a legacy \`crawl4ai\` container started
from \`/home/nikola/hermes-web/docker-compose.yml\` is NOT part of the
local-search-mcp-managed Compose project. This script deliberately does not
stop or disable it. Inspect that separate deployment and its restart policy
manually; do not treat a matching container name as proof of ownership.

## Optional root-managed firewall service

If the opt-in root-owned service from PR #68 was installed, the script
reports that administrator action is needed and exits nonzero. After
confirming the tunnel is stopped, use \`sudo systemctl disable --now
local-search-mcp-crawler-egress.service\` as an **explicit operator action**
only if appropriate. That service's stop action does not remove the
nftables egress table. Do not remove the firewall table during routine
shutdown; if you intend a separate network-policy rollback, follow the
reviewed PR #68 rollback instructions after remote crawler access is off.

## Restart safely

1. Review the private Compose files and bring up only the necessary backend
   services using your selected, already installed images; do not auto-pull
   mutable tags.
2. Apply and **probe** the host/container egress guard against the live
   crawler before permitting remote access. A previously installed firewall
   rule is not proof that Docker preserved the same network after restart.
3. Start the bridge user service, validate local health, then start the
   tunnel. Re-enable health/update watchers only if you want them.

Restarting containers with \`docker compose up\` may restore the Compose
file's restart policy; \`./stop.sh\` has no permanent effect on future
explicit deployments. The script never installs services or enables units.

## Operator smoke check

In an approved maintenance window: snapshot service/container statuses,
run \`./stop.sh\`, confirm bridge/tunnel/timers/path units disabled and
inactive, confirm only local-search-mcp Compose containers are stopped and
have restart policy \`no\`, then confirm that they remain stopped after a
reboot. Repeat with the optional SearXNG overlay and with only Crawl4AI.
Live Docker, systemd and firewall tests are **not** performed by CI.
