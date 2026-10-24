"""Clock-controlled, offline regression tests for inert backend activity."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest

from scripts.backend_idle_activity import BackendBusy, IdleActivity


class IdleActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.clock = lambda: self.now[0]
        self.gate = IdleActivity(clock=self.clock)

    def advance(self, seconds):
        self.now[0] += seconds

    def test_disabled_or_unverified_backends_never_claim_idle_stop(self):
        self.advance(7200)
        for name in ("searxng", "crawl4ai"):
            self.assertFalse(self.gate.claim_idle_stop(name))
            self.assertIsNone(self.gate.snapshot(name)["idle_seconds"])
        self.gate.arm_running("searxng")
        self.gate.disarm("searxng")
        self.advance(7200)
        self.assertFalse(self.gate.claim_idle_stop("searxng"))

    def test_full_quiet_window_and_polling_do_not_reset_clock(self):
        self.gate.arm_running("crawl4ai")
        self.advance(1799)
        self.assertEqual(self.gate.snapshot("crawl4ai")["active_requests"], 0)
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))
        self.advance(1)
        self.assertTrue(self.gate.claim_idle_stop("crawl4ai"))
        self.assertTrue(self.gate.snapshot("crawl4ai")["stop_claimed"])
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))
        self.gate.finish_stop("crawl4ai", stopped=True)
        self.assertFalse(self.gate.snapshot("crawl4ai")["armed"])
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))

    def test_repeated_running_verification_does_not_reset_idle_window(self):
        self.gate.arm_running("crawl4ai")
        self.advance(1700)
        self.gate.arm_running("crawl4ai")
        self.assertEqual(self.gate.snapshot("crawl4ai")["idle_seconds"], 1700)
        self.advance(100)
        self.assertTrue(self.gate.claim_idle_stop("crawl4ai"))

    def test_long_request_and_overlap_prohibit_stop(self):
        self.gate.arm_running("crawl4ai")
        self.gate.begin_work("crawl4ai")
        self.advance(4000)
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))
        self.gate.begin_work("crawl4ai")
        self.gate.finish_work("crawl4ai")
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))
        self.advance(4000)
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))
        self.gate.finish_work("crawl4ai")
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))
        self.advance(1800)
        self.assertTrue(self.gate.claim_idle_stop("crawl4ai"))

    def test_failure_leases_finish_and_backend_clocks_are_independent(self):
        self.gate.arm_running("crawl4ai")
        self.gate.arm_running("searxng")
        self.advance(1500)
        with self.assertRaisesRegex(RuntimeError, "crawler"):
            with self.gate.work("crawl4ai"):
                self.advance(4000)
                raise RuntimeError("crawler failed")
        self.assertTrue(self.gate.claim_idle_stop("searxng"))
        self.assertFalse(self.gate.claim_idle_stop("crawl4ai"))
        self.assertEqual(self.gate.snapshot("crawl4ai")["active_requests"], 0)

    def test_stop_claim_blocks_new_work_and_failed_stop_resets_quiet_window(self):
        self.gate.arm_running("searxng")
        self.advance(1800)
        self.assertTrue(self.gate.claim_idle_stop("searxng"))
        with self.assertRaises(BackendBusy):
            self.gate.begin_work("searxng")
        with self.assertRaises(BackendBusy):
            self.gate.arm_running("searxng")
        with self.assertRaises(BackendBusy):
            self.gate.disarm("searxng")
        self.gate.finish_stop("searxng", stopped=False)
        self.assertFalse(self.gate.claim_idle_stop("searxng"))
        self.advance(1800)
        self.assertTrue(self.gate.claim_idle_stop("searxng"))

    def test_many_concurrent_stop_claims_have_one_winner(self):
        self.gate.arm_running("crawl4ai")
        self.advance(1800)
        with ThreadPoolExecutor(max_workers=16) as executor:
            claims = list(executor.map(
                self.gate.claim_idle_stop, ["crawl4ai"] * 50))
        self.assertEqual(sum(claims), 1)

    def test_invalid_transition_and_configuration_fail_closed(self):
        for number in (0, 86401, True, None, "1800", 1.2):
            with self.subTest(number=number), self.assertRaises(ValueError):
                IdleActivity(number, clock=self.clock)
        for name in ("foreign-container", "docker", ""):
            with self.assertRaises(ValueError):
                self.gate.arm_running(name)
        with self.assertRaises(ValueError):
            self.gate.finish_work("crawl4ai")
        with self.assertRaises(ValueError):
            self.gate.finish_stop("searxng", stopped=True)
        with self.assertRaises(ValueError):
            self.gate.finish_stop("searxng", stopped=1)
        self.gate.arm_running("searxng")
        self.gate.begin_work("searxng")
        with self.assertRaises(BackendBusy):
            self.gate.disarm("searxng")
        self.gate.finish_work("searxng")
        self.gate.disarm("searxng")
        self.advance(2000)
        self.assertFalse(self.gate.claim_idle_stop("searxng"))


if __name__ == "__main__":
    unittest.main()
