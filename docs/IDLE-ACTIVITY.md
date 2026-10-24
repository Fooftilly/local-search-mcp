# Inert MCP idle-activity accounting (issue #75)

`scripts.backend_idle_activity.IdleActivity` is a pure in-memory,
thread-safe activity accounting primitive for a **future**, optional
on-demand backend supervisor. It is not imported by the active MCP bridge or
started by the setup wizard. It never calls Docker, systemd or firewall tools,
opens a port, starts a timer thread, or sleeps/stops/wakes a backend.

A future owner-validated controller must:

1. Confirm a backend is running and its security checks succeeded, then call
   `arm_running("searxng")` or `arm_running("crawl4ai")`.
2. Wrap **actual upstream work** in `with tracker.work(service):` so long
   operations, errors and overlapping requests hold active leases.
3. Invoke `claim_idle_stop(service)` under its own lifecycle coordination;
   this returns true only after the full quiet period (1800 seconds by default)
   and grants an atomic stop claim. Health/dashboard/status polling must not
   take a work lease or reset the clock.
4. Recheck container identity, firewall/egress policy and in-flight access
   before any hypothetical Docker operation. On success call
   `finish_stop(service, stopped=True)`; on failure call
   `finish_stop(service, stopped=False)`, restarting the idle window.
5. `disarm(service)` when no idle stop should be planned. **Disarm does not
   disable MCP requests or stop services**: future full-stop and wake
   suppression require a separate persistent supervisor shutdown gate.

The idle counter does not itself serialize Docker startup, share wake attempts,
preserve restart policies, implement cross-process locking, or remain persistent
across crashes/reboots. `BackendBusy` must be surfaced as a retryable
transition result, not treated as a successful MCP fetch. The tracker is
restricted to the two fixed service names and exposes only activity counts,
time elapsed and claimed-state booleans. Tests use an injected monotonic clock
and concurrent stop claimants.

**No on-demand mode is enabled by this library.** The installation and CLI
remain unchanged and `on_demand_eligibility.can_activate` stays `false`.
Integration belongs to later #75 PRs after PR #94 and the documented firewall,
full-stop and Docker ownership gates are satisfied.
