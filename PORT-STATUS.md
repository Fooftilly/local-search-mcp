# Local backend ports and restart behavior — issue #13

The dashboard's local `/status` page displays a **read-only** table with
the numeric loopback ports selected for SearXNG and Crawl4AI. Values are
derived from the configured `SEARXNG_URL`/`CRAWL4AI_URL` environment
values or the existing defaults (8888 and 11235). URLs, URL paths,
credentials and non-loopback addresses are never displayed. Malformed,
credential-bearing, or non-loopback endpoints are shown as invalid rather
than exposed or probed.

Backend *connection* URL changes require a **bridge restart** to be picked
up by the process. Changing the actual service container's published port
is a separate deployment operation and must not be attempted from the
dashboard: it requires a reviewed, explicitly authorized container
reconfiguration and corresponding bridge settings.

The MCP listener is currently fixed to localhost port 8790.
The dashboard's nominal port is 8791; although the dashboard can be
configured with `LOCAL_SEARCH_MCP_DASHBOARD_PORT`, the existing health
monitor expects port 8791 and can otherwise show a misleading failure.
Do not make port changes without coordinating monitoring.

The status panel executes no Docker, systemd, firewall, update or
configuration-writing command. It retains the same local Host/Origin and
Fetch Metadata restrictions as the existing management UI. Editing ports
via browser forms and disruptive restart buttons remain out of scope.
