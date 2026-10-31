"""Opt-in, loopback-only ASGI factory; deliberately NO server launcher (#230).

The factory never binds a port or starts a tunnel, Docker, nftables, or
systemd. A separate reviewed/authorized loopback-only server runner and
authenticated HTTPS ingress are required before a remote agent can connect.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, AsyncIterator

import httpx

from remote_agent_gateway import LoopbackMCPGateway
from remote_agent_prerequisites import fixed_root_tunnel_gate, load_client_grants

# All requests ultimately target the gateway's hardcoded 127.0.0.1:8790/mcp.
# Never accept a user-supplied destination or inherit HTTP_PROXY settings.
UPSTREAM_LIMITS = httpx.Limits(
    max_connections=8,
    max_keepalive_connections=2,
    keepalive_expiry=5.0,
)
UPSTREAM_TIMEOUT = httpx.Timeout(
    connect=3.0, read=15.0, write=15.0, pool=3.0,
)


@asynccontextmanager
async def open_guarded_runtime(
    credentials_path: str | Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    guard_probe: Callable[[], bool] = fixed_root_tunnel_gate,
) -> AsyncIterator[LoopbackMCPGateway]:
    """Build one bounded gateway; close its upstream client on exit.

    `transport` and `guard_probe` are injection points for isolated test
    transports; neither changes the fixed MCP target in normal operation.
    The file is intentionally read per request, not at factory creation,
    and the host guard is freshly probed before any upstream forwarding.
    """
    path = Path(credentials_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Provide a private absolute credential-file path")
    if not callable(guard_probe):
        raise ValueError("A guard probe must be callable")

    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=UPSTREAM_TIMEOUT,
        limits=UPSTREAM_LIMITS,
        transport=transport,
    ) as client:
        yield LoopbackMCPGateway(
            upstream=client,
            grants=lambda: load_client_grants(path),
            guard_ready=guard_probe,
        )
