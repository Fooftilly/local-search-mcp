"""Import-only, loopback-only *prototype* MCP authentication boundary (#230).

No server/CLI/tunnel is started by importing this module. This is not ready
for deployment: it lacks an installed secret store, production-grade distributed quotas
and a verified external tunnel/identity provider. Its sole upstream
is the existing locally bound FastMCP Streamable HTTP endpoint.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
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
MAX_CACHED_IDENTITIES = 64
MAX_PER_CLIENT_ACTIVE = 2
MAX_TOTAL_ACTIVE = 8
MAX_PER_CLIENT_PER_MINUTE = 30
MAX_RESPONSE_SECONDS = 120
MAX_BODY_SECONDS = 15
MAX_RESPONSE_START_SECONDS = 15
MAX_GUARD_SECONDS = 32
REVOCATION_POLL_SECONDS = 0.25
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]{32,128})\Z", re.ASCII | re.IGNORECASE)
_LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::(?P<port>[1-9][0-9]{0,4}))?\Z", re.ASCII)
_FORWARD_REQUEST_HEADERS = (
    b"accept", b"content-type", b"mcp-protocol-version",
)
_FORWARD_RESPONSE_HEADERS = (
    b"content-type", b"mcp-protocol-version", b"retry-after",
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




@asynccontextmanager
async def _upstream_response(client: httpx.AsyncClient, method: str,
                             headers: dict, content: bytes):
    """Hard deadline for upstream response headers; not the SSE body.

    HTTPX's per-read timeout alone does not bound a slow-dripping response.
    ExitStack owns the HTTPX stream context after the opening phase, so
    downstream disconnect, revocation and body timeouts still close it.
    """
    async with AsyncExitStack() as resources:
        async with asyncio.timeout(MAX_RESPONSE_START_SECONDS):
            response = await resources.enter_async_context(
                client.stream(method, UPSTREAM, headers=headers,
                              content=content, follow_redirects=False))
        yield response



class ClientRequestBudget:
    """Bounded, single-event-loop quota; never stores tokens or query content.

    Reserve/release have no await points and are atomic on the gateway's ASGI
    event loop. This does not implement cross-process distributed quotas.
    """
    def __init__(self):
        self._calls = {}
        self._active = {}
        self._active_total = 0

    def reserve(self, client_id: str, now: float) -> bool:
        # Remove stale, inactive identities so successive rotations cannot
        # accumulate unbounded per-client state in a long-lived gateway.
        for key in list(self._calls):
            recent = deque(t for t in self._calls[key] if 0 <= now - t < 60)
            if recent or self._active.get(key, 0):
                self._calls[key] = recent
            else:
                del self._calls[key]
        if (self._active_total >= MAX_TOTAL_ACTIVE or
                self._active.get(client_id, 0) >= MAX_PER_CLIENT_ACTIVE or
                len(self._calls.get(client_id, ())) >= MAX_PER_CLIENT_PER_MINUTE):
            return False
        # Recent counters outlive grant revocation so reinstating an ID
        # cannot reset its rate limit. A separate, bounded cache allowance
        # permits normal rotation of the eight live grants without 429s.
        if (client_id not in self._calls and
                len(self._calls) >= MAX_CACHED_IDENTITIES):
            return False
        self._calls.setdefault(client_id, deque()).append(now)
        self._active[client_id] = self._active.get(client_id, 0) + 1
        self._active_total += 1
        return True

    def release(self, client_id: str) -> None:
        current = self._active.get(client_id, 0)
        if current <= 0:
            raise RuntimeError("Unbalanced gateway request release")
        if current == 1:
            del self._active[client_id]
        else:
            self._active[client_id] = current - 1
        self._active_total -= 1


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
        rate_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(grants) or not callable(guard_ready):
            raise ValueError("Explicit credential and guard providers required")
        self._upstream = upstream
        self._grants = grants
        self._guard_ready = guard_ready
        self._clock = clock
        self._rate_clock = rate_clock
        self._budget = ClientRequestBudget()
        # Serializes expensive root guard checks. No cached attestation:
        # every authorized request must pass a fresh host read/probe.
        self._guard_slot = asyncio.Lock()
        self._guard_inflight: asyncio.Task[bool] | None = None

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
            authority = (_LOCAL_HOST.fullmatch(hosts[0].decode("ascii"))
                         if len(hosts) == 1 else None)
            valid_host = authority is not None and (
                authority.group("port") is None or
                int(authority.group("port")) <= 65535)
        except (UnicodeDecodeError, ValueError):
            valid_host = False
        if not valid_host or b"origin" in names or b"transfer-encoding" in names:
            await _reply(send, 403)
            return

        # The installed FastMCP bridge runs stateless_http=True.
        # Client-controlled opaque session/replay identifiers must not cross
        # client identities until an identity-bound session store exists.
        if b"mcp-session-id" in names or b"last-event-id" in names:
            await _reply(send, 400)
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
            client_id = None
            for item in current:
                equal = hmac.compare_digest(digest, item.token_sha256)
                if equal and item.expires_at > now:
                    client_id = item.client_id
        except (ValueError, TypeError, OSError):
            await _reply(send, 503)
            return
        if client_id is None:
            await _reply(send, 401)
            return
        if not self._budget.reserve(client_id, self._rate_clock()):
            await _reply(send, 429)
            return
        try:
            await self._forward_authenticated(
                names, method, receive, send, digest, client_id)
        finally:
            self._budget.release(client_id)

    def _credential_still_valid(self, digest: bytes, client_id: str) -> bool:
        """Revocation and expiration are checked throughout active streams."""
        try:
            current = tuple(self._grants())
            if len(current) > MAX_GRANTS or not all(
                    isinstance(item, ClientGrant) for item in current):
                return False
            now = self._clock()
            return any(
                hmac.compare_digest(digest, item.token_sha256) and
                item.client_id == client_id and item.expires_at > now
                for item in current)
        except Exception:
            return False

    @staticmethod
    def _consume_guard_task_outcome(task: asyncio.Task) -> None:
        """Avoid unobserved exceptions from probes completing after timeout."""
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _fresh_guard_ok(self) -> bool:
        """No concurrent root probe, including after caller timeout/cancel.

        Once a probe times out, it still owns the sole worker slot until the
        underlying thread completes. A later request is denied rather than
        starting overlapping Docker/nftables operations. Completed probes
        are never reused as permission to forward a later request.
        """
        try:
            # The deadline starts before acquiring the lock: a queued
            # request must not wait behind multiple 30-second guard probes.
            async with asyncio.timeout(MAX_GUARD_SECONDS):
                async with self._guard_slot:
                    if (self._guard_inflight is not None and
                            not self._guard_inflight.done()):
                        return False
                    probe = asyncio.create_task(
                        asyncio.to_thread(self._guard_ready))
                    probe.add_done_callback(self._consume_guard_task_outcome)
                    self._guard_inflight = probe
                    # Shield keeps the underlying worker tracked after the
                    # caller's deadline/cancellation releases the lock.
                    return await asyncio.shield(probe) is True
        except Exception:
            return False

    async def _forward_authenticated(
            self, names, method, receive, send, digest: bytes,
            client_id: str) -> None:
        response_started = False
        try:
            length = names.get(b"content-length", [])
            if len(length) > 1 or (length and (
                    not length[0].isdigit() or int(length[0]) > MAX_REQUEST_BYTES)):
                await _reply(send, 413)
                return
            chunks = bytearray()
            try:
                async with asyncio.timeout(MAX_BODY_SECONDS):
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
            except TimeoutError:
                await _reply(send, 408)
                return
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

            # After authorization and validation, verify the installed
            # guard without blocking the event loop or overlapping a probe
            # that has already timed out or whose caller disconnected.
            if not await self._fresh_guard_ok():
                await _reply(send, 503)
                return

            # This gateway *never* forwards Authorization, Cookie, Host,
            # Forwarded, Origin, X-Forwarded-*, or arbitrary client headers.
            async with _upstream_response(
                self._upstream, method, headers=safe_headers,
                content=bytes(chunks),
            ) as response:
                if 300 <= response.status_code < 400:
                    await _reply(send, 502)
                    return
                if "mcp-session-id" in response.headers:
                    # Stateful upstream sessions require a separate,
                    # identity-bound store and revocation mechanism.
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
                response_started = True
                async def forward():
                    async for body in response.aiter_bytes():
                        if body:
                            await send({"type": "http.response.body",
                                        "body": body, "more_body": True})

                async def watch_disconnect():
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return

                async def watch_revocation():
                    while True:
                        await asyncio.sleep(REVOCATION_POLL_SECONDS)
                        if not self._credential_still_valid(digest, client_id):
                            return

                # Enforce a finite response lifetime, active revocation, and
                # downstream disconnection even if upstream SSE stays silent.
                reader = asyncio.create_task(forward())
                watcher = asyncio.create_task(watch_disconnect())
                revoked = asyncio.create_task(watch_revocation())
                try:
                    completed, _ = await asyncio.wait(
                        (reader, watcher, revoked),
                        timeout=MAX_RESPONSE_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED)
                    if not completed or watcher in completed or revoked in completed:
                        return
                    await reader
                    await send({"type": "http.response.body", "body": b"",
                                "more_body": False})
                finally:
                    for task in (reader, watcher, revoked):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(reader, watcher, revoked,
                                         return_exceptions=True)
        except TimeoutError:
            # A response-header stall is distinct from a failed upstream
            # connection. Never leak its URL, request details or tokens.
            if response_started:
                raise
            await _reply(send, 504)
        except (httpx.HTTPError, OSError):
            # Before headers, report a generic failure without upstream
            # details. After headers, a connection abort is safer than
            # manufacturing an HTTP status in an active MCP/SSE stream.
            if response_started:
                raise
            await _reply(send, 502)
