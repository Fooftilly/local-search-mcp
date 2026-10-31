"""Non-deploying ASGI runtime factory and guard scheduling acceptance."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from remote_agent_prerequisites import SCHEMA
from remote_agent_runtime import open_guarded_runtime


class GuardedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "grants.json"
        self.token = "Z" * 48
        self.store([{
            "client_id": "grok-api",
            "token_sha256": hashlib.sha256(self.token.encode("ascii")).hexdigest(),
            "expires_at": time.time() + 3600,
        }])
        self.upstream_calls = []
        self.guard_calls = []
        self.guard_success = [True]

        def responder(request):
            self.upstream_calls.append(request)
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
                headers={"set-cookie": "not-for-clients=1"},
            )

        self.transport = httpx.MockTransport(responder)

    def store(self, grants):
        self.path.write_text(json.dumps({"schema": SCHEMA, "grants": grants}))
        self.path.chmod(0o600)

    def headers(self):
        return {"authorization": "Bearer " + self.token,
                "content-type": "application/json"}

    def post(self, client):
        return client.post(
            "/mcp", headers=self.headers(),
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )

    async def test_private_grants_and_fixed_guard_integrate(self):
        def guard():
            self.guard_calls.append(threading.get_ident())
            return self.guard_success[0]

        async with open_guarded_runtime(
                self.path, transport=self.transport, guard_probe=guard) as app:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:18790") as client:
                response = await self.post(client)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(self.upstream_calls), 1)
                self.assertEqual(str(self.upstream_calls[0].url),
                                 "http://127.0.0.1:8790/mcp")
                self.assertNotIn("authorization",
                                 self.upstream_calls[0].headers)
                self.assertNotIn("set-cookie", response.headers)
                self.assertEqual(len(self.guard_calls), 1)
                self.assertNotEqual(self.guard_calls[0], threading.get_ident())

                self.guard_success[0] = False
                response = await self.post(client)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(len(self.upstream_calls), 1)

                self.guard_success[0] = True
                self.store([])
                self.assertEqual((await self.post(client)).status_code, 401)
                self.assertEqual(len(self.guard_calls), 2)
                self.assertEqual(len(self.upstream_calls), 1)

                self.path.chmod(0o644)
                self.assertEqual((await self.post(client)).status_code, 503)
                self.assertEqual(len(self.upstream_calls), 1)

    async def test_invalid_payload_and_missing_token_do_not_probe_root(self):
        def guard():
            self.guard_calls.append("called")
            return True

        async with open_guarded_runtime(
                self.path, transport=self.transport, guard_probe=guard) as app:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:18790") as client:
                self.assertEqual((await client.post(
                    "/mcp", content=b"", headers=self.headers())).status_code, 400)
                self.assertEqual((await client.post(
                    "/mcp", json={"jsonrpc": "2.0"})).status_code, 401)
        self.assertEqual(self.guard_calls, [])
        self.assertEqual(self.upstream_calls, [])

    async def test_guard_offloads_from_asgi_event_loop(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_guard():
            entered.set()
            release.wait(timeout=3)
            return True

        async with open_guarded_runtime(
                self.path, transport=self.transport, guard_probe=slow_guard) as app:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:18790") as client:
                task = asyncio.create_task(self.post(client))
                try:
                    for _ in range(100):
                        if entered.is_set():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(entered.is_set())
                    # asyncio scheduling continues while the guard is blocked.
                    tick = asyncio.Event()
                    asyncio.get_running_loop().call_soon(tick.set)
                    await asyncio.wait_for(tick.wait(), 0.3)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                self.assertEqual((await asyncio.wait_for(task, 2)).status_code, 200)
        self.assertEqual(len(self.upstream_calls), 1)

    async def test_concurrent_requests_serialize_guard_probe(self):
        entered = threading.Event()
        release = threading.Event()
        guarded = []

        def guard():
            guarded.append("entry")
            if len(guarded) == 1:
                entered.set()
                release.wait(timeout=3)
            return True

        async with open_guarded_runtime(
                self.path, transport=self.transport, guard_probe=guard) as app:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:18790") as client:
                first = asyncio.create_task(self.post(client))
                second = asyncio.create_task(self.post(client))
                try:
                    for _ in range(100):
                        if entered.is_set():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(entered.is_set())
                    await asyncio.sleep(0.05)
                    self.assertEqual(guarded, ["entry"])
                finally:
                    release.set()
                a, b = await asyncio.wait_for(
                    asyncio.gather(first, second), 3)
                self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(len(self.upstream_calls), 2)

    async def test_guard_errors_and_timeout_fail_closed(self):
        def failed_guard():
            raise OSError("do not expose details")

        async with open_guarded_runtime(
                self.path, transport=self.transport, guard_probe=failed_guard) as app:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:18790") as client:
                response = await self.post(client)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("do not expose", response.text)
        self.assertFalse(self.upstream_calls)

        stop = threading.Event()
        def stalled_guard():
            stop.wait(timeout=2)
            return True

        try:
            with patch("remote_agent_gateway.MAX_GUARD_SECONDS", 0.025):
                async with open_guarded_runtime(
                        self.path, transport=httpx.MockTransport(
                            lambda _: httpx.Response(200)),
                        guard_probe=stalled_guard) as app:
                    async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=app),
                            base_url="http://127.0.0.1:18790") as client:
                        response = await asyncio.wait_for(self.post(client), 1)
                        self.assertEqual(response.status_code, 503)
        finally:
            stop.set()

    async def test_no_listeners_are_created_and_credentials_are_not_printed(self):
        # A factory merely builds an in-process application. No uvicorn,
        # sockets, systemd or sudo calls are required at construction.
        with patch("remote_agent_runtime.httpx.AsyncClient") as client_class:
            client_class.return_value.__aenter__.return_value = object()
            async with open_guarded_runtime(
                    self.path, guard_probe=lambda: True) as app:
                self.assertIsNotNone(app)
                client_class.assert_called_once()
                kwargs = client_class.call_args.kwargs
                self.assertFalse(kwargs["trust_env"])
                self.assertFalse(kwargs["follow_redirects"])
                self.assertEqual(kwargs["limits"].max_connections, 8)

    async def test_bad_factory_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            async with open_guarded_runtime("relative.json"):
                pass
        with self.assertRaises(ValueError):
            async with open_guarded_runtime(self.path.parent / ".." / "unsafe.json"):
                pass
        with self.assertRaises(ValueError):
            async with open_guarded_runtime(self.path, guard_probe=None):
                pass


if __name__ == "__main__":
    unittest.main()
