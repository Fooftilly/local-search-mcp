"""Cloudflare capability-burst admission stays bounded, private and revocable."""
import asyncio
import unittest
from unittest.mock import patch

import httpx

from remote_agent_gateway import (
    LoopbackMCPGateway, PORTAL_MAX_WAITING, synthetic_grant,
)


class PortalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = [100.0]
        self.grants = [synthetic_grant("a" * 48, "cloudflare-portal", 300),
                       synthetic_grant("b" * 48, "other-client", 300)]
        self.release = asyncio.Event()
        self.entered = 0
        self.active = 0
        self.max_active = 0

        async def handler(request):
            self.entered += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await self.release.wait()
                return httpx.Response(200, json={"jsonrpc": "2.0",
                                                  "id": 1, "result": {}})
            finally:
                self.active -= 1

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.gateway = LoopbackMCPGateway(
            upstream=self.upstream, grants=lambda: tuple(self.grants),
            guard_ready=lambda: True,
            clock=lambda: self.clock[0], rate_clock=lambda: self.clock[0])
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.gateway),
            base_url="http://127.0.0.1:18790")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.upstream.aclose)

    async def post(self, token="a" * 48):
        return await self.client.post(
            "/mcp", headers={"authorization": "Bearer " + token},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_parallel_discovery_queues_but_never_exceeds_active_cap(self):
        jobs = [asyncio.create_task(self.post()) for _ in range(6)]
        try:
            await self.until(lambda: self.entered == 2
                             and self.gateway._portal_waiters == 4)
            self.assertEqual(self.gateway._budget._active_total, 2)
            self.release.set()
            replies = await asyncio.wait_for(asyncio.gather(*jobs), 3)
            self.assertEqual([r.status_code for r in replies], [200] * 6)
            self.assertEqual(self.max_active, 2)
            self.assertEqual(self.gateway._portal_waiters, 0)
            self.assertEqual(self.gateway._budget._active_total, 0)
        finally:
            self.release.set()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def test_other_clients_keep_immediate_rejection(self):
        jobs = [asyncio.create_task(self.post()) for _ in range(2)]
        try:
            await self.until(lambda: self.entered == 2)
            # Global budget is available, so an unrelated client proceeds.
            other = asyncio.create_task(self.post("b" * 48))
            await self.until(lambda: self.entered == 3)
            self.assertEqual(self.gateway._portal_waiters, 0)
            self.release.set()
            self.assertEqual((await other).status_code, 200)
        finally:
            self.release.set()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def test_full_queue_and_timeout_do_not_leak_slots(self):
        with patch("remote_agent_gateway.PORTAL_MAX_WAIT_SECONDS", 0.04):
            jobs = [asyncio.create_task(self.post())
                    for _ in range(PORTAL_MAX_WAITING + 3)]
            try:
                await self.until(lambda: self.entered == 2)
                await self.until(
                    lambda: self.gateway._portal_waiters == PORTAL_MAX_WAITING)
                await self.until(lambda: self.gateway._portal_waiters == 0)
                self.assertEqual(self.entered, 2)
            finally:
                self.release.set()
                results = await asyncio.gather(*jobs, return_exceptions=True)
            self.assertEqual(sum(r.status_code == 429 for r in results), 9)
            self.assertEqual(self.gateway._budget._active_total, 0)

    async def test_queued_revocation_fails_without_upstream_call(self):
        jobs = [asyncio.create_task(self.post()) for _ in range(3)]
        try:
            await self.until(lambda: self.entered == 2
                             and self.gateway._portal_waiters == 1)
            self.grants[:] = self.grants[1:]
            await self.until(lambda: self.gateway._portal_waiters == 0)
            self.release.set()
            statuses = [r.status_code for r in
                        await asyncio.wait_for(asyncio.gather(*jobs), 3)]
            self.assertIn(401, statuses)
            self.assertEqual(self.entered, 2)
        finally:
            self.release.set()
            await asyncio.gather(*jobs, return_exceptions=True)

    async def test_client_minute_quota_does_not_enter_wait_queue(self):
        # A Cloudflare discovery burst may wait for a slot, never a rate
        # quota: the 31st request must fail immediately without forwarding.
        self.release.set()
        for _ in range(30):
            self.assertEqual((await self.post()).status_code, 200)
        self.assertEqual(self.entered, 30)
        self.assertEqual((await self.post()).status_code, 429)
        self.assertEqual(self.entered, 30)
        self.assertEqual(self.gateway._portal_waiters, 0)
        self.assertEqual(self.gateway._budget._active_total, 0)

    async def test_cancel_waiter_releases_queue_count(self):
        jobs = [asyncio.create_task(self.post()) for _ in range(3)]
        try:
            await self.until(lambda: self.gateway._portal_waiters == 1)
            jobs[2].cancel()
            await asyncio.gather(jobs[2], return_exceptions=True)
            self.assertEqual(self.gateway._portal_waiters, 0)
        finally:
            self.release.set()
            await asyncio.gather(*jobs, return_exceptions=True)
        self.assertEqual(self.gateway._budget._active_total, 0)


if __name__ == "__main__":
    unittest.main()
