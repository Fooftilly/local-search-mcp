"""Bounded, read-only loopback transport diagnostics, with no secrets.

TCP reachability does not prove service health, authentication, or container
isolation. The dashboard never invokes Docker, systemctl, or remote URLs.
"""
from __future__ import annotations

import errno
import os
import socket
from urllib.parse import urlsplit


SERVICES = (
    ("SearXNG", "SEARXNG_URL", "http://127.0.0.1:8888"),
    ("Crawl4AI", "CRAWL4AI_URL", "http://127.0.0.1:11235"),
)


def _endpoint(raw: str) -> tuple[str, int] | None:
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        # None means absent; an explicit :0 must remain invalid, not fall
        # back to the default service port.
        port = (parsed.port if parsed.port is not None
                else (443 if parsed.scheme == "https" else 80))
        if (parsed.scheme not in ("http", "https") or host not in
                ("localhost", "127.0.0.1", "::1") or
                parsed.username is not None or parsed.password is not None or
                not 1 <= port <= 65535):
            return None
        # Never resolve localhost via DNS or /etc/hosts: pin it to loopback.
        return ("127.0.0.1" if host == "localhost" else host), port
    except (ValueError, TypeError):
        return None


def _supervisor_activity() -> dict | None:
    """Snapshot in-process supervisor state only; never wake or inspect Docker."""
    try:
        from scripts import on_demand_supervisor
        return on_demand_supervisor.local_status()
    except (ImportError, OSError, ValueError, RuntimeError):
        return None


def _expected_sleeping(activity: dict | None, backend: str) -> bool:
    """Only an actual initialized on-demand supervisor may explain an absent TCP listener.

    Snapshot state is advisory, not a substitute for verified Docker/egress
    health; preserve "TCP unavailable" for failed or ambiguous states.
    """
    if (type(activity) is not dict or activity.get("enabled") is not True
            or activity.get("mode") != "on-demand"):
        return False
    entries = activity.get("backends")
    if type(entries) is not dict or set(entries) != {"searxng", "crawl4ai"}:
        return False
    row = entries.get(backend)
    return (type(row) is dict and row.get("state") == "sleeping"
            and row.get("armed") is False
            and type(row.get("active_requests")) is int
            and row["active_requests"] == 0
            and row.get("stop_claimed") is False)


def probe_services(*, connector=socket.create_connection) -> list[dict]:
    """Return transport results annotated by optional *reported* idle state."""
    results = []
    activity = None
    checked_activity = False
    for label, variable, default in SERVICES:
        target = _endpoint(os.environ.get(variable, default))
        if target is None:
            results.append({"service": label, "status": "configuration invalid",
                            "source": "environment" if variable in os.environ
                            else "default"})
            continue
        try:
            with connector(target, timeout=0.3):
                status = "TCP reachable"
        except OSError as exc:
            status = "TCP unavailable"
            # An absent listener normally refuses a TCP connection. Timeouts,
            # address/routing errors and local socket exhaustion are failures,
            # not evidence that Docker intentionally stopped a backend.
            refused = (isinstance(exc, ConnectionRefusedError)
                       or getattr(exc, "errno", None) == errno.ECONNREFUSED)
            # The supervisor only owns the fixed loopback backend endpoints;
            # an overridden URL may refer to an unrelated service.
            if refused and target == _endpoint(default):
                if not checked_activity:
                    activity = _supervisor_activity()
                    checked_activity = True
                backend = "searxng" if label == "SearXNG" else "crawl4ai"
                if _expected_sleeping(activity, backend):
                    status = "TCP unavailable (supervisor reports sleeping)"
        results.append({"service": label, "status": status,
                        "source": "environment" if variable in os.environ
                        else "default"})
    return results


def configured_ports() -> list[dict]:
    """Display bounded loopback ports only; never emit upstream URLs or secrets.

    Changing a backend URL in the environment requires a service restart.
    The dashboard cannot bind/reconfigure a Docker service or MCP listener.
    """
    results = []
    for label, variable, default in SERVICES:
        target = _endpoint(os.environ.get(variable, default))
        results.append({
            "service": label,
            "port": target[1] if target is not None else None,
            "source": "environment" if variable in os.environ else "default",
            "apply": "bridge restart required" if variable in os.environ
                     else "fixed default unless overridden",
            "valid": target is not None,
        })
    return results
