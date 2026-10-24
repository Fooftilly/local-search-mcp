"""Offline fail-closed tests: no Docker lifecycle actions."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
from scripts import on_demand_eligibility as gate


def state(**kw):
    return dict(state="running", managed_running=2, managed_stopped=0,
                unverified_project_containers=0,
                other_published_crawlers=0,
                verified_services={"crawl4ai": 1, "searxng": 1}, **kw)


class OnDemandEligibilityTests(unittest.TestCase):
    def test_defaults_and_candidate_never_activate(self):
        self.assertEqual(gate.validate_options(), {"enabled": False, "idle_minutes": 30})
        x = gate.eligibility(inventory=state())
        self.assertEqual(x["state"], "manual_verification_required")
        self.assertFalse(x["can_activate"])
        self.assertFalse(x["enabled"])

    def test_invalid_types_and_timeout_rejected(self):
        for n in (0, -1, 1441, True, "30", 1.5, None):
            with self.subTest(n=n), self.assertRaises(ValueError):
                gate.validate_options(idle_minutes=n)
        for enabled in ("true", 1, None):
            with self.assertRaises(ValueError):
                gate.validate_options(enabled=enabled)
        self.assertEqual(gate.validate_options(enabled=True, idle_minutes=1440)
                         ["idle_minutes"], 1440)

    def test_foreign_shared_crawler_refused(self):
        x = gate.eligibility(inventory={**state(), "other_published_crawlers": 1})
        self.assertEqual(x["state"], "blocked")
        self.assertFalse(x["can_activate"])
        self.assertIn("shared", x["reason"])

    def test_missing_and_unverified_refused(self):
        for changes in ({"state": "unknown"}, {"state": "unverified"},
                        {"state": "not_configured", "managed_running": 0},
                        {"unverified_project_containers": 1},
                        {"other_published_crawlers": None},
                        {"managed_running": True}, {"managed_stopped": -1},
                        {"managed_running": 1}):
            with self.subTest(changes=changes):
                data = {**state(), **changes}
                self.assertEqual(gate.eligibility(inventory=data)["state"], "blocked")

    def test_two_crawlers_or_missing_service_counts_are_not_candidates(self):
        for services in (None, {}, {"crawl4ai": 2, "searxng": 0},
                         {"crawl4ai": 1, "searxng": 0},
                         {"crawl4ai": True, "searxng": 1},
                         {"crawl4ai": 1, "searxng": 1, "extra": 0}):
            with self.subTest(services=services):
                data = {**state(), "verified_services": services}
                result = gate.eligibility(inventory=data)
                self.assertEqual(result["state"], "blocked")
                self.assertFalse(result["can_activate"])
        older = state()
        del older["verified_services"]
        self.assertEqual(gate.eligibility(inventory=older)["state"], "blocked")

    def test_two_stopped_containers_only_candidate(self):
        data = {**state(), "state": "stopped",
                "managed_running": 0, "managed_stopped": 2}
        self.assertFalse(gate.eligibility(inventory=data)["can_activate"])

    def test_cli_read_only(self):
        with (patch.object(gate.backend_inventory, "report",
                           return_value=state()) as get,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(gate.main(["--idle-minutes", "45"]), 0)
        get.assert_called_once_with()
        obj = json.loads(output.getvalue())
        self.assertEqual(obj["preview"], {"enabled": False, "idle_minutes": 45})
        self.assertFalse(obj["can_activate"])


if __name__ == "__main__":
    unittest.main()
