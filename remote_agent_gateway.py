"""Import-only, loopback-only *prototype* MCP authentication boundary (#230).

No server/CLI/tunnel is started by importing this module. This is not ready
for deployment: it lacks an installed secret store, rate limits, active stream
revocation and a verified external tunnel/identity provider. Its sole upstream
is the existing locally bound FastMCP Streamable HTTP endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re
import time
from typing import Callable, Sequence

import httpx

UPSTREAM = "http://127.0.0.1:8790/mcp"
MAX_REQUEST_BYTES = 128 * 1024
MAX_HEADER_BYTES = 8192
MAX_HEADERS = 48
MAX_GRANTS = 8
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]{32,128})\Z", re.ASCII)
_LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1)(?::([1-9][0-9]{0,4}))?\Z", re.ASCII)
_FORWARD_REQUEST_HEADERS = (
    b"accept", b"content-type", b"mcp-session-id",
    b"mcp-protocol-version", b"last-event-id",
)
_FORWARD_RESPONSE_HEADERS = (
    b"content-type", b"mcp-session-id", b"mcp-protocol-version",
    b"retry-after",
)


@dataclass(frozen=True)
class ClientGrant:
    """A high-entropy bearer verifier; never hold an un-hashed token here."""
    client_id: str
    token_sha256: bytes
    expires_at: float

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", self.client_id):
            raise ValueError("Invalid client id")
        if len(self.token_sha256) != 32:
            raise ValueError("Expected a SHA-256 digest")


def synthetic_grant(token: str, client_id: str, expires_at: float) -> ClientGrant:
    """Construct a verifier for controlled tests only; no persistence."""
    if not _BEARER.fullmatch("Bearer " + token):
        raise ValueError("Expected a 32-128 character URL-safe bearer token")
    return ClientGrant(client_id, hashlib.sha256(token.encode("ascii")).digest(),
                       expires_at)


async def _reply(send, code: int) -> None:
    """Uniform non-disclosing errors; never echo requests or auth material."""
    body = b"Request rejected"
    headers = [(b"content-type", b"text/plain; charset=utf-8"),
               (b"cache-control", b"no-store"),
               (b"x-content-type-options", b"nosniff")]
    if code == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class LoopbackMCPGateway:
    """Testable ASGI forwarding boundary; not a public deployment entrypoint.

    Callers must provide a running, constrained httpx.AsyncClient and explicit
    guard-ready and credential-provider callbacks. All are checked *before*
    the gateway forwards any bytes or calls the existing MCP application.
    The gateway has intentionally no `uvicorn.run`, server settings or token
    configuration, so it cannot enable a public endpoint by itself.
    """

    def __init__(
        self,
        *,
        upstream: httpx.AsyncClient,
        grants: Callable[[], Sequence[ClientGrant]],
        guard_ready: Callable[[], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(grants) or not callable(guard_ready):
            raise ValueError("Explicit credential and guard providers required")
        self._upstream = upstream
        self._grants = grants
        self._guard_ready = guard_ready
        self._clock = clock

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        # Never interpret proxy-supplied targets or queries as upstream routes.
        if scope.get("path") != "/mcp" or scope.get("raw_path", b"/mcp") != b"/mcp" or scope.get("query_string", b""):
            await _reply(send, 404)
            return
        method = scope.get("method")
        if method not in ("POST", "GET", "DELETE"):
            await _reply(send, 405)
            return
        client = scope.get("client")
        if not client or client[0] not in ("127.0.0.1", "::1"):
            await _reply(send, 403)
            return
        raw = scope.get("headers", [])
        if (len(raw) > MAX_HEADERS or
                sum(len(k) + len(v) for k, v in raw) > MAX_HEADER_BYTES):
            await _reply(send, 431)
            return
        names = {}
        for key, value in raw:
            key = key.lower()
            names.setdefault(key, []).append(value)
        hosts = names.get(b"host", [])
        try:
            valid_host = len(hosts) == 1 and bool(_LOCAL_HOST.fullmatch(hosts[0].decode("ascii")))
            valid_host = valid_host and (b":" not in hosts[0] or
                                         int(hosts[0].rsplit(b":", 1)[1]) <= 65535)
        except (UnicodeDecodeError, ValueError):
            valid_host = False
        if not valid_host or b"origin" in names or b"transfer-encoding" in names:
            await _reply(send, 403)
            return

        authorizations = names.get(b"authorization", [])
        if len(authorizations) != 1:
            await _reply(send, 401)
            return
        try:
            match = _BEARER.fullmatch(authorizations[0].decode("ascii"))
        except UnicodeDecodeError:
            match = None
        if match is None:
            await _reply(send, 401)
            return
        digest = hashlib.sha256(match.group(1).encode("ascii")).digest()
        # Iterating over bounded digests avoids exposing a token-length
        # dependent raw-secret comparison. Providers are checked each request:
        # removing a grant revokes new requests without restarting the app.
        try:
            current = tuple(self._grants())
            if len(current) > MAX_GRANTS or not all(
                    isinstance(item, ClientGrant) for item in current):
                raise ValueError("Invalid grant configuration")
            now = self._clock()
            allowed = False
            for item in current:
                equal = hmac.compare_digest(digest, item.token_sha256)
                allowed = bool(equal and item.expires_at > now) or allowed
        except (ValueError, TypeError, OSError):
            await _reply(send, 503)
            return
        if not allowed:
            await _reply(send, 401)
            return
        # Guard failures must never reach the upstream or create a Docker lease.
        try:
            if self._guard_ready() is not True:
                await _reply(send, 503)
                return
        except Exception:
            await _reply(send, 503)
            return
        try:
            length = names.get(b"content-length", [])
            if len(length) > 1 or (length and (
                    not length[0].isdigit() or int(length[0]) > MAX_REQUEST_BYTES)):
                await _reply(send, 413)
                return
            chunks = bytearray()
            while True:
                incoming = await receive()
                if incoming["type"] == "http.disconnect":
                    return
                if incoming["type"] != "http.request":
                    await _reply(send, 400)
                    return
                chunk = incoming.get("body", b"")
                if len(chunks) + len(chunk) > MAX_REQUEST_BYTES:
                    await _reply(send, 413)
                    return
                chunks.extend(chunk)
                if not incoming.get("more_body", False):
                    break
            if method != "POST" and chunks:
                await _reply(send, 400)
                return
            if method == "POST" and not chunks:
                await _reply(send, 400)
                return
            safe_headers = {}
            for name in _FORWARD_REQUEST_HEADERS:
                values = names.get(name, [])
                if len(values) > 1 or any(len(v) > 512 for v in values):
                    await _reply(send, 400)
                    return
                if values:
                    safe_headers[name.decode("ascii")] = values[0].decode("latin1")
            # This gateway *never* forwards Authorization, Cookie, Host,
            # Forwarded, Origin, X-Forwarded-*, or arbitrary client headers.
            async with self._upstream.stream(
                method, UPSTREAM, headers=safe_headers, content=bytes(chunks),
                follow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400:
                    await _reply(send, 502)
                    return
                response_headers = [
                    (key, value) for key, value in response.headers.raw
                    if key.lower() in _FORWARD_RESPONSE_HEADERS
                ]
                response_headers.extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ])
                await send({"type": "http.response.start",
                            "status": response.status_code,
                            "headers": response_headers})
                async for body in response.aiter_raw():
                    # Backpressure and cancellation are delegated to the ASGI
                    # server; httpx's stream context always closes upstream.
                    if body:
                        await send({"type": "http.response.body",
                                    "body": body, "more_body": True})
                await send({"type": "http.response.body", "body": b"",
                            "more_body": False})
        except (httpx.HTTPError, OSError):
            # The upstream is unavailable. If streaming has already begun
            # there is no safe way to replace status/headers with a 502, so
            # abort the stream and let the ASGI server close the connection.
            raise
