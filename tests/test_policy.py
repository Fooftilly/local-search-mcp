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

    def test_requested_subdomain_obeys_later_parent_allow(self):
        first = policy.evaluate_fetch_url("https://docs.python.org/3/?token=private")
        self.assertEqual(first["status"], "approval_required")
        self.assertEqual(first["rule"], "docs.python.org")
        policy.decide("python.org", "allow")
        # After the parent decision the stale pending row must disappear,
        # and the SAME previously requested hostname must be allowed.
        self.assertNotIn("docs.python.org", {row["domain"] for row in policy.entries()})
        self.assertEqual(policy.request("docs.python.org", "https://docs.python.org/3/"),
                         ("allowed", "python.org"))
        self.assertEqual(policy.request("www.python.org", "https://www.python.org/"),
                         ("allowed", "python.org"))
        self.assertNotIn("docs.python.org", {row["domain"] for row in policy.entries()})

    def test_inherited_deny_never_persists_as_explicit_child_rule(self):
        policy.decide("example.org", "deny")
        self.assertEqual(policy.request("a.example.org", "https://a.example.org/"),
                         ("denied", "example.org"))
        self.assertNotIn("a.example.org", {row["domain"] for row in policy.entries()})
        policy.decide("example.org", "allow")
        self.assertEqual(policy.request("a.example.org", "https://a.example.org/"),
                         ("allowed", "example.org"))

    def test_explicit_child_exception_survives_parent_changes_and_restart(self):
        policy.decide("example.org", "allow")
        policy.decide("blocked.example.org", "deny")
        policy.decide("ignored.example.org", "allow")
        policy.decide("example.org", "deny")
        policy.initialize()
        self.assertEqual(policy.request("blocked.example.org", "https://blocked.example.org/"),
                         ("denied", "blocked.example.org"))
        self.assertEqual(policy.request("ignored.example.org", "https://ignored.example.org/"),
                         ("allowed", "ignored.example.org"))
        self.assertEqual(policy.request("other.example.org", "https://other.example.org/"),
                         ("denied", "example.org"))
        policy.decide("example.org", "allow")
        self.assertEqual(policy.request("blocked.example.org", "https://blocked.example.org/")[0], "denied")
        self.assertEqual(policy.request("other.example.org", "https://other.example.org/")[0], "allowed")

    def test_legacy_inherited_deny_rows_migrate_without_losing_explicit_deny(self):
        """Pre-migration child denies copied by request() are not user rules."""
        with policy._db() as db:
            now = policy.utcnow()
            for domain, status in (
                ("example.org", "denied"),
                ("child.example.org", "denied"),
                ("explicit.example.org", "denied"),
                ("allowed.example.net", "allowed"),
            ):
                db.execute(
                    "INSERT INTO domains(domain,status,created_at,updated_at) VALUES(?,?,?,?)",
                    (domain, status, now, now),
                )
            db.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,'deny')",
                       (now, "example.org"))
            db.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,'deny')",
                       (now, "explicit.example.org"))
            db.execute("INSERT INTO metadata(key,value) VALUES('domain_seed_v1','1')")
            db.commit()
        policy.initialize()
        rows = {row["domain"]: row for row in policy.entries()}
        # Parent has an explicit deny, so the auto-inherited child row is
        # obsolete and removed during migration, not retained as a rule.
        self.assertNotIn("child.example.org", rows)
        self.assertEqual(rows["explicit.example.org"]["status"], "denied")
        self.assertEqual(rows["explicit.example.org"]["explicit_rule"], 1)
        self.assertEqual(rows["allowed.example.net"]["explicit_rule"], 1)
        policy.decide("example.org", "allow")
        self.assertEqual(policy.request("child.example.org", "https://child.example.org/"),
                         ("allowed", "example.org"))
        self.assertEqual(policy.request("explicit.example.org", "https://explicit.example.org/"),
                         ("denied", "explicit.example.org"))
        policy.initialize()
        with policy._db() as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM metadata WHERE key='explicit_rules_v1'"
            ).fetchone()[0], 1)

    def test_upgrade_adds_explicit_rule_column_to_original_sqlite_schema(self):
        """Verify the actual pre-upgrade domains table migrates in place."""
        database = policy.database_path()
        database.parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(database) as conn:
            conn.executescript("""
                CREATE TABLE domains(
                    domain TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    last_requested_at TEXT NOT NULL DEFAULT '',
                    last_requested_url TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE decisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL, domain TEXT NOT NULL, action TEXT NOT NULL
                );
                INSERT INTO domains(domain,status,created_at,updated_at)
                    VALUES('example.org','denied','2026-01-01','2026-01-01');
                INSERT INTO domains(domain,status,created_at,updated_at,request_count)
                    VALUES('sub.example.org','denied','2026-01-01','2026-01-01',1);
                INSERT INTO metadata(key,value) VALUES('domain_seed_v1','1');
                INSERT INTO decisions(at,domain,action)
                    VALUES('2026-01-01','example.org','deny');
            """)
        policy.initialize()
        with policy._db() as conn:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(domains)")}
            self.assertIn("explicit_rule", columns)
            self.assertEqual(conn.execute(
                "SELECT explicit_rule FROM domains WHERE domain='example.org'"
            ).fetchone()[0], 1)
        self.assertEqual(policy.request("sub.example.org", "https://sub.example.org/"),
                         ("denied", "example.org"))
        policy.decide("example.org", "allow")
        self.assertEqual(policy.request("sub.example.org", "https://sub.example.org/"),
                         ("allowed", "example.org"))

    def test_legacy_child_denial_does_not_follow_removed_or_allowed_parent(self):
        """Old auto-denials are not user rules, even after a parent changed."""
        with policy._db() as db:
            now = policy.utcnow()
            for domain, status, requests in (
                ("example.org", "allowed", 0),        # former deny, now allowed
                ("blocked.example.org", "denied", 3), # inherited during old deny
                ("orphan.example.net", "denied", 2),  # parent deleted before upgrade
                ("manual.example.net", "denied", 0),  # no request-derived history
                ("explicit.example.org", "denied", 2),
            ):
                db.execute("""INSERT INTO domains
                    (domain,status,created_at,updated_at,request_count,last_requested_at)
                    VALUES(?,?,?,?,?,?)""", (domain, status, now, now, requests,
                                               now if requests else ""))
            db.execute("INSERT INTO metadata(key,value) VALUES('domain_seed_v1','1')")
            db.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,'allow')",
                       (now, "example.org"))
            db.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,'deny')",
                       (now, "explicit.example.org"))
            db.commit()
        policy.initialize()
        rows = {r["domain"]: r for r in policy.entries()}
        self.assertNotIn("blocked.example.org", rows)
        self.assertEqual(rows["orphan.example.net"]["status"], "pending")
        self.assertEqual(rows["orphan.example.net"]["explicit_rule"], 0)
        self.assertEqual(rows["manual.example.net"]["status"], "denied")
        self.assertEqual(rows["manual.example.net"]["explicit_rule"], 1)
        self.assertEqual(rows["explicit.example.org"]["explicit_rule"], 1)
        self.assertEqual(policy.request("blocked.example.org", "https://blocked.example.org/"),
                         ("allowed", "example.org"))
        self.assertEqual(policy.request("orphan.example.net", "https://orphan.example.net/"),
                         ("pending", "orphan.example.net"))
        self.assertEqual(policy.request("explicit.example.org", "https://explicit.example.org/"),
                         ("denied", "explicit.example.org"))

    def test_legacy_pending_child_under_allowed_parent_is_removed_on_upgrade(self):
        """A stale pending row does not keep asking for approval on a grant."""
        with policy._db() as db:
            now = policy.utcnow()
            db.execute(
                "INSERT INTO domains(domain,status,created_at,updated_at) VALUES(?,?,?,?)",
                ("example.org", "allowed", now, now),
            )
            db.execute(
                "INSERT INTO domains(domain,status,created_at,updated_at) VALUES(?,?,?,?)",
                ("docs.example.org", "pending", now, now),
            )
            db.execute("INSERT INTO metadata(key,value) VALUES('domain_seed_v1','1')")
            db.commit()
        policy.initialize()
        rows = {entry["domain"]: entry for entry in policy.entries()}
        self.assertIn("example.org", rows)
        self.assertNotIn("docs.example.org", rows)
        self.assertEqual(policy.request("docs.example.org", "https://docs.example.org/"),
                         ("allowed", "example.org"))

    def test_explicit_rule_after_pending_request_overrides_parent(self):
        result = policy.evaluate_fetch_url("https://docs.python.org/")
        self.assertEqual(result["status"], "approval_required")
        policy.decide("docs.python.org", "deny")
        policy.decide("python.org", "allow")
        self.assertEqual(policy.request("docs.python.org", "https://docs.python.org/"),
                         ("denied", "docs.python.org"))
        policy.decide("docs.python.org", "delete")
        self.assertEqual(policy.request("docs.python.org", "https://docs.python.org/"),
                         ("allowed", "python.org"))

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
