# Scoped Docker lifecycle adapter (#75, second slice)

`scripts/on_demand_docker.py` exposes **explicit**, synchronous `start`
and `stop` operations to a future authenticated supervisor. Neither
operation is currently called by the MCP tools or setup wizard; no Docker
behavior changes by installing this slice.

Both operations select **full 64-character container IDs**, only after the
private Compose files and every container's Compose project/service/config
labels are re-checked. Exactly one managed SearXNG and one managed Crawl4AI
service must be present, and another published crawler on port 11235 is a
hard stop. All operations use the fixed local Docker socket; no Compose
`down`, container name targets, network/volume removal or image pull.
The project-local `unless-stopped` and `no` restart policies are preserved
unchanged for transient `docker stop`. Other policies are rejected rather
than secretly updated. Full `./stop.sh` retains its stronger
restart-disabled semantics.

A sleeping crawler `start` requires **two independent, explicit
security checks**: a successful pre-start firewall gate *before* issuing
`docker start`, and a separate **live egress probe** after wake but before
returning control to the MCP caller. If either callback is missing, fails,
raises or reports anything other than boolean `True`, activation is refused.
A failed pre-start gate issues **no Docker command**. An ambiguous Docker
start failure or post-start gate failure triggers a stop attempt against the
previously verified full container ID, even when a fresh inventory is
temporarily unavailable. A failed cleanup is also an explicit error, not
success. These callbacks must come from separately reviewed, root-owned
probing mechanisms with narrow authorization; never grant passwordless sudo
access to a user-writable Git script. The existing
`docs/EGRESS-SYSTEMD.md` explains the privileged root-owned checker, but
the default bridge user service has `NoNewPrivileges=true` and therefore
cannot itself execute an elevated `sudo -n` probe. A safe privileged
RPC/one-shot integration, full-stop suppression, and tests are prerequisites
before any automatic crawler wake.

All launch operations must be coordinated externally with the merged
`IdleActivity` lease/stop claims; this module alone is not an idle
supervisor and does not implement concurrent start coalescing, a timer or
a status endpoint. Verification gaps and timeouts fail with sanitized
errors. A backend started by this call is stopped on readiness/probe failure
if it remains verified. Operators should review and test the Docker
daemon restart behavior of the canonical `unless-stopped` policy, and
measure actual memory savings before declaring #75 complete.

No default install mode changes in this slice.
