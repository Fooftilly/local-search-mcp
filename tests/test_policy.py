"""No external HTTP calls; policy tests use isolated SQLite per test."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import domain_policy as policy


class DomainPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name, "ALLOWED_CRAWL_DOMAINS": "example.com"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_public_suffixes_rejected_including_private(self):
        for name in ("co.uk", "github.io", "blogspot.com"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Public suffix"):
                policy.normalize_domain(name)

    def test_normal_public_subdomains_permitted(self):
        self.assertEqual(policy.normalize_domain("WWW.Example.COM."), "www.example.com")
        self.assertEqual(policy.normalize_domain("my-project.github.io"), "my-project.github.io")

    def test_private_addresses_and_special_domains_rejected(self):
        for value in ("localhost", "127.0.0.1", "foo.local", "a.internal", "http://example.com", "a.test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.normalize_domain(value)

    def test_existing_insecure_rules_migrated_out(self):
        with policy._db() as db:
            now = policy.utcnow()
            for name in ("co.uk", "github.io", "example.com"):
                db.execute("INSERT INTO domains(domain,status,created_at,updated_at) VALUES(?,?,?,?)", (name,"allowed",now,now))
            db.execute("INSERT INTO metadata(key,value) VALUES('domain_seed_v1','1')")
            db.commit()
        policy.initialize()
        domains={row['domain'] for row in policy.entries()}
        self.assertEqual(domains, {"example.com"})
        # Old broadly approved parents can no longer cover unrelated subdomains.
        result = policy.evaluate_fetch_url("https://someone-else.github.io/")
        self.assertEqual(result["status"], "approval_required")

    def test_unknown_request_is_pending_and_requires_local_decision(self):
        result = policy.evaluate_fetch_url("https://docs.python.org/help?token=secret")
        self.assertEqual(result["status"], "approval_required")
        self.assertNotIn("secret", str(policy.entries()))
        self.assertEqual(policy.request("docs.python.org", "https://docs.python.org/help")[0], "pending")
        policy.decide("python.org", "allow")
        self.assertEqual(policy.request("www.python.org", "https://www.python.org/")[0], "allowed")

    def test_deny_rule_takes_priority_over_allowed_parent(self):
        policy.decide("example.com", "allow")
        policy.decide("untrusted.example.com", "deny")
        self.assertEqual(policy.request("untrusted.example.com", "https://untrusted.example.com")[0], "denied")
        self.assertEqual(policy.request("safe.example.com", "https://safe.example.com")[0], "allowed")

    def test_nonstandard_port_is_blocked_before_approval(self):
        result=policy.evaluate_fetch_url("http://example.com:8080/")
        self.assertEqual(result["status"], "blocked")

    def test_dns_resolution_blocks_private_addresses(self):
        with patch.object(policy.socket, 'getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',443))]):
            result=policy.evaluate_fetch_url("https://example.com/")
        self.assertEqual(result["status"], "blocked")
        self.assertIn('Non-public',result['reason'])


if __name__ == '__main__':
    unittest.main()
