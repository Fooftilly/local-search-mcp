"""No external HTTP calls; policy tests use isolated SQLite per test."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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

    def test_approval_link_uses_only_validated_loopback_port(self):
        with patch.dict(os.environ, {"LOCAL_SEARCH_MCP_DASHBOARD_PORT": "18791"}):
            self.assertEqual(policy.approval_dashboard_url(), "http://127.0.0.1:18791/")
            result = policy.evaluate_fetch_url("https://unapproved.example.net/page")
            self.assertEqual(result["approval_dashboard"], "http://127.0.0.1:18791/")
        for value in ("http://evil.example", "99999", "0", "-1", "8791/path",
                      "8791\\nX-Injected: yes", ""):
            with self.subTest(value=value), patch.dict(os.environ,
                   {"LOCAL_SEARCH_MCP_DASHBOARD_PORT": value}):
                self.assertEqual(policy.approval_dashboard_url(), "")
        with patch.dict(os.environ, clear=True):
            self.assertEqual(policy.approval_dashboard_url(), "http://127.0.0.1:8791/")

    def test_public_suffixes_rejected_including_private(self):
        for name in ("co.uk", "github.io", "blogspot.com"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Public suffix"):
                policy.normalize_domain(name)

    def test_normal_public_subdomains_permitted(self):
        self.assertEqual(policy.normalize_domain("WWW.Example.COM."), "www.example.com")
        self.assertEqual(policy.normalize_domain("my-project.github.io"), "my-project.github.io")

    def test_idna2003_browser_host_divergence_is_rejected(self):
        # Operators who approve fass.de must never authorize faß.de.
        # Other deviation characters can also cause different hosts.
        for name in ("faß.de", "straße.de", "ςa.gr",
                     "go\u115fogle.com", "x\u1d2cy.com", "\U0002f874.com"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "Ambiguous IDN"):
                    policy.normalize_domain(name)
        # Unambiguous internationalized names still work as before.
        self.assertEqual(policy.normalize_domain("café.fr"), "xn--caf-dma.fr")
        self.assertEqual(policy.normalize_domain("xn--fa-hia.de"), "xn--fa-hia.de")
        for name in ("ab--cd.example.com", "r1---sn-ab5l6nzr.googlevideo.com",
                     "xn--abc.com"):
            self.assertEqual(policy.normalize_domain(name), name)

    def test_ambiguous_idn_never_reaches_dns_or_pending_approvals(self):
        policy.decide("fass.de", "allow")
        for url in ("https://faß.de/secret", "https://straße.de/private",
                    "https://ςa.gr/"):
            with self.subTest(url=url), patch.object(
                    policy.socket, "getaddrinfo",
                    side_effect=AssertionError("DNS must not be queried")):
                result = policy.evaluate_fetch_url(url)
                self.assertIsNone(policy._public_url_host(url))
            self.assertEqual(result["status"], "blocked")
            self.assertIn("Ambiguous IDN", result["reason"])
        self.assertEqual(policy.counts()["pending"], 0)

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

    def test_dashboard_rule_change_invalidates_fetch_cache_immediately(self):
        import request_cache
        request_cache.reset_for_test()
        self.addCleanup(request_cache.reset_for_test)
        key = ("fetch", "raw", "https://example.com/page")
        request_cache.run("fetch", key, lambda: {"success": True, "markdown": "cached"},
                          ttl=120)
        self.assertIn(key, request_cache._CACHE)
        initial = request_cache.policy_epoch()
        policy.decide("example.com", "deny")
        self.assertNotIn(key, request_cache._CACHE)
        self.assertEqual(request_cache.policy_epoch(), initial + 1)
        self.assertEqual(policy.request("example.com", "https://example.com/page")[0],
                         "denied")
        policy.decide("example.com", "allow")
        self.assertEqual(request_cache.policy_epoch(), initial + 2)

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
                # Old request() inserted a clone with count=1, then incremented
                # it to 2; standalone dashboard decisions had no requests.
                inherited = domain == "child.example.org"
                db.execute("""INSERT INTO domains
                    (domain,status,created_at,updated_at,request_count,last_requested_at)
                    VALUES(?,?,?,?,?,?)""",
                    (domain, status, now, now, 2 if inherited else 0,
                     now if inherited else ""),
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

    def test_concurrent_v1_schema_upgrades_are_serialized(self):
        """The dashboard and bridge may hit the first-time ALTER concurrently."""
        database = policy.database_path()
        database.parent.mkdir(parents=True, mode=0o700)
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TABLE domains (
                    domain TEXT PRIMARY KEY, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    last_requested_at TEXT NOT NULL DEFAULT '',
                    last_requested_url TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO domains(domain,status,created_at,updated_at)
                    VALUES('example.org','denied','2026-01-01','2026-01-01');
            """)
        workers = 6
        start = Barrier(workers)

        def initialize_together(_):
            start.wait(timeout=10)
            policy.initialize()
            return policy.request("child.example.org", "https://child.example.org/")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(initialize_together, range(workers)))
        self.assertEqual(results, [("denied", "example.org")] * workers)
        with policy._db() as db:
            self.assertEqual(
                [col["name"] for col in db.execute("PRAGMA table_info(domains)")
                 if col["name"] == "explicit_rule"], ["explicit_rule"]
            )
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM metadata WHERE key='explicit_rules_v1'"
            ).fetchone()[0], 1)

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

    def test_v1_deleted_or_renamed_denial_audit_does_not_revive_inherited_child(self):
        """A stale deny audit must not turn a re-created v1 child clone into a rule."""
        database = policy.database_path()
        database.parent.mkdir(parents=True, mode=0o700)
        # Create the real original v1 schema, without explicit_rule.
        with sqlite3.connect(database) as db:
            db.executescript("""
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
                    at TEXT NOT NULL, domain TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('allow','deny','delete','rename'))
                );
                INSERT INTO metadata(key,value) VALUES('domain_seed_v1','1');
            """)
            # Original v1 semantics:
            # deny child -> delete child -> request under denied parent
            # creates a *new*, automatically inherited denied child row.
            for domain, created, updated, count, requested in (
                ("example.org", "01:00", "01:00", 0, ""),
                ("deleted.example.org", "01:17", "01:17", 2, "01:17"),
                # Same-second explicit re-deny of an inherited clone must stay.
                ("redenied.example.org", "01:14", "01:14", 2, "01:14"),
                # The renamed destination is a genuine explicit child rule.
                ("new.example.org", "01:15", "01:16", 2, "01:15"),
                # Rename away -> request at old name also creates a clone.
                ("renamed.example.org", "01:18", "01:18", 2, "01:18"),
                # With no parent, a deleted rule's new clone is pending.
                ("orphan.example.net", "01:19", "01:19", 2, "01:19"),
            ):
                stamp = lambda t: "2026-01-01T" + t + ":00Z" if t else ""
                db.execute("""INSERT INTO domains
                    (domain,status,created_at,updated_at,request_count,last_requested_at)
                    VALUES(?,?,?,?,?,?)""",
                    (domain, "denied",
                     stamp(created), stamp(updated), count, stamp(requested)))
            for action, domain, at in (
                ("deny", "example.org", "01:00"),
                ("deny", "deleted.example.org", "01:10"),
                ("delete", "deleted.example.org", "01:11"),
                ("deny", "redenied.example.org", "01:12"),
                ("delete", "redenied.example.org", "01:13"),
                ("deny", "redenied.example.org", "01:14"),
                ("deny", "renamed.example.org", "01:15"),
                ("rename", "renamed.example.org", "01:16"),
                ("deny", "orphan.example.net", "01:10"),
                ("delete", "orphan.example.net", "01:11"),
            ):
                db.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,?)",
                           ("2026-01-01T" + at + ":00Z", domain, action))
        policy.initialize()
        rows = {r["domain"]: r for r in policy.entries()}
        self.assertNotIn("deleted.example.org", rows)
        self.assertNotIn("renamed.example.org", rows)
        self.assertEqual(rows["orphan.example.net"]["status"], "pending")
        self.assertEqual(rows["orphan.example.net"]["explicit_rule"], 0)
        for domain in ("redenied.example.org", "new.example.org"):
            self.assertEqual(rows[domain]["explicit_rule"], 1)

        policy.decide("example.org", "allow")
        for domain in ("deleted.example.org", "renamed.example.org"):
            self.assertEqual(policy.request(domain, "https://" + domain + "/"),
                             ("allowed", "example.org"))
        for domain in ("redenied.example.org", "new.example.org"):
            self.assertEqual(policy.request(domain, "https://" + domain + "/"),
                             ("denied", domain))
        self.assertEqual(policy.request("orphan.example.net",
                                        "https://orphan.example.net/")[0], "pending")

    def test_legacy_renamed_denial_is_not_dropped_under_allowed_parent(self):
        """Old rename audit names only the source; preserve destination deny."""
        with policy._db() as db:
            now = policy.utcnow()
            before = "2026-01-01T00:00:00Z"
            db.execute("INSERT INTO domains(domain,status,created_at,updated_at) VALUES(?,?,?,?)",
                       ("example.org", "allowed", before, now))
            db.execute("""INSERT INTO domains(domain,status,created_at,updated_at,
                        request_count,last_requested_at)
                        VALUES(?,?,?,?,?,?)""",
                       # Creation and rename happened in the same UTC second.
                       # With equal timestamps, only the rename audit protects
                       # this explicit denial from migration cleanup.
                       ("renamed.example.org", "denied", now, now, 1, before))
            db.execute("""INSERT INTO domains(domain,status,created_at,updated_at,
                        request_count,last_requested_at)
                        VALUES(?,?,?,?,?,?)""",
                       ("inherited.example.org", "denied", before, before, 3, before))
            db.execute("INSERT INTO metadata(key,value) VALUES('domain_seed_v1','1')")
            db.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,'deny')",
                       (before, "old.example.org"))
            db.execute("INSERT INTO decisions(at,domain,action) VALUES(?,?,'rename')",
                       (now, "old.example.org"))
            db.commit()
        policy.initialize()
        rows = {r["domain"]: r for r in policy.entries()}
        self.assertEqual(rows["renamed.example.org"]["explicit_rule"], 1)
        self.assertNotIn("inherited.example.org", rows)
        self.assertEqual(policy.request("renamed.example.org", "https://renamed.example.org/"),
                         ("denied", "renamed.example.org"))
        self.assertEqual(policy.request("inherited.example.org", "https://inherited.example.org/"),
                         ("allowed", "example.org"))

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

    def test_pending_caps_preserve_existing_requests_and_operator_rules(self):
        with (patch.object(policy, "MAX_PENDING_TOTAL", 4),
              patch.object(policy, "MAX_PENDING_PER_SITE", 2),
              patch.object(policy, "MAX_NEW_PENDING_PER_SITE_PER_HOUR", 2)):
            for host in ("a.queue-example.org", "b.queue-example.org"):
                self.assertEqual(policy.request(host, "https://" + host), ("pending", host))
            self.assertEqual(policy.request("c.queue-example.org", "https://c.queue-example.org"),
                             ("queue_full", "c.queue-example.org"))
            # Repeat requests consume no new queue slots and update statistics.
            self.assertEqual(policy.request("a.queue-example.org", "https://a.queue-example.org"),
                             ("pending", "a.queue-example.org"))
            self.assertEqual(next(r for r in policy.entries() if
                                  r["domain"] == "a.queue-example.org")["request_count"], 2)
            for host in ("a.other-example.net", "b.other-example.net"):
                self.assertEqual(policy.request(host, "https://" + host), ("pending", host))
            self.assertEqual(policy.request("third-example.io", "https://third-example.io"),
                             ("queue_full", "third-example.io"))
            self.assertEqual(policy.counts()["pending"], 4)
            self.assertEqual(policy.request("example.com", "https://example.com")[0], "allowed")
            # A local user can approve a hostname despite queue saturation.
            policy.decide("c.queue-example.org", "allow")
            self.assertEqual(policy.request("c.queue-example.org", "https://c.queue-example.org"),
                             ("allowed", "c.queue-example.org"))
            policy.decide("queue-example.org", "allow")
            self.assertEqual(policy.request("a.queue-example.org", "https://a.queue-example.org"),
                             ("allowed", "queue-example.org"))
            self.assertEqual(policy.counts()["pending"], 2)

    def test_pending_creation_hourly_window_resets_without_eviction(self):
        with (patch.object(policy, "MAX_PENDING_TOTAL", 50),
              patch.object(policy, "MAX_PENDING_PER_SITE", 5),
              patch.object(policy, "MAX_NEW_PENDING_PER_SITE_PER_HOUR", 2)):
            for host in ("a.example.org", "b.example.org"):
                self.assertEqual(policy.request(host, "https://" + host)[0], "pending")
            self.assertEqual(policy.request("c.example.org", "https://c.example.org")[0],
                             "queue_full")
            with policy._db() as conn:
                conn.execute("UPDATE pending_admissions SET admitted_at='2020-01-01T00:00:00Z'"
                             " WHERE id=(SELECT MIN(id) FROM pending_admissions)")
                conn.commit()
            self.assertEqual(policy.request("c.example.org", "https://c.example.org")[0],
                             "pending")
            self.assertEqual(policy.counts()["pending"], 3)

    def test_hourly_quota_survives_resolve_delete_and_restart(self):
        with (patch.object(policy, "MAX_PENDING_TOTAL", 50),
              patch.object(policy, "MAX_PENDING_PER_SITE", 50),
              patch.object(policy, "MAX_NEW_PENDING_PER_SITE_PER_HOUR", 2)):
            first = "a.quota-example.net"
            second = "b.quota-example.net"
            for domain in (first, second):
                self.assertEqual(policy.request(domain, "https://" + domain)[0], "pending")
            policy.decide(first, "allow")
            policy.decide(second, "delete")
            self.assertEqual(policy.counts()["pending"], 0)
            policy.initialize()  # The admissions survive reopening SQLite.
            blocked = "c.quota-example.net"
            self.assertEqual(policy.request(blocked, "https://" + blocked),
                             ("queue_full", blocked))
            with policy._db() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM pending_admissions WHERE site='quota-example.net'"
                ).fetchone()[0], 2)
                conn.execute("""UPDATE pending_admissions SET admitted_at='2020-01-01T00:00:00Z'
                    WHERE site='quota-example.net'""")
                conn.commit()
            self.assertEqual(policy.request(blocked, "https://" + blocked),
                             ("pending", blocked))
            with policy._db() as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM pending_admissions WHERE site='quota-example.net'"
                ).fetchone()[0], 1)

    def test_global_hourly_admissions_bound_cross_site_bursts(self):
        with (patch.object(policy, "MAX_PENDING_TOTAL", 512),
              patch.object(policy, "MAX_NEW_PENDING_GLOBAL_PER_HOUR", 3),
              patch.object(policy, "MAX_PENDING_PER_SITE", 32),
              patch.object(policy, "MAX_NEW_PENDING_PER_SITE_PER_HOUR", 8)):
            for i in range(3):
                host = f"h.multi-site-{i}.org"
                self.assertEqual(policy.request(host, "https://" + host)[0], "pending")
            fourth = "new.unique-site.org"
            self.assertEqual(policy.request(fourth, "https://" + fourth)[0], "queue_full")
            self.assertEqual(policy.clear_pending_requests(), 3)
            self.assertEqual(policy.counts()["pending"], 0)
            # Bulk-clearing requests does not reset a rolling admission limit.
            self.assertEqual(policy.request(fourth, "https://" + fourth)[0], "queue_full")
            with policy._db() as conn:
                conn.execute("UPDATE pending_admissions SET admitted_at='2020-01-01T00:00:00Z'")
                conn.commit()
            self.assertEqual(policy.request(fourth, "https://" + fourth)[0], "pending")

    def test_clear_pending_preserves_explicit_rules_and_audits(self):
        policy.decide("blocked.example.net", "deny")
        self.assertEqual(policy.request("one.queue-example.net", "https://one.queue-example.net")[0],
                         "pending")
        self.assertEqual(policy.request("two.queue-example.net", "https://two.queue-example.net")[0],
                         "pending")
        self.assertEqual(policy.clear_pending_requests(), 2)
        self.assertEqual(policy.clear_pending_requests(), 0)
        self.assertEqual(policy.counts()["pending"], 0)
        self.assertEqual(policy.request("blocked.example.net", "https://blocked.example.net")[0],
                         "denied")
        self.assertEqual(policy.request("example.com", "https://example.com")[0], "allowed")
        with policy._db() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM decisions WHERE action='delete' "
                "AND domain IN ('one.queue-example.net','two.queue-example.net')"
            ).fetchone()[0], 2)

    def test_queue_limited_fetch_is_not_falsely_reported_pending(self):
        with patch.object(policy, "MAX_PENDING_TOTAL", 0):
            result = policy.evaluate_fetch_url("https://docs.python.org/3/")
            self.assertEqual(result["status"], "approval_queue_full")
            self.assertFalse(result["success"])
            self.assertIn("not queued", result["reason"])
            self.assertEqual(policy.counts()["pending"], 0)
            policy.decide("python.org", "allow")
            self.assertEqual(policy.request("docs.python.org", "https://docs.python.org/"),
                             ("allowed", "python.org"))

    def test_pending_site_limits_respect_private_psl_boundaries(self):
        from public_suffix import registrable_domain
        self.assertEqual(registrable_domain("docs.example.co.uk"), "example.co.uk")
        self.assertEqual(registrable_domain("docs.team.github.io"), "team.github.io")
        with (patch.object(policy, "MAX_PENDING_TOTAL", 10),
              patch.object(policy, "MAX_PENDING_PER_SITE", 1),
              patch.object(policy, "MAX_NEW_PENDING_PER_SITE_PER_HOUR", 5)):
            self.assertEqual(policy.request("a.team.github.io", "https://a.team.github.io")[0],
                             "pending")
            self.assertEqual(policy.request("b.team.github.io", "https://b.team.github.io")[0],
                             "queue_full")
            self.assertEqual(policy.request("a.other.github.io", "https://a.other.github.io")[0],
                             "pending")

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
