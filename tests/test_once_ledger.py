"""Security invariants for the INERT one-use grant transaction ledger.

This file intentionally does not activate once grants in MCP or dashboard.
The guarded fetch integration must own a private proof through DNS checks,
redirect validation, cached results, batches, and continuation pages.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import domain_policy as policy
import request_cache


class OnceLedgerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": tmp.name,
            "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)

    def test_staging_never_grants_a_live_policy_or_mcp_fetch(self):
        grant_id = policy._stage_once_grant("docs.example.org")
        self.assertRegex(grant_id, r"^[a-f0-9]{32}$")
        self.assertFalse(policy.is_domain_currently_allowed("docs.example.org"))
        self.assertEqual(
            policy.request("docs.example.org", "https://docs.example.org/")[0],
            "pending")
        with patch.object(policy.socket, "getaddrinfo",
                          side_effect=AssertionError("DNS must not be reached")):
            result = policy.evaluate_fetch_url("https://docs.example.org/secret")
        self.assertEqual(result["status"], "approval_required")
        self.assertNotIn(grant_id, repr(result))
        self.assertNotIn(grant_id, repr(policy.entries()))
        self.assertNotIn(grant_id, repr(policy.recent_decisions()))
        self.assertFalse(policy._once_proof_current("docs.example.org", grant_id))

    def test_atomic_consumption_survives_restart_and_rejects_replays(self):
        with patch.object(policy, "utcnow", return_value="2026-10-18T12:00:00Z"):
            grant = policy._stage_once_grant("docs.example.org", expires_minutes=60)
            claimed = policy._consume_once_grant("docs.example.org")
            self.assertEqual(claimed, grant)
            self.assertTrue(policy._once_proof_current("docs.example.org", claimed))
        policy.initialize()  # Open SQLite again, as the service does at startup.
        with patch.object(policy, "utcnow", return_value="2026-10-18T12:30:00Z"):
            self.assertIsNone(policy._consume_once_grant("docs.example.org"))
            self.assertTrue(policy._once_proof_current("docs.example.org", claimed))
        with patch.object(policy, "utcnow", return_value="2026-10-18T13:00:00Z"):
            self.assertIsNone(policy._consume_once_grant("docs.example.org"))
            self.assertFalse(policy._once_proof_current("docs.example.org", claimed))

    def test_concurrent_claims_admit_exactly_one(self):
        policy._stage_once_grant("docs.example.org", expires_minutes=1440)
        gate = Barrier(12)
        def claim(_):
            gate.wait(timeout=15)
            return policy._consume_once_grant("docs.example.org")
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(claim, range(12)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertIsNone(policy._consume_once_grant("docs.example.org"))
        with policy._db() as conn:
            row = conn.execute("SELECT consumed_at FROM once_grants").fetchone()
            self.assertTrue(row["consumed_at"])

    def test_exact_subdomain_boundaries_and_nested_explicit_denials(self):
        parent = policy._stage_once_grant("example.org", scope="exact")
        self.assertIsNone(policy._consume_once_grant("child.example.org"))
        self.assertEqual(policy._consume_once_grant("example.org"), parent)
        self.assertFalse(policy._once_proof_current("child.example.org", parent))
        broader = policy._stage_once_grant("example.org", scope="subdomains")
        self.assertNotEqual(parent, broader)
        policy.decide("blocked.example.org", "deny", scope="subdomains")
        self.assertIsNone(policy._consume_once_grant("blocked.example.org"))
        self.assertIsNone(policy._consume_once_grant("deep.blocked.example.org"))
        self.assertEqual(policy._consume_once_grant("safe.example.org"), broader)
        self.assertTrue(policy._once_proof_current("deep.safe.example.org", broader))
        self.assertFalse(policy._once_proof_current("blocked.example.org", broader))
        self.assertFalse(policy._once_proof_current("example.org", parent))

    def test_replacement_and_revocation_invalidate_old_inflight_proofs(self):
        first = policy._stage_once_grant("example.org")
        self.assertEqual(policy._consume_once_grant("example.org"), first)
        epoch = request_cache.policy_epoch()
        replacement = policy._stage_once_grant("example.org")
        self.assertNotEqual(first, replacement)
        self.assertGreater(request_cache.policy_epoch(), epoch)
        self.assertFalse(policy._once_proof_current("example.org", first))
        self.assertEqual(policy._consume_once_grant("example.org"), replacement)
        self.assertTrue(policy._once_proof_current("example.org", replacement))
        policy._revoke_once_grant("example.org")
        self.assertFalse(policy._once_proof_current("example.org", replacement))
        self.assertIsNone(policy._consume_once_grant("example.org"))
        policy._revoke_once_grant("example.org")  # Idempotent.

    def test_explicit_denial_precedence_and_expired_grant(self):
        policy.decide("example.org", "deny")
        with self.assertRaisesRegex(ValueError, "explicit denial"):
            policy._stage_once_grant("example.org")
        policy.decide("example.org", "delete")
        with patch.object(policy, "utcnow", return_value="2026-10-18T12:00:00Z"):
            grant = policy._stage_once_grant("example.org", expires_minutes=60)
        with patch.object(policy, "utcnow", return_value="2026-10-18T13:00:00Z"):
            self.assertIsNone(policy._consume_once_grant("example.org"))
            self.assertFalse(policy._once_proof_current("example.org", grant))
        grant = policy._stage_once_grant("example.org")
        policy.decide("example.org", "deny")
        self.assertIsNone(policy._consume_once_grant("example.org"))
        self.assertFalse(policy._once_proof_current("example.org", grant))

    def test_bad_domains_scope_durations_and_proofs_are_rejected(self):
        for domain in ("com", "github.io", "localhost", "127.0.0.1",
                       "http://example.org", "example.org:8080"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                policy._stage_once_grant(domain)
        for scope in ("*", "parent", ""):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                policy._stage_once_grant("example.org", scope=scope)
        for value in (-1, 1, 10080 + 1, "60", True, None):
            with self.subTest(duration=value), self.assertRaises(ValueError):
                policy._stage_once_grant("example.org", expires_minutes=value)
        grant = policy._stage_once_grant("example.org")
        for value in ("", "wrong", grant.upper(), grant[:24], None):
            with self.subTest(proof=value):
                self.assertFalse(policy._once_proof_current("example.org", value))
        self.assertTrue(Path(policy.database_path()).exists())


if __name__ == "__main__":
    unittest.main()
