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

    def test_explicit_camel_case_jsonld_field_mapping(self):
        example = {**CASES[2], "spec": {
            "strategy": "css", "base_selector": "article",
            "fields": [{"name": "date_published", "selector": "time",
                        "type": "text"}]},
            "expected": [{"date_published": "2026-11-07"}],
            "baseline_fields": {"date_published": "datePublished"}}
        cases = subject._validate_manifest({
            "cases": [CASES[0], CASES[1], example]})
        self.assertEqual(cases[2]["baseline_fields"],
                         {"datePublished": "date_published"})
        baseline = {"jsonld": [{"fields": {"datePublished": "2026-11-07"}}]}
        converted = subject._baseline_rows(baseline, "jsonld",
                                            cases[2]["baseline_fields"])
        score = subject._match(subject._field_counter(cases[2]["expected"]),
                               subject._field_counter(converted))
        self.assertEqual(score["precision_pct"], 100)
        self.assertEqual(score["completeness_pct"], 100)
        with self.assertRaises(ValueError):
            subject._validate_manifest({"cases": [
                CASES[0], CASES[1], {**example,
                "baseline_fields": {"date_published": "datePublished", "unknown": "name"}}]})

    def test_reject_ambiguous_typed_baseline_aliases(self):
        first = {**CASES[2], "spec": {
            "strategy": "css", "base_selector": "article",
            "fields": [
                {"name": "one", "selector": "h2", "type": "text"},
                {"name": "two", "selector": "p", "type": "text"}]},
            "expected": [{"one": "A", "two": "B"}],
            "baseline_fields": {"one": "datePublished", "two": "datePublished"}}
        with self.assertRaises(ValueError):
            subject._validate_manifest({"cases": [CASES[0], CASES[1], first]})

    def test_record_alignment_detects_swapped_associations(self):
        truth = [{"title": "A", "date_published": "2026-01-01"},
                 {"title": "B", "date_published": "2026-01-02"}]
        swapped = [{"title": "A", "date_published": "2026-01-02"},
                   {"title": "B", "date_published": "2026-01-01"}]
        self.assertEqual(
            subject._match(subject._field_counter(truth),
                           subject._field_counter(swapped))["precision_pct"], 100)
        alignment = subject._record_alignment(truth, swapped)
        self.assertEqual(alignment["matched"], 0)
        self.assertEqual(alignment["completeness_pct"], 0)
        self.assertEqual(alignment["precision_pct"], 0)
        # Partial annotations should match a complete observed row exactly
        # once, not claim two copies from a single candidate record.
        partial = subject._record_alignment(
            [{"title": "A"}, {"title": "A"}], [{"title": "A", "extra": "value"}])
        self.assertEqual(partial["matched"], 1)
        self.assertEqual(partial["completeness_pct"], 50)

    def test_baseline_alias_cannot_overwrite_existing_publisher_field(self):
        # Existing publisher headline must never be replaced by an aliased
        # secondary name value just to inflate the comparison score.
        response = {"jsonld": [{"fields": {
            "headline": "Original", "name": "Alternate"}}]}
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            subject._baseline_rows(response, "jsonld", {"name": "headline"})

    def test_baseline_comparison_row_cap_is_reported_separately(self):
        def guarded(url, **kwargs):
            if "structured_spec" in kwargs:
                return {"success": True, "url": url, "structured_extraction": {
                    "records": [{"title": "public entry"}],
                    "authorization_verified": False,
                    "provenance": {"fact_verification": "not_verified"}}}
            return {
                "success": True, "url": url, "markdown": "public entry",
                "tables": [{"headers": ["title"],
                            "rows": [["public entry"]] * 10}
                           for _ in range(3)],
                "jsonld": [{"fields": {"title": "public entry"}}],
                "structured_info": {"tables": {
                    "truncated": False, "input_scan_capped": False,
                    "table_limits": [{"rows_truncated": False,
                                      "columns_truncated": False}] * 3}},
            }
        with (patch.object(subject, "_preflight_permissions"),
              patch.object(subject, "_load_local_credentials"),
              patch.dict(sys.modules, {"bridge": types.SimpleNamespace(
                  web_fetch=guarded)})):
            report = subject.compare(CASES)
        self.assertTrue(report["passed"])
        table_limits = report["results"][0]["baseline_limits"]
        self.assertEqual(table_limits["comparison_rows_observed"], 30)
        self.assertEqual(table_limits["comparison_rows_scored"], 20)
        self.assertTrue(table_limits["comparison_row_cap_applied"])
        self.assertFalse(table_limits["table_candidates_truncated"])
        self.assertFalse(table_limits["table_row_or_column_preview_capped"])
        # JSON-LD's independently bounded six-record parser is unaffected.
        self.assertFalse(report["results"][2]["baseline_limits"][
            "comparison_row_cap_applied"])

    def test_duplicate_keys_and_oversized_file_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "manifest.json"
            p.write_text('{"cases":[],"cases":[]}', encoding="utf-8")
            with patch.object(subject, "_MANIFEST", p):
                with self.assertRaises(ValueError):
                    subject._load_manifest()
                p.write_bytes(b"x" * (subject._MAX_MANIFEST_BYTES + 1))
                with self.assertRaises(ValueError):
                    subject._load_manifest()
                p.write_text(json.dumps({"cases": CASES}), encoding="utf-8")
                loaded = subject._load_manifest()
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
                    "success": True, "url": url,
                    "structured_extraction": {
                        "records": [{"title": "public entry"}],
                        "authorization_verified": False,
                        "provenance": {"fact_verification": "not_verified"},
                    },
                }
            return {
                "success": True, "url": url, "markdown": "public entry",
                "truncated": True,
                "tables": [{"headers": ["title"], "rows": [["public entry"]]}],
                "jsonld": [{"fields": {"title": "public entry"}}],
                "structured_info": {"tables": {
                    "truncated": True, "input_scan_capped": False,
                    "table_limits": [{"rows_truncated": True}]}},
                "jsonld_info": {"source_html_scan_truncated": False,
                                "limits_applied": True},
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
            self.assertEqual(entry["structured_record_alignment"]["completeness_pct"], 100)
            self.assertEqual(entry["markdown_evidence"]["evidence_coverage_pct"], 100)
            self.assertTrue(entry["baseline_limits"]["markdown_truncated"])
            self.assertTrue(entry["baseline_limits"]["table_candidates_truncated"])
            self.assertTrue(entry["baseline_limits"]["table_row_or_column_preview_capped"])
            self.assertTrue(entry["baseline_limits"]["jsonld_limits_applied"])
            self.assertEqual(entry["llm_calls"], 0)
            self.assertEqual(entry["crawl_requests"], 2)
            self.assertIsNotNone(entry["baseline_seconds"])
            self.assertIsNotNone(entry["structured_seconds"])
        self.assertIsNone(report["results"][1]["baseline_typed_values"])
        self.assertIsNone(report["results"][1]["baseline_record_alignment"])
        self.assertEqual(report["results"][0]["baseline_typed_values"]["precision_pct"], 100)
        self.assertEqual(report["results"][2]["baseline_typed_values"]["precision_pct"], 100)
        encoded = json.dumps(report)
        self.assertNotIn("public entry", encoded)
        self.assertNotIn("https://example.com/", encoded)

    def test_unverified_typed_layouts_cannot_make_benchmark_pass(self):
        # A manifest label is not evidence that the actual response contains
        # a typed table or JSON-LD layout. The benchmark must not advertise a
        # completed 3-layout evaluation from three heading-only pages.
        def guarded(url, **kwargs):
            if "structured_spec" in kwargs:
                return {"success": True, "url": url, "structured_extraction": {
                    "records": [{"title": "public entry"}],
                    "authorization_verified": False,
                    "provenance": {"fact_verification": "not_verified"}}}
            return {"success": True, "url": url, "markdown": "public entry",
                    "tables": [], "jsonld": [],
                    "structured_info": {}, "jsonld_info": {}}
        with (patch.object(subject, "_preflight_permissions"),
              patch.object(subject, "_load_local_credentials"),
              patch.dict(sys.modules, {"bridge": types.SimpleNamespace(
                  web_fetch=guarded)})):
            report = subject.compare(CASES)
        self.assertFalse(report["passed"])
        self.assertEqual(report["evaluated"], 1)
        self.assertEqual([r["status"] for r in report["results"]], [
            "typed_layout_not_observed", "evaluated", "typed_layout_not_observed"])
        self.assertNotIn("public entry", json.dumps(report))

    def test_empty_card_records_are_not_valid_comparison_evidence(self):
        def guarded(url, **kwargs):
            if "structured_spec" in kwargs:
                rows = [] if url.endswith("/cards") else [{"title": "public entry"}]
                return {"success": True, "url": url, "structured_extraction": {
                    "records": rows, "authorization_verified": False,
                    "provenance": {"fact_verification": "not_verified"}}}
            return {"success": True, "url": url, "markdown": "public entry",
                    "tables": [{"headers": ["title"], "rows": [["public entry"]]}],
                    "jsonld": [{"fields": {"title": "public entry"}}],
                    "structured_info": {}, "jsonld_info": {}}
        with (patch.object(subject, "_preflight_permissions"),
              patch.object(subject, "_load_local_credentials"),
              patch.dict(sys.modules, {"bridge": types.SimpleNamespace(
                  web_fetch=guarded)})):
            report = subject.compare(CASES)
        self.assertFalse(report["passed"])
        self.assertEqual(report["evaluated"], 2)
        self.assertEqual(report["results"][1]["status"], "card_records_not_observed")

    def test_comparison_requires_same_authorized_final_url(self):
        # Two independent approved crawls may redirect to DIFFERENT public
        # pages. Scoring their values against one annotation is invalid,
        # regardless of both individual fetches reporting success.
        def run_with_targets(baseline_target, candidate_target):
            def guarded(url, **kwargs):
                if "structured_spec" in kwargs:
                    return {"success": True, "url": candidate_target,
                            "structured_extraction": {
                                "records": [{"title": "public entry"}],
                                "authorization_verified": False,
                                "provenance": {"fact_verification": "not_verified"}}}
                return {"success": True, "url": baseline_target,
                        "markdown": "public entry",
                        "tables": [{"headers": ["title"], "rows": [["public entry"]]}],
                        "jsonld": [{"fields": {"title": "public entry"}}]}
            with (patch.object(subject, "_preflight_permissions"),
                  patch.object(subject, "_load_local_credentials"),
                  patch.dict(sys.modules, {"bridge": types.SimpleNamespace(
                      web_fetch=guarded)})):
                return subject.compare(CASES)

        original = "https://example.com/approved"
        other = "https://example.com/other"
        mismatch = run_with_targets(original, other)
        self.assertFalse(mismatch["passed"])
        self.assertEqual(mismatch["evaluated"], 0)
        self.assertTrue(all(x["status"] == "inconsistent_final_url"
                            for x in mismatch["results"]))
        self.assertNotIn(original, json.dumps(mismatch))
        self.assertNotIn(other, json.dumps(mismatch))

        missing = run_with_targets(original, None)
        self.assertFalse(missing["passed"])
        self.assertTrue(all(x["status"] == "inconsistent_final_url"
                            for x in missing["results"]))

        # A stable authorized redirect does not invalidate the comparison:
        # both fresh crawls measured the same final document.
        stable = run_with_targets(other, other)
        self.assertTrue(stable["passed"])
        self.assertEqual(stable["evaluated"], 3)

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
            with (patch.object(subject, "_MANIFEST", p),
                  patch("domain_policy.is_domain_currently_allowed", return_value=True),
                  patch.object(subject, "_load_local_credentials",
                               side_effect=AssertionError("no credentials")),
                  patch.object(subject, "compare",
                               side_effect=AssertionError("no crawl")),
                  contextlib.redirect_stdout(output)):
                code = subject.main(["--preview"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["fetches_if_live"], 6)

    def test_cli_cannot_choose_arbitrary_manifest_path(self):
        with (contextlib.redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit) as err):
            subject.main(["--preview", "--manifest", "/etc/shadow"])
        self.assertEqual(err.exception.code, 2)

    def test_symlinked_manifest_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret.json"
            secret.write_text(json.dumps({"cases": CASES}), encoding="utf-8")
            link = Path(tmp) / "structured-layouts.json"
            link.symlink_to(secret)
            with patch.object(subject, "_MANIFEST", link):
                with self.assertRaises(ValueError):
                    subject._load_manifest()

    def test_requires_explicit_live_consent(self):
        with (contextlib.redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit) as err):
            subject.main([])
        self.assertEqual(err.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
