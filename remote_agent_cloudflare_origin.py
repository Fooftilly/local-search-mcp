"""Optional, loopback-only Cloudflare private-origin Host adapter.

This is NOT a Cloudflare route or an Access authenticator. It only translates
an exact private HTTP Host to the existing authenticated loopback gateway.
Cloudflare private hostname reachability must be verified independently.
Nothing binds, provisions, or modifies host services on import or --check.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import sys
import time

import httpx
import uvicorn

from remote_agent_credentials import default_store_path
from remote_agent_gateway import (
    MAX_BODY_SECONDS, MAX_HEADER_BYTES, MAX_HEADERS, MAX_REQUEST_BYTES,
    MAX_RESPONSE_SECONDS, MAX_RESPONSE_START_SECONDS, _BEARER, _reply,
)
from remote_agent_prerequisites import (
    CredentialStoreError, _check_parent_chain, fixed_root_tunnel_gate,
    load_client_grants,
)
from remote_agent_server import StartupRefused, preflight_local

ORIGIN_HOST = "127.0.0.1"
ORIGIN_PORT = 18791
GATEWAY_URL = "http://127.0.0.1:18790/mcp"
SCHEMA = "local-search-mcp/cloudflare-private-origin/v1"
CLIENT_ID = "cloudflare-portal"
REQUEST_HEADERS = (b"authorization", b"accept", b"content-type",
                   b"mcp-protocol-version")
RESPONSE_HEADERS = (b"content-type", b"mcp-protocol-version", b"retry-after")
HOST_PATTERN = re.compile(
    r"(?=.{4,253}\\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\\Z",
    re.ASCII,
)


def validate_hostname(value: str) -> str:
    """Only one exact DNS hostname, never an address, URL, port or wildcard."""
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("Invalid private hostname")
    host = value.lower()
    if (not HOST_PATTERN.fullmatch(host) or host.endswith(".localhost")
            or host.endswith(".local") or host.endswith(".invalid")
            or host.endswith(".example") or host.endswith(".test")):
        raise ValueError("Invalid private hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("IP literals are not private hostname routes")


def default_config_path() -> Path:
    return (Path(pwd.getpwuid(os.geteuid()).pw_dir)
            / ".config" / "local-search-mcp" / "private"
            / "cloudflare-portal-origin.json")


def read_private_hostname(path: Path) -> str:
    """Require the same private owner-only path protections as grant files."""
    try:
        _check_parent_chain(path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 2048):
            raise ValueError("Unsafe private origin configuration")
        with path.open("rb") as stream:
            raw = stream.read(2049)
        if len(raw) > 2048:
            raise ValueError("Oversized private origin configuration")
        data = json.loads(raw)
        if type(data) is not dict or set(data) != {"schema", "hostname"}:
            raise ValueError("Invalid private origin configuration")
        if data["schema"] != SCHEMA:
            raise ValueError("Unsupported private origin schema")
        return validate_hostname(data["hostname"])
    except (OSError, ValueError, TypeError, CredentialStoreError):
        raise StartupRefused("Missing or unsafe private origin configuration") from None


def check_portal_grant() -> None:
    """No token material returned, and no implicit token is ever created."""
    try:
        grants = load_client_grants(default_store_path())
    except CredentialStoreError:
        raise StartupRefused("Missing or unsafe remote MCP credential store") from None
    if not any(g.client_id == CLIENT_ID and g.expires_at > time.time()
               for g in grants):
        raise StartupRefused("No active cloudflare-portal credential")


class CloudflarePrivateOrigin:
    """Exact private-host ingress; all identity and guard decisions stay at 18790."""

    def __init__(self, hostname: str, upstream: httpx.AsyncClient):
        self.hostname = validate_hostname(hostname)
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if (scope.get("path") != "/mcp" or scope.get("raw_path", b"/mcp") != b"/mcp"
                or scope.get("query_string", b"")):
            await _reply(send, 404)
            return
        method = scope.get("method")
        if method not in ("POST", "GET", "DELETE"):
            await _reply(send, 405)
            return
        peer = scope.get("client")
        if not peer or peer[0] not in ("127.0.0.1", "::1"):
            await _reply(send, 403)
            return
        raw = scope.get("headers", [])
        if (len(raw) > MAX_HEADERS
                or sum(len(k) + len(v) for k, v in raw) > MAX_HEADER_BYTES):
            await _reply(send, 431)
            return
        names: dict[bytes, list[bytes]] = {}
        for k, v in raw:
            names.setdefault(k.lower(), []).append(v)
        host = names.get(b"host", [])
        if (host != [self.hostname.encode("ascii")]
                or b"origin" in names or b"transfer-encoding" in names
                or b"mcp-session-id" in names or b"last-event-id" in names
                or any(k.startswith(b"x-forwarded-") or k in (
                    b"forwarded", b"proxy-authorization", b"cookie",
                    b"cf-access-client-id", b"cf-access-client-secret",
                ) for k in names)):
            await _reply(send, 403)
            return
        tokens = names.get(b"authorization", [])
        if len(tokens) != 1:
            await _reply(send, 401)
            return
        try:
            allowed = _BEARER.fullmatch(tokens[0].decode("ascii")) is not None
        except UnicodeDecodeError:
            allowed = False
        if not allowed:
            await _reply(send, 401)
            return
        selected: dict[str, str] = {}
        for k in REQUEST_HEADERS:
            values = names.get(k, [])
            if len(values) > 1 or any(len(v) > 512 for v in values):
                await _reply(send, 400)
                return
            if values:
                selected[k.decode("ascii")] = values[0].decode("latin1")
        length = names.get(b"content-length", [])
        if (len(length) > 1 or (length and (
                not length[0].isdigit() or int(length[0]) > MAX_REQUEST_BYTES))):
            await _reply(send, 413)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(MAX_BODY_SECONDS):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    if event["type"] != "http.request":
                        await _reply(send, 400)
                        return
                    chunk = event.get("body", b"")
                    if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                        await _reply(send, 413)
                        return
                    body.extend(chunk)
                    if not event.get("more_body", False):
                        break
        except TimeoutError:
            await _reply(send, 408)
            return
        if (method == "POST" and not body) or (method != "POST" and body):
            await _reply(send, 400)
            return
        response = None
        started = False
        try:
            # This endpoint and Host are fixed. httpx never consults proxy env.
            request = self.upstream.build_request(
                method, GATEWAY_URL, headers=selected, content=bytes(body))
            async with asyncio.timeout(MAX_RESPONSE_START_SECONDS):
                response = await self.upstream.send(request, stream=True)
            if 300 <= response.status_code < 400:
                await _reply(send, 502)
                return
            if "mcp-session-id" in response.headers:
                await _reply(send, 502)
                return
            safe = [(k, v) for k, v in response.headers.raw
                    if k.lower() in RESPONSE_HEADERS]
            safe += [(b"cache-control", b"no-store"),
                     (b"x-content-type-options", b"nosniff")]
            await send({"type": "http.response.start", "status": response.status_code,
                        "headers": safe})
            started = True
            async def forward():
                async for chunk in response.aiter_raw():
                    if chunk:
                        await send({"type": "http.response.body", "body": chunk,
                                    "more_body": True})

            async def disconnect():
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return

            task = asyncio.create_task(forward())
            watcher = asyncio.create_task(disconnect())
            try:
                completed, _ = await asyncio.wait(
                    (task, watcher), timeout=MAX_RESPONSE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED)
                if task in completed:
                    await task
                    await send({"type": "http.response.body", "body": b"",
                                "more_body": False})
            finally:
                for item in (task, watcher):
                    if not item.done():
                        item.cancel()
                await asyncio.gather(task, watcher, return_exceptions=True)
        except (httpx.HTTPError, OSError, TimeoutError):
            if not started:
                await _reply(send, 502)
            # After a streamed response has begun, fail closed by closing it.
        finally:
            if response is not None:
                await response.aclose()


def build_config(app) -> uvicorn.Config:
    return uvicorn.Config(
        app, host=ORIGIN_HOST, port=ORIGIN_PORT, workers=1, proxy_headers=False,
        access_log=False, server_header=False, date_header=False, lifespan="off",
        ws="none", limit_concurrency=16, backlog=16, log_level="warning",
    )


async def serve(path: Path) -> None:
    """No listener before grants, root guard, and all 12 tools pass preflight."""
    hostname = read_private_hostname(path)
    check_portal_grant()
    await preflight_local(default_store_path(), guard_probe=fixed_root_tunnel_gate)
    try:
        with socket.create_connection(("127.0.0.1", 18790), timeout=2):
            pass
    except OSError:
        raise StartupRefused("Authenticated loopback gateway is not listening") from None
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(connect=3, read=15, write=15, pool=3),
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=2),
    ) as client:
        await uvicorn.Server(build_config(CloudflarePrivateOrigin(
            hostname, client))).serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--serve-loopback", action="store_true",
                        help="explicitly serve the private Host adapter on 127.0.0.1:18791")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise StartupRefused("Run as the unprivileged account owner")
        hostname = read_private_hostname(default_config_path())
        check_portal_grant()
        if not args.serve_loopback:
            print("Private Cloudflare origin configuration and dedicated grant valid; "
                  "no route, listener or service started. Gateway and private routing "
                  "remain unverified.")
            return 0
        asyncio.run(serve(default_config_path()))
        return 0
    except StartupRefused as exc:
        print("Private Cloudflare origin refused: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError):
        print("Private Cloudflare origin unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
