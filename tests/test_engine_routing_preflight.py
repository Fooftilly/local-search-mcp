"""Read-only acceptance preflight; no backend wake or provider traffic."""
import json
import unittest
from unittest.mock import patch

import bridge
import engine_routing_plan as plan
from scripts import engine_routing_preflight as preflight


INVENTORY = [
    {"name": "duckduckgo", "enabled": True, "categories": ["general", "images"]},
    {"name": "qwant", "enabled": True, "categories": ["general"]},
    {"name": "bing images", "enabled": True, "categories": ["images"]},
    {"name": "google", "enabled": True, "categories": ["general"]},
    {"name": "disabled", "enabled": False, "categories": ["general"]},
]
POLICY = {"duckduckgo": 10, "qwant": 10, "bing images": 10, "disabled": 30}


def history():
    return {
        "hours": 24,
        "available": True,
        "source_snapshot_lock_acquired": True,
        "retention_or_scan_limited": False,
        "isolated_engine_groups_truncated": False,
        "isolated_engine_operations": [
            {"name": "duckduckgo", "direct_operations": 20,
             "direct_verified_results": 12, "direct_verified_result_mean_ms": 100},
            {"name": "qwant", "direct_operations": 20,
             "direct_verified_results": 19, "direct_verified_result_mean_ms": 130},
            {"name": "bing images", "direct_operations": 20,
             "direct_verified_results": 18, "direct_verified_result_mean_ms": 110},
        ],
    }


class RoutingPreflightTests(unittest.TestCase):
    def _run(self, *, routing="evidence", enabled=False, inventory=INVENTORY,
             observed=None):
        with (patch.object(preflight.engine_pacing_dashboard, "snapshot",
                           return_value={"enabled": enabled, "engines": POLICY,
                                         "routing": routing}) as config,
              patch.object(preflight.engine_dashboard, "snapshot",
                           return_value=(inventory, ("duckduckgo", "qwant"), "rev")) as live,
              patch.object(preflight, "_verified_raw_active_inventory",
                           return_value=[{"name": item["name"],
                                          "categories": tuple(item["categories"])}
                                         for item in inventory if item["enabled"]]) as raw,
              patch.object(preflight.provider_observations, "observations",
                           return_value=history() if observed is None else observed) as reader):
            result = preflight.report()
        config.assert_called_once_with()
        live.assert_called_once_with()
        raw.assert_called_once_with(inventory)
        reader.assert_called_once_with(24)
        return result

    def test_fixed_vs_evidence_replay_does_not_mutate_policy(self):
        result = self._run()
        self.assertEqual(result["status"], "ready_for_attended_comparison")
        self.assertFalse(result["pacing_enabled"])
        self.assertEqual(result["routing"], "evidence")
        self.assertTrue(result["history_complete"])
        self.assertEqual(result["plans"][0]["fixed"], ["duckduckgo", "qwant"])
        self.assertEqual(result["plans"][0]["evidence"], ["qwant", "duckduckgo"])
        self.assertEqual(result["plans"][0]["effective_if_enabled"], ["qwant", "duckduckgo"])
        self.assertEqual(result["plans"][1]["scope"], "images")
        self.assertEqual(result["plans"][1]["fixed"], ["bing images"])
        self.assertEqual(result["configured_but_unavailable"], ["disabled"])
        self.assertEqual(result["searches_sent"], 0)
        self.assertEqual(result["services_started"], 0)
        self.assertIs(result["settings_modified"], False)
        self.assertNotIn("google", json.dumps(result))

    def test_dedicated_category_only_is_eligible_even_without_general(self):
        only_images = [{"name": "bing images", "enabled": True,
                        "categories": ["images"]}]
        result = self._run(inventory=only_images)
        self.assertEqual(result["status"], "ready_for_attended_comparison")
        self.assertFalse(result["general_route_available"])
        self.assertEqual(result["plans"][0]["eligible_engines"], 0)
        self.assertEqual(result["plans"][1]["scope"], "images")
        self.assertEqual(result["plans"][1]["effective_if_enabled"], ["bing images"])

    def test_raw_bridge_inventory_rejects_normalized_duplicate_category(self):
        import httpx
        verified = [{"name": "bing images", "enabled": True,
                     "categories": ["images"]}]
        original = httpx.Client
        def handler(request):
            self.assertEqual(request.url.path, "/config")
            return httpx.Response(200, json={"engines": [
                {"name": "bing images", "enabled": True,
                 "categories": ["images", "images"]}]})
        def factory(**kwargs):
            return original(transport=httpx.MockTransport(handler), trust_env=False)
        with patch.object(preflight.httpx, "Client", side_effect=factory):
            with self.assertRaises(ValueError):
                preflight._verified_raw_active_inventory(verified)

    def test_backend_drift_returns_no_plan_and_sanitized_blocker(self):
        with (patch.object(preflight.engine_pacing_dashboard, "snapshot",
                           return_value={"enabled": True, "engines": POLICY,
                                         "routing": "evidence"}),
              patch.object(preflight.engine_dashboard, "snapshot",
                           return_value=(INVENTORY, (), "rev")),
              patch.object(preflight, "_verified_raw_active_inventory",
                           side_effect=ValueError("PRIVATE_DRIFT")),
              patch.object(preflight.provider_observations, "observations",
                           side_effect=AssertionError("must not read"))):
            result = preflight.report()
        self.assertEqual(result["status"], "inventory_unavailable")
        self.assertEqual(result["plans"], [])
        self.assertNotIn("PRIVATE_DRIFT", str(result))

    def test_fixed_mode_and_incomplete_history_keep_name_order(self):
        fixed = self._run(routing="fixed")
        self.assertEqual(fixed["plans"][0]["effective_if_enabled"], ["duckduckgo", "qwant"])
        self.assertTrue(fixed["plans"][0]["evidence_eligible"])
        incomplete = {**history(), "retention_or_scan_limited": True}
        fallback = self._run(observed=incomplete)
        self.assertFalse(fallback["plans"][0]["evidence_eligible"])
        self.assertEqual(fallback["plans"][0]["effective_if_enabled"], ["duckduckgo", "qwant"])
        missing = {**history()}
        missing["isolated_engine_operations"] = missing["isolated_engine_operations"][:1]
        self.assertFalse(self._run(observed=missing)["plans"][0]["evidence_eligible"])

    def test_sleeping_backend_refuses_without_history_access(self):
        with (patch.object(preflight.engine_pacing_dashboard, "snapshot",
                           return_value={"enabled": True, "engines": POLICY,
                                         "routing": "evidence"}),
              patch.object(preflight.engine_dashboard, "snapshot",
                           side_effect=ValueError("PRIVATE_URL")) as live,
              patch.object(preflight.provider_observations, "observations",
                           side_effect=AssertionError("must not read"))):
            result = preflight.report()
        live.assert_called_once_with()
        self.assertEqual(result["status"], "inventory_unavailable")
        self.assertNotIn("PRIVATE_URL", str(result))
        self.assertEqual(result["plans"], [])

    def test_missing_or_unsafe_policy_cannot_trigger_provider_call(self):
        for outcome in ({"enabled": False, "engines": {}, "routing": "fixed"},
                        ValueError("PRIVATE_SETTINGS")):
            with (self.subTest(outcome=str(type(outcome))),
                  patch.object(preflight.engine_pacing_dashboard, "snapshot",
                               side_effect=outcome if isinstance(outcome, Exception) else None,
                               return_value=outcome if isinstance(outcome, dict) else None),
                  patch.object(preflight.engine_dashboard, "snapshot",
                               side_effect=AssertionError("must not call"))):
                result = preflight.report()
            self.assertIn(result["status"], ("no_configured_engines", "invalid_policy"))
            self.assertEqual(result["searches_sent"], 0)
            self.assertNotIn("PRIVATE_SETTINGS", str(result))

    def test_candidate_subset_matches_bridge_contract(self):
        active = [dict(name=x["name"], categories=tuple(x["categories"]))
                  for x in INVENTORY if x["enabled"]]
        self.assertEqual(plan.candidates(active, POLICY),
                         ["duckduckgo", "qwant"])
        self.assertEqual(plan.candidates(active, POLICY, category="images"),
                         ["bing images"])
        self.assertEqual(plan.candidates(INVENTORY, POLICY),
                         ["duckduckgo", "qwant"])
        self.assertEqual(plan.candidates(INVENTORY, POLICY, category="general"),
                         ["qwant"])
        with self.assertRaises(ValueError):
            plan.candidates(active + [active[0]], POLICY)
        with self.assertRaises(ValueError):
            plan.candidates([{"name": "bad", "categories": ["general"],
                              "enabled": "yes"}], POLICY)

    def test_bridge_and_preflight_use_exact_same_eligible_subset(self):
        # The pure candidate selector is invoked after the bridge's bounded
        # /config validation; compare both without sending any /search.
        import httpx
        with httpx.Client(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"engines": INVENTORY})),
                trust_env=False) as client:
            self.assertEqual(bridge._paced_general_candidates(client, POLICY),
                             plan.candidates(INVENTORY, POLICY))
            self.assertEqual(bridge._paced_category_candidates(client, POLICY, "images"),
                             plan.candidates(INVENTORY, POLICY, category="images"))


if __name__ == "__main__":
    unittest.main()
