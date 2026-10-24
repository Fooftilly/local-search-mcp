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
The separate `health_cli.py` monitor probes fixed loopback ports:
**8888 for SearXNG**, **11235 for Crawl4AI**, and **8791 for the dashboard**.
It does not follow `SEARXNG_URL`, `CRAWL4AI_URL`, or
`LOCAL_SEARCH_MCP_DASHBOARD_PORT` overrides. Changing a backend connection
URL and restarting the bridge can therefore leave the MCP tools working
while the existing health timer incorrectly reports `degraded`. A custom
dashboard port has the same limitation. Coordinate any nondefault backend
or dashboard port with an explicit monitoring change before deployment;
this read-only status page cannot fix the health monitor.

The status panel executes no Docker, systemd, firewall, update or
configuration-writing command. It retains the same local Host/Origin and
Fetch Metadata restrictions as the existing management UI. Editing ports
via browser forms and disruptive restart buttons remain out of scope.
