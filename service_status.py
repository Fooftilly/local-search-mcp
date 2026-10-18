"""Bounded, read-only loopback transport diagnostics, with no secrets.

TCP reachability does not prove service health, authentication, or container
isolation. The dashboard never invokes Docker, systemctl, or remote URLs.
"""
from __future__ import annotations

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


def probe_services(*, connector=socket.create_connection) -> list[dict]:
    """Return transport-only results. Never disclose configured endpoint URLs."""
    results = []
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
        except (OSError, TimeoutError):
            status = "TCP unavailable"
        results.append({"service": label, "status": status,
                        "source": "environment" if variable in os.environ
                        else "default"})
    return results
