"""Offline tests: three-layout quality evaluation never widens crawl approvals."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from scripts import structured_layout_comparison as subject


def _case(kind, url):
    return {
        "kind": kind, "url": url,
        "spec": {
            "strategy": "css", "base_selector": "article",
            "fields": [{"name": "title", "selector": "h2", "type": "text"}],
        },
        "expected": [{"title": "public entry"}],
    }


CASES = [
    _case("table", "https://example.com/table"),
    _case("cards", "https://example.com/cards"),
    _case("jsonld", "https://example.com/structured"),
]


class LayoutComparisonTests(unittest.TestCase):
    def test_manifest_exactly_three_kinds_and_restricted_schemas(self):
        cases = subject._validate_manifest({"cases": CASES})
        self.assertEqual({c["kind"] for c in cases},
                         {"table", "cards", "jsonld"})
        invalid = [
            {"cases": CASES[:2]},
            {"cases": CASES + [CASES[0]]},
            {"cases": [CASES[0], CASES[1], CASES[1]]},
            {"cases": [{**CASES[0], "url": "http://example.com/table"},
                       *CASES[1:]]},
            {"cases": [{**CASES[0], "url": "https://example.com:443/table"},
                       *CASES[1:]]},
            {"cases": [{**CASES[0], "url": "https://me:token@example.com/table"},
                       *CASES[1:]]},
            {"cases": [{**CASES[0], "url": "https://example.com/table?q=secret"},
                       *CASES[1:]]},
            {"cases": [{**CASES[0], "expected": [{"wrong_field": "secret"}]},
                       *CASES[1:]]},
            {"cases": [{**CASES[0], "spec": {
                **CASES[0]["spec"], "base_selector": "script:hover"}}, *CASES[1:]]},
            {"cases": [{**CASES[0], "expected": [{"title": "X" * 400}]},
                       *CASES[1:]]},
        ]
        for manifest in invalid:
            with self.subTest(manifest=repr(manifest)[:120]):
                with self.assertRaises(ValueError):
                    subject._validate_manifest(manifest)

    def test_duplicate_keys_and_oversized_file_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "manifest.json"
            p.write_text('{"cases":[],"cases":[]}', encoding="utf-8")
            with self.assertRaises(ValueError):
                subject._load_manifest(str(p))
            p.write_bytes(b"x" * (subject._MAX_MANIFEST_BYTES + 1))
            with self.assertRaises(ValueError):
                subject._load_manifest(str(p))
            p.write_text(json.dumps({"cases": CASES}), encoding="utf-8")
            loaded = subject._load_manifest(str(p))
        self.assertEqual(len(loaded), 3)

    def test_all_hosts_approved_before_crawls(self):
        calls = []
        def permitted(host):
            calls.append(host)
            return host != "example.com"
        with (patch("domain_policy.is_domain_currently_allowed",
                    side_effect=permitted),
              patch.object(subject, "_load_local_credentials",
                           side_effect=AssertionError("no credentials")),
              patch.dict(sys.modules, {"bridge": types.SimpleNamespace(
                  web_fetch=lambda *a, **k: (_ for _ in ()).throw(
                      AssertionError("no network")))})):
            with self.assertRaises(ValueError):
                subject.compare(CASES)
        self.assertEqual(calls, ["example.com"])

    def test_runner_compares_three_baselines_and_redacts_values(self):
        calls = []
        def guarded(url, **kwargs):
            calls.append((url, kwargs))
            if "structured_spec" in kwargs:
                return {
                    "success": True,
                    "structured_extraction": {
                        "records": [{"title": "public entry"}],
                        "authorization_verified": False,
                        "provenance": {"fact_verification": "not_verified"},
                    },
                }
            return {
                "success": True, "markdown": "public entry",
                "tables": [{"headers": ["title"], "rows": [["public entry"]]}],
                "jsonld": [{"fields": {"title": "public entry"}}],
            }
        with (patch.object(subject, "_preflight_permissions"),
              patch.object(subject, "_load_local_credentials"),
              patch.dict(sys.modules, {"bridge": types.SimpleNamespace(
                  web_fetch=guarded)})):
            report = subject.compare(CASES)
        self.assertTrue(report["passed"])
        self.assertEqual(report["evaluated"], 3)
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(c[1]["fresh"] is True for c in calls))
        self.assertEqual([bool(c[1].get("structured_spec")) for c in calls],
                         [False, True, False, True, False, True])
        for entry in report["results"]:
            self.assertEqual(entry["structured"]["precision_pct"], 100)
            self.assertEqual(entry["structured"]["completeness_pct"], 100)
            self.assertEqual(entry["markdown_evidence"]["evidence_coverage_pct"], 100)
            self.assertEqual(entry["llm_calls"], 0)
            self.assertEqual(entry["crawl_requests"], 2)
            self.assertIsNotNone(entry["baseline_seconds"])
            self.assertIsNotNone(entry["structured_seconds"])
        self.assertIsNone(report["results"][1]["baseline_typed_values"])
        self.assertEqual(report["results"][0]["baseline_typed_values"]["precision_pct"], 100)
        self.assertEqual(report["results"][2]["baseline_typed_values"]["precision_pct"], 100)
        encoded = json.dumps(report)
        self.assertNotIn("public entry", encoded)
        self.assertNotIn("https://example.com/", encoded)

    def test_failure_cannot_claim_completeness(self):
        def denied(url, **kwargs):
            return {"success": False, "status": "approval_required",
                    "markdown": "PRIVATE_SOURCE_SHOULD_NOT_LEAK"}
        with (patch.object(subject, "_preflight_permissions"),
              patch.object(subject, "_load_local_credentials"),
              patch.dict(sys.modules, {"bridge": types.SimpleNamespace(
                  web_fetch=denied)})):
            report = subject.compare(CASES)
        self.assertFalse(report["passed"])
        self.assertEqual(report["evaluated"], 0)
        self.assertTrue(all(x["status"] == "fetch_failed"
                            for x in report["results"]))
        self.assertNotIn("PRIVATE_SOURCE_SHOULD_NOT_LEAK", json.dumps(report))

    def test_preview_never_starts_crawls_or_loads_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "approved-layouts.json"
            p.write_text(json.dumps({"cases": CASES}), encoding="utf-8")
            output = io.StringIO()
            with (patch("domain_policy.is_domain_currently_allowed", return_value=True),
                  patch.object(subject, "_load_local_credentials",
                               side_effect=AssertionError("no credentials")),
                  patch.object(subject, "compare",
                               side_effect=AssertionError("no crawl")),
                  contextlib.redirect_stdout(output)):
                code = subject.main(["--preview", "--manifest", str(p)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["fetches_if_live"], 6)

    def test_requires_explicit_live_consent(self):
        with (contextlib.redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit) as err):
            subject.main(["--manifest", "/some/file.json"])
        self.assertEqual(err.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
