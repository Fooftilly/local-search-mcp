"""Read-only, bounded routing evidence derived from direct search observations."""
import unittest
from unittest.mock import patch

import engine_pacing_dashboard
import engine_routing_evidence as evidence


def report(rows=(), *, available=True, locked=True, limited=False, truncated=False):
    return {
        "hours": 24, "available": available,
        "source_snapshot_lock_acquired": locked,
        "retention_or_scan_limited": limited,
        "isolated_engine_groups_truncated": truncated,
        "isolated_engine_operations": list(rows),
    }


def entry(name, attempts, results, ms):
    return {
        "name": name, "direct_operations": attempts,
        "direct_verified_results": results,
        "direct_verified_result_mean_ms": ms,
    }


class EngineEvidenceTests(unittest.TestCase):
    def test_minimum_sample_and_observed_ranking(self):
        cfg = {"duckduckgo": 15, "qwant": 60, "brave": 30}
        doc = report([
            entry("duckduckgo", 20, 17, 450),
            entry("qwant", 14, 12, 790),
            entry("brave", 9, 9, 20),
            entry("unlisted", 1000, 1000, 1),
        ])
        result = evidence.compare(cfg, doc)
        self.assertTrue(result["complete_history"])
        self.assertEqual(result["comparable_count"], 2)
        self.assertEqual([row["name"] for row in result["rows"]],
                         ["qwant", "duckduckgo", "brave"])
        self.assertEqual(result["rows"][0]["fraction"], 85.7)
        self.assertFalse(result["rows"][2]["comparable"])
        self.assertNotIn("unlisted", str(result))

    def test_no_history_missing_samples_and_truncated_never_claim_comparison(self):
        cfg = {"brave": 60}
        row = entry("brave", 20, 18, 20)
        for opts in ({"available": False}, {"locked": False},
                     {"limited": True}, {"truncated": True}):
            with self.subTest(opts=opts):
                result = evidence.compare(cfg, report([row], **opts))
                self.assertEqual(result["comparable_count"], 0)
                self.assertFalse(result["complete_history"])
        for row in (entry("brave", 10, 0, None),
                    entry("brave", 9, 9, 25)):
            with self.subTest(row=row):
                self.assertFalse(evidence.compare(cfg, report([row]))["rows"][0]["comparable"])

    def test_reject_untrusted_metrics_without_disclosing_strings(self):
        bad = (
            entry("brave", True, 1, 10),
            entry("brave", 20, 21, 5),
            entry("brave", 10, 10, float("nan")),
        )
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                evidence.compare({"brave": 60}, report([row]))
        with self.assertRaises(ValueError):
            evidence.compare({"brave": 60}, report(
                [entry("brave", 10, 8, 10), entry("brave", 10, 8, 10)]))

    def test_editor_escapes_names_no_backend_or_policy_mutations(self):
        configured = {"brave": 15, "<img onerror=alert(1)>": 15}
        observations = report([entry("brave", 10, 8, 310)])
        with (patch.object(engine_pacing_dashboard.provider_observations,
                           "observations", return_value=observations) as reader,
              patch.object(engine_pacing_dashboard.engine_dashboard,
                           "snapshot", side_effect=AssertionError("must not wake"))):
            page = engine_pacing_dashboard.evidence_preview(configured)
        reader.assert_called_once_with(24)
        self.assertIn("brave", page)
        self.assertIn("&lt;img onerror=alert(1)&gt;", page)
        self.assertNotIn("<img onerror=alert(1)>", page)
        self.assertIn("preview only", page)
        self.assertIn("80.0%", page)

    def test_unavailable_metrics_cannot_break_operator_settings(self):
        with patch.object(engine_pacing_dashboard.provider_observations,
                          "observations", side_effect=OSError("PRIVATE PATH")):
            text = engine_pacing_dashboard.evidence_preview({"brave": 15})
        self.assertIn("unavailable", text)
        self.assertNotIn("PRIVATE PATH", text)


if __name__ == "__main__":
    unittest.main()
