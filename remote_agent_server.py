"""Explicitly opt-in loopback MCP gateway runner; never a public ingress.

The default invocation is a read-only configuration check. Only the exact
`--serve-loopback` command can bind, and only 127.0.0.1:18790 is supported.
Nothing installs units, credentials, firewall rules or a public tunnel.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Callable

import httpx
import uvicorn

from remote_agent_credentials import default_store_path
from remote_agent_prerequisites import (
    CredentialStoreError, fixed_root_tunnel_gate, load_client_grants,
)
from remote_agent_runtime import open_guarded_runtime

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 18790
STARTUP_GUARD_SECONDS = 32
MCP_DISCOVERY_URL = "http://127.0.0.1:8790/mcp"
MAX_DISCOVERY_BYTES = 512 * 1024
MCP_DISCOVERY_SECONDS = 8
REQUIRED_MCP_TOOLS = frozenset((
    "web_search", "web_search_capabilities", "web_search_batch",
    "web_fetch", "web_fetch_batch", "web_fetch_page", "web_table_page",
    "web_links_page", "web_find", "web_extract_section",
    "web_compare_documents", "web_metrics",
))


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


def _verify_tool_list(body: bytes, content_type: str) -> None:
    """Validate stateless MCP tools/list in bounded JSON or SSE framing."""
    try:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "application/json":
            messages = [json.loads(body)]
        elif media_type == "text/event-stream":
            # Streamable HTTP may frame one JSON-RPC response as an SSE event.
            # Never retain or print event payloads after validation.
            blocks = body.decode("utf-8").replace("\r\n", "\n").split("\n\n")
            messages = []
            for block in blocks:
                data = [line[5:].lstrip()
                        for line in block.split("\n") if line.startswith("data:")]
                if data:
                    messages.append(json.loads("\n".join(data)))
        else:
            raise ValueError("Unsupported MCP content type")
        replies = [msg for msg in messages if isinstance(msg, dict)
                   and msg.get("jsonrpc") == "2.0" and msg.get("id") == 1
                   and isinstance(msg.get("result"), dict)]
        if len(replies) != 1:
            raise ValueError("Invalid MCP discovery reply")
        tools = replies[0]["result"]["tools"]
        if (not isinstance(tools, list)
                or not all(isinstance(t, dict)
                           and isinstance(t.get("name"), str) for t in tools)):
            raise ValueError("Invalid MCP tool inventory")
        names = [t["name"] for t in tools]
        if len(names) != len(set(names)) or not REQUIRED_MCP_TOOLS.issubset(names):
            raise ValueError("Expected MCP tools missing or duplicated")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        raise StartupRefused("Loopback MCP tool discovery did not pass") from None


async def preflight_local(
    path: Path,
    *,
    guard_probe: Callable[[], bool] = fixed_root_tunnel_gate,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Explicit read-only readiness: grants, root guard, then local tools/list.

    Unlike --check, this deliberately invokes the existing sudo-approved,
    read-only root guard probe and makes one POST to the *fixed* loopback MCP
    endpoint. It never serves, provisions, wakes search engines or runs tools.
    """
    active = check_local_config(path)
    try:
        healthy = await asyncio.wait_for(
            asyncio.to_thread(guard_probe), STARTUP_GUARD_SECONDS)
    except (OSError, ValueError, TimeoutError):
        healthy = False
    if healthy is not True:
        raise StartupRefused("Root-owned crawler egress guard is not ready")

    try:
        async with asyncio.timeout(MCP_DISCOVERY_SECONDS):
            async with httpx.AsyncClient(
                transport=transport, trust_env=False,
                follow_redirects=False, timeout=3.0,
            ) as client:
                async with client.stream(
                    "POST", MCP_DISCOVERY_URL,
                    headers={"accept": "application/json, text/event-stream",
                             "mcp-protocol-version": "2025-06-18"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                ) as response:
                    if response.status_code != 200:
                        raise StartupRefused("Loopback MCP discovery unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_DISCOVERY_BYTES:
                            raise StartupRefused("Loopback MCP response exceeds limit")
                        body.extend(chunk)
                    _verify_tool_list(bytes(body),
                                      response.headers.get("content-type", ""))
    except (TimeoutError, httpx.HTTPError, OSError):
        raise StartupRefused("Loopback MCP discovery unavailable") from None
    return active


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
    mode.add_argument("--preflight", action="store_true",
                      help="explicit read-only guard and localhost tools/list check; no listener")
    args = parser.parse_args(argv)
    try:
        path = default_store_path()
        if args.preflight:
            clients = asyncio.run(preflight_local(path))
            print(f"Preflight passed: {clients} active client grant(s), "
                  f"root guard ready, {len(REQUIRED_MCP_TOOLS)} required MCP tools. "
                  "Nothing started or changed.")
            return 0
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
