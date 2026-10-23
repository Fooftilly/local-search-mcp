"""Offline ownership diagnostics; every Docker interaction is mocked."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import backend_inventory as inventory

CRAWLER = "a" * 64
SEARCH = "b" * 64
SHARED = "c" * 64


class DockerOwnershipTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.project_dir = self.base / "local-search-mcp" / "crawler-compose"
        self.project_dir.mkdir(parents=True, mode=0o700)
        (self.project_dir / "compose.yaml").write_text("name: local-search-crawler\n")
        self.project_ids = [CRAWLER]
        self.published_ids = [CRAWLER, SHARED]
        self.labels = {CRAWLER: self.labels_for("crawl4ai")}
        self.running = {CRAWLER: True}
        self.calls = []

    def labels_for(self, service, *, extra=None):
        files = [str(self.project_dir / "compose.yaml")]
        if service == "searxng":
            files.append(str(self.project_dir / "searxng.compose.yaml"))
        if extra:
            files.append(extra)
        return {"com.docker.compose.project": inventory.PROJECT,
                "com.docker.compose.service": service,
                "com.docker.compose.project.config_files": ",".join(files)}

    def fake_docker(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.assertEqual(argv[:3], ["docker", "--host", "unix:///var/run/docker.sock"])
        self.assertEqual(kwargs["check"], False)
        self.assertTrue(kwargs["capture_output"])
        self.assertNotIn("DOCKER_CONTEXT", kwargs["env"])
        self.assertNotIn("DOCKER_HOST", kwargs["env"])
        self.assertNotIn("COMPOSE_PROJECT_NAME", kwargs["env"])
        command = argv[3]
        if command == "ps":
            self.assertIn("--no-trunc", argv)
            ids = (self.project_ids if any(inventory.PROJECT in word for word in argv)
                   else self.published_ids)
            return subprocess.CompletedProcess(argv, 0, "\n".join(ids) + "\n", "")
        if command == "inspect":
            ident = argv[-1]
            if "Config.Labels" in " ".join(argv):
                raw = json.dumps(self.labels.get(ident, {}))
            else:
                raw = "true" if self.running.get(ident) else "false"
            return subprocess.CompletedProcess(argv, 0, raw + "\n", "")
        raise AssertionError("Unexpected mutating Docker command: " + command)

    def report(self):
        with patch.object(inventory.subprocess, "run", side_effect=self.fake_docker):
            return inventory.report(config_base=self.base)

    def test_managed_crawler_and_external_shared_stack_are_separate(self):
        result = self.report()
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["managed_running"], 1)
        self.assertEqual(result["other_published_crawlers"], 1)
        self.assertEqual(result["unverified_project_containers"], 0)
        self.assertNotIn(CRAWLER, json.dumps(result))
        self.assertNotIn(SHARED, json.dumps(result))
        self.assertNotIn(str(self.project_dir), json.dumps(result))
        self.assertFalse(any(cmd[3] in ("start", "stop", "update", "rm", "run", "exec")
                             for cmd in self.calls))

    def test_project_stopped_does_not_mean_external_backend_stopped(self):
        self.running[CRAWLER] = False
        result = self.report()
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["managed_stopped"], 1)
        self.assertEqual(result["other_published_crawlers"], 1)

    def test_optional_searxng_overlay_is_verified(self):
        (self.project_dir / "searxng.compose.yaml").write_text("services: {}\n")
        self.project_ids.append(SEARCH)
        self.labels[SEARCH] = self.labels_for("searxng")
        self.running[SEARCH] = False
        result = self.report()
        self.assertEqual(result["state"], "partially_running")
        self.assertEqual(result["managed_running"], 1)
        self.assertEqual(result["managed_stopped"], 1)

    def test_foreign_compose_override_is_not_owned(self):
        self.labels[CRAWLER] = self.labels_for("crawl4ai", extra="/foreign/override.yaml")
        result = self.report()
        self.assertEqual(result["state"], "unverified")
        self.assertEqual(result["managed_running"], 0)
        self.assertEqual(result["unverified_project_containers"], 1)

    def test_searxng_without_overlay_is_not_owned(self):
        self.labels[CRAWLER] = self.labels_for("searxng")
        result = self.report()
        self.assertEqual(result["state"], "unverified")

    def test_missing_config_with_project_containers_is_unverified(self):
        (self.project_dir / "compose.yaml").unlink()
        self.project_dir.rmdir()
        result = self.report()
        self.assertEqual(result["state"], "unverified")

    def test_symlinked_compose_config_is_not_accepted(self):
        (self.project_dir / "compose.yaml").unlink()
        (self.project_dir / "compose.yaml").symlink_to(self.base / "untrusted")
        self.assertEqual(self.report()["state"], "unverified")

    def test_docker_unavailable_is_unknown_not_no_managed_containers(self):
        with patch.object(inventory.subprocess, "run",
                          side_effect=OSError("private credentials")):
            result = inventory.report(config_base=self.base)
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["other_published_crawlers"])
        self.assertNotIn("private credentials", json.dumps(result))

    def test_malformed_container_ids_fail_closed(self):
        self.project_ids = ["short"]
        self.assertEqual(self.report()["state"], "unknown")

    def test_missing_project_reports_not_configured_without_claiming_ownership(self):
        self.project_ids = []
        self.labels = {}
        self.running = {}
        self.assertEqual(self.report()["state"], "not_started")
        (self.project_dir / "compose.yaml").unlink()
        self.project_dir.rmdir()
        self.assertEqual(self.report()["state"], "not_configured")

    def test_docker_commands_ignore_untrusted_selection_environment(self):
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "remote",
                                     "DOCKER_HOST": "tcp://remote",
                                     "COMPOSE_PROJECT_NAME": "hermes-web"}):
            result = self.report()
        self.assertEqual(result["state"], "running")


if __name__ == "__main__":
    unittest.main()
