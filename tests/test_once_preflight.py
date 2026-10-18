"""One-use grant preflight must fail closed before consuming a private proof."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import os
import socket
import tempfile
import unittest

import domain_policy as policy


def public_answers(ip: str = "93.184.216.34", port: int = 443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


class OncePreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "XDG_STATE_HOME": temp.name, "ALLOWED_CRAWL_DOMAINS": "example.com",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_public_url_consumes_only_once_and_returns_private_proof(self):
        expected = policy._stage_once_grant("docs.example.org")
        with patch.object(policy.socket, "getaddrinfo",
                          return_value=public_answers()) as resolver:
            claim = policy._claim_once_for_public_url(
                "https://docs.example.org/private?token=PRIVATE")
            self.assertEqual(claim, ("docs.example.org", expected))
            self.assertIsNone(policy._claim_once_for_public_url(
                "https://docs.example.org/again"))
        resolver.assert_any_call("docs.example.org", 443, type=socket.SOCK_STREAM)
        self.assertTrue(policy._once_proof_current(*claim))
        self.assertNotIn(expected, repr(policy.entries()))
        self.assertNotIn("PRIVATE", repr(policy.entries()))
        self.assertEqual(
            policy.evaluate_fetch_url("https://docs.example.org/article")["status"],
            "approval_required")  # Even after consuming, MCP is still inert.

    def test_reject_invalid_public_urls_without_consuming(self):
        grant = policy._stage_once_grant("docs.example.org")
        for url in (
            "http://127.0.0.1/", "https://com/", "https://github.io/",
            "https://docs.example.org:8443/path",
            "https://user:pass@docs.example.org/path",
            "file:///etc/passwd", "http://docs.example.org:abc/",
            "https://docs.example.org:0/", "https://docs.example.org:65535/",
            "https://docs.example.org@localhost/", "http://localhost/",
        ):
            with self.subTest(url=url), patch.object(
                    policy.socket, "getaddrinfo",
                    side_effect=AssertionError("DNS must not be called")):
                self.assertIsNone(policy._claim_once_for_public_url(url))
        with patch.object(policy.socket, "getaddrinfo",
                          return_value=public_answers()):
            self.assertEqual(policy._claim_once_for_public_url(
                "https://docs.example.org/"), ("docs.example.org", grant))

    def test_dns_failure_or_mixed_public_private_never_consumes(self):
        grant = policy._stage_once_grant("docs.example.org")
        patterns = [
            [], public_answers("127.0.0.1"), public_answers("10.2.3.4"),
            public_answers("169.254.169.254"), public_answers("::1"),
            public_answers() + public_answers("192.168.0.1"),
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))],
        ]
        for result in patterns:
            with self.subTest(records=result), patch.object(
                    policy.socket, "getaddrinfo", return_value=result):
                self.assertIsNone(policy._claim_once_for_public_url(
                    "https://docs.example.org/"))
        with patch.object(policy.socket, "getaddrinfo",
                          side_effect=socket.gaierror("private DNS error")):
            self.assertIsNone(policy._claim_once_for_public_url(
                "https://docs.example.org/"))
        with patch.object(policy.socket, "getaddrinfo",
                          return_value=public_answers()):
            self.assertEqual(policy._claim_once_for_public_url(
                "https://docs.example.org/"), ("docs.example.org", grant))

    def test_active_nested_denial_remains_authoritative_after_dns(self):
        policy._stage_once_grant("example.org", scope="subdomains")
        policy.decide("blocked.example.org", "deny", scope="subdomains")
        with patch.object(policy.socket, "getaddrinfo",
                          return_value=public_answers()):
            self.assertIsNone(policy._claim_once_for_public_url(
                "https://blocked.example.org/"))
            self.assertIsNone(policy._claim_once_for_public_url(
                "https://deep.blocked.example.org/"))
            self.assertIsNotNone(policy._claim_once_for_public_url(
                "https://safe.example.org/"))

    def test_multiple_public_dns_claimants_have_one_winner(self):
        expected = policy._stage_once_grant("docs.example.org")
        gate = Barrier(8)
        def check(_):
            gate.wait(timeout=10)
            return policy._claim_once_for_public_url("https://docs.example.org/")
        with patch.object(policy.socket, "getaddrinfo",
                          return_value=public_answers()):
            with ThreadPoolExecutor(max_workers=8) as pool:
                claims = list(pool.map(check, range(8)))
        self.assertEqual([x for x in claims if x is not None],
                         [("docs.example.org", expected)])

    def test_expiration_during_dns_is_fail_closed(self):
        with patch.object(policy, "utcnow", return_value="2026-10-18T12:00:00Z"):
            policy._stage_once_grant("docs.example.org", expires_minutes=60)
        with patch.object(policy.socket, "getaddrinfo",
                          return_value=public_answers()), patch.object(
                              policy, "utcnow", return_value="2026-10-18T13:00:00Z"):
            self.assertIsNone(policy._claim_once_for_public_url(
                "https://docs.example.org/"))


if __name__ == "__main__":
    unittest.main()
