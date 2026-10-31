"""Explicitly opt-in loopback MCP gateway runner; never a public ingress.

The default invocation is a read-only configuration check. Only the exact
`--serve-loopback` command can bind, and only 127.0.0.1:18790 is supported.
Nothing installs units, credentials, firewall rules or a public tunnel.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys
import time
from typing import Callable

import uvicorn

from remote_agent_credentials import default_store_path
from remote_agent_prerequisites import (
    CredentialStoreError, fixed_root_tunnel_gate, load_client_grants,
)
from remote_agent_runtime import open_guarded_runtime

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 18790
STARTUP_GUARD_SECONDS = 32


class StartupRefused(RuntimeError):
    """Safe operator-visible startup refusal, without private path leakage."""


def check_local_config(path: Path) -> int:
    """Validate grants without creating files, invoking sudo or binding."""
    if os.geteuid() == 0:
        raise StartupRefused("Run as the unprivileged account owner")
    try:
        grants = load_client_grants(path)
    except CredentialStoreError:
        raise StartupRefused("Missing or unsafe private credential store") from None
    valid = sum(grant.expires_at > time.time() for grant in grants)
    if valid == 0:
        raise StartupRefused("No active client grants configured")
    return valid


def build_uvicorn_config(app) -> uvicorn.Config:
    """One fixed, loopback-only, single-worker configuration.

    Never accept forwarded headers or configurable Host/port/worker count.
    Runtime quotas are in-process, so multi-worker would defeat them.
    """
    return uvicorn.Config(
        app,
        host=LOCAL_HOST,
        port=LOCAL_PORT,
        workers=1,
        proxy_headers=False,
        forwarded_allow_ips="",
        access_log=False,
        server_header=False,
        date_header=False,
        lifespan="off",
        ws="none",
        timeout_keep_alive=5,
        backlog=16,
        limit_concurrency=16,
        log_level="warning",
    )


async def run_loopback(
    path: Path,
    *,
    guard_probe: Callable[[], bool] = fixed_root_tunnel_gate,
) -> None:
    """Preflight guard and credentials *before* starting local listener."""
    check_local_config(path)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(guard_probe), STARTUP_GUARD_SECONDS)
    except (OSError, ValueError, TimeoutError):
        result = False
    if result is not True:
        raise StartupRefused("Root-owned crawler egress guard is not ready")

    # The gateway re-checks file grants and the root guard for every
    # authenticated, quota-admitted request. Startup is not cached as trust.
    async with open_guarded_runtime(path, guard_probe=guard_probe) as gateway:
        config = build_uvicorn_config(gateway)
        server = uvicorn.Server(config)
        await server.serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="read-only grant check; no sudo or listener (default)")
    mode.add_argument("--serve-loopback", action="store_true",
                      help="explicitly run authenticated gateway only on loopback")
    args = parser.parse_args(argv)
    try:
        path = default_store_path()
        clients = check_local_config(path)
        if not args.serve_loopback:
            print(f"Verified {clients} active client grant(s); nothing started.")
            return 0
        asyncio.run(run_loopback(path))
        return 0
    except StartupRefused as exc:
        print(f"Remote MCP gateway refused startup: {exc}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError):
        print("Remote MCP gateway could not start safely", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
