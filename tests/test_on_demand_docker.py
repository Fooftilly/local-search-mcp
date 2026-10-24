"""Docker mutation adapters stay scoped to full verified IDs."""
from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import on_demand_docker as docker

CRAWLER = "a" * 64
SEARCH = "b" * 64


class ScopedDockerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.crawler_file = str(self.base / "local-search-mcp" / "crawler-compose" / "compose.yaml")
        self.overlay_file = str(self.base / "local-search-mcp" / "crawler-compose" / "searxng.compose.yaml")
        self.running = {"crawl4ai": True, "searxng": False}
        self.policy = {"crawl4ai": "unless-stopped", "searxng": "no"}
        self.ids = [CRAWLER, SEARCH]
        self.external = 0
        self.operations = []

    def label(self, service):
        paths = [self.crawler_file]
        if service == "searxng":
            paths.append(self.overlay_file)
        return {"com.docker.compose.project": docker.inv.PROJECT,
                "com.docker.compose.service": service,
                "com.docker.compose.project.config_files": ",".join(paths)}

    def inventory(self, *, config_base):
        return {"state": "running" if all(self.running.values()) else "partially_running",
                "other_published_crawlers": self.external,
                "unverified_project_containers": 0,
                "verified_services": {"crawl4ai": 1, "searxng": 1},
                "managed_running": sum(self.running.values()),
                "managed_stopped": 2 - sum(self.running.values())}

    def inspect(self, ident):
        service = {"a": "crawl4ai", "b": "searxng"}[ident[0]]
        return self.label(service), ("true" if self.running[service] else "false"), self.policy[service]

    def mutate(self, service, ident, *, stopping):
        self.operations.append((service, ident, stopping))
        self.running[service] = not stopping

    def setup_mocks(self):
        self.enterContext(patch.object(docker.inv, "report", side_effect=self.inventory))
        self.enterContext(patch.object(docker.inv, "_ids", side_effect=lambda *a: self.ids))
        self.enterContext(patch.object(docker.inv, "_private_files",
                                      return_value=({self.crawler_file, self.overlay_file}, True)))
        self.enterContext(patch.object(docker.inv, "_inspect", side_effect=self.inspect))
        self.enterContext(patch.object(docker, "_run", side_effect=self.mutate))

    def test_target_uses_full_ids_and_private_compose(self):
        self.setup_mocks()
        target = docker._safe_target("searxng", config_base=self.base)
        self.assertEqual(target.ident, SEARCH)
        self.assertFalse(target.running)
        self.assertEqual(target.policy, "no")

    def test_shared_published_port_blocks_all_mutations(self):
        self.setup_mocks()
        self.external = 1
        with self.assertRaises(docker.LifecycleError):
            docker.start("searxng", config_base=self.base,
                         health=lambda _: True, sleep=lambda _: None)
        with self.assertRaises(docker.LifecycleError):
            docker.stop("crawl4ai", config_base=self.base)
        self.assertEqual(self.operations, [])

    def test_service_id_duplication_or_foreign_source_fails_closed(self):
        self.setup_mocks()
        self.ids = [CRAWLER, CRAWLER]
        with self.assertRaises(docker.LifecycleError):
            docker.stop("crawl4ai", config_base=self.base)
        self.ids = [CRAWLER, SEARCH]
        original = self.label
        self.label = lambda s: {**original(s),
                                "com.docker.compose.project.config_files": "/foreign/compose.yaml"} if s == "searxng" else original(s)
        with self.assertRaises(docker.LifecycleError):
            docker.stop("crawl4ai", config_base=self.base)
        self.assertEqual(self.operations, [])

    def test_search_wake_and_safe_idle_stop_never_modify_policy(self):
        self.setup_mocks()
        awake = docker.start("searxng", config_base=self.base,
                             health=lambda name: self.running[name])
        self.assertTrue(awake)
        self.assertEqual(self.operations, [("searxng", SEARCH, False)])
        self.assertTrue(docker.stop("searxng", config_base=self.base))
        self.assertEqual(self.operations[-1], ("searxng", SEARCH, True))
        self.assertEqual(self.policy["searxng"], "no")

    def test_crawler_requires_live_probe_and_stops_after_failed_probe(self):
        self.setup_mocks()
        self.running["crawl4ai"] = False
        with self.assertRaisesRegex(docker.LifecycleError, "not configured"):
            docker.start("crawl4ai", config_base=self.base,
                         health=lambda _: True)
        self.assertEqual(self.operations, [])
        with self.assertRaisesRegex(docker.LifecycleError, "egress probe failed"):
            docker.start("crawl4ai", config_base=self.base,
                         health=lambda _: True, crawler_probe=lambda: False)
        self.assertEqual(self.operations,
                         [("crawl4ai", CRAWLER, False),
                          ("crawl4ai", CRAWLER, True)])
        self.assertFalse(self.running["crawl4ai"])

    def test_always_restart_policy_rejected_without_change(self):
        self.setup_mocks()
        self.policy["crawl4ai"] = "always"
        with self.assertRaisesRegex(docker.LifecycleError, "Restart policy"):
            docker.stop("crawl4ai", config_base=self.base)
        self.assertEqual(self.operations, [])

    def test_startup_timeout_is_reported_without_false_success(self):
        self.setup_mocks()
        count = [0]
        def clock():
            count[0] += 1
            return float(count[0] * 20)
        with self.assertRaisesRegex(docker.LifecycleError, "timed out"):
            docker.start("searxng", config_base=self.base,
                         health=lambda _: False,
                         clock=clock, sleep=lambda _: None)
        self.assertEqual(self.operations, [("searxng", SEARCH, False),
                                           ("searxng", SEARCH, True)])


if __name__ == "__main__":
    unittest.main()
