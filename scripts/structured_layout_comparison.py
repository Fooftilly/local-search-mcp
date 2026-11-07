"""Attended quality/cost comparison of three separately approved HTML layouts.

Never approve a domain, start an MCP listener, call a second downloader, or
print source text, expected answers, credentials, or full URLs. A comparison
requires explicit --live-approved and an operator-annotated local manifest.
Each case makes two existing guarded bridge.web_fetch calls (baseline and
opt-in schema). Read-only --preview validates the manifest and approvals.
"""
from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from structured_schema import validate_spec

_KINDS = frozenset({"table", "cards", "jsonld"})
_MAX_MANIFEST_BYTES = 48 * 1024
_MAX_ANNOTATIONS = 20
_MAX_RECORDS = 20


def _duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError("Nonfinite JSON value")


def _clean(value: str) -> str:
    return " ".join(value.split()).strip()


def _validate_manifest(obj: object) -> list[dict]:
    if type(obj) is not dict or set(obj) != {"cases"}:
        raise ValueError("Invalid manifest")
    cases = obj["cases"]
    if type(cases) is not list or len(cases) != 3:
        raise ValueError("Require exactly three annotated layouts")
    if {x.get("kind") for x in cases if type(x) is dict} != _KINDS:
        raise ValueError("Require table, cards and jsonld exactly once")
    approved: list[dict] = []
    urls: set[str] = set()
    for case in cases:
        if type(case) is not dict or set(case) != {"kind", "url", "spec", "expected"}:
            raise ValueError("Unexpected case fields")
        url, truth = case["url"], case["expected"]
        if type(url) is not str or not 1 <= len(url) <= 2048:
            raise ValueError("Invalid HTTPS URL")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.port is not None
                or parsed.query or parsed.fragment
                or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                or url != url.strip() or url in urls):
            raise ValueError("Only distinct canonical public HTTPS URLs allowed")
        spec = validate_spec(case["spec"])
        if (type(truth) is not list or not 1 <= len(truth) <= _MAX_ANNOTATIONS
                or any(type(row) is not dict or not row for row in truth)):
            raise ValueError("Invalid annotated ground truth")
        field_names = {f["name"] for f in spec["fields"]}
        expected = []
        for row in truth:
            if not set(row).issubset(field_names):
                raise ValueError("Truth field is not declared in schema")
            normalized = {}
            for key, value in row.items():
                if (type(value) is not str or not 1 <= len(value) <= 240
                        or not _clean(value)):
                    raise ValueError("Invalid ground-truth value")
                normalized[key] = _clean(value)
            expected.append(normalized)
        urls.add(url)
        approved.append({"kind": case["kind"], "url": url, "spec": spec,
                         "expected": expected})
    return approved


def _load_manifest(path: str) -> list[dict]:
    local = Path(path)
    if not local.is_file() or local.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("Manifest file unavailable or oversized")
    with local.open("rb") as handle:
        raw = handle.read(_MAX_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds input cap")
    obj = json.loads(raw, object_pairs_hook=_duplicate_keys,
                     parse_constant=_nonfinite)
    return _validate_manifest(obj)


def _preflight_permissions(cases: list[dict]) -> None:
    # Check every host BEFORE making any fetch, so an invalid case cannot
    # cause two successful fetches on the first host before later denial.
    from domain_policy import is_domain_currently_allowed
    if not all(is_domain_currently_allowed(urlsplit(x["url"]).hostname)
               for x in cases):
        raise ValueError("At least one layout host lacks a current approval")


def _field_counter(rows: object) -> Counter[tuple[str, str]]:
    values: Counter[tuple[str, str]] = Counter()
    if type(rows) is not list or len(rows) > _MAX_RECORDS:
        return values
    for row in rows:
        if type(row) is not dict:
            return Counter()
        for name, value in row.items():
            if type(name) is str and type(value) is str and _clean(value):
                values[(name, _clean(value))] += 1
    return values


def _match(ground_truth: Counter, candidates: Counter) -> dict:
    matched = sum((ground_truth & candidates).values())
    total_truth, total_candidates = sum(ground_truth.values()), sum(candidates.values())
    return {
        "matched": matched,
        "expected_count": total_truth,
        "observed_count": total_candidates,
        "precision_pct": round(100 * matched / total_candidates, 1)
                         if total_candidates else None,
        "completeness_pct": round(100 * matched / total_truth, 1)
                            if total_truth else None,
    }


def _baseline_values(response: dict, kind: str) -> Counter[tuple[str, str]]:
    rows: list[dict] = []
    if kind == "table":
        for table in response.get("tables", [])[:4]:
            if type(table) is not dict:
                continue
            headers = table.get("headers")
            if type(headers) is not list:
                continue
            for cells in table.get("rows", [])[:10]:
                if type(cells) is not list:
                    continue
                rows.append({str(key).strip().casefold().replace(" ", "_"): value
                             for key, value in zip(headers, cells)
                             if type(key) is str and type(value) is str})
    elif kind == "jsonld":
        for record in response.get("jsonld", [])[:6]:
            if type(record) is dict and type(record.get("fields")) is dict:
                rows.append(record["fields"])
    # Card layouts have no typed card baseline in the current 12-tool API.
    # Compare expected-value evidence in Markdown separately, not as
    # invented card-row precision.
    return _field_counter(rows[:_MAX_RECORDS])


def _markdown_coverage(markdown: object, truth: Counter) -> dict:
    text = _clean(markdown) if type(markdown) is str else ""
    matched = sum(count for (_, value), count in truth.items() if value in text)
    total = sum(truth.values())
    return {"expected_count": total, "evidence_hits": matched,
            "evidence_coverage_pct": round(100 * matched / total, 1) if total else None,
            "precision_pct": None}


def _load_local_credentials() -> None:
    # Same owner-only local token loader as the three-page smoke script.
    from scripts.structured_fetch_attended import _load_local_credentials as load
    load()


def compare(cases: list[dict]) -> dict:
    _preflight_permissions(cases)
    _load_local_credentials()
    import bridge  # token and opt-in flag must exist before bridge import
    outcomes = []
    for case in cases:
        expected = _field_counter(case["expected"])
        t0 = time.monotonic()
        try:
            baseline = bridge.web_fetch(case["url"], fresh=True,
                                        include_tables=True, include_jsonld=True)
            base_latency = round(time.monotonic() - t0, 3)
            t1 = time.monotonic()
            candidate = bridge.web_fetch(case["url"], fresh=True,
                                         structured_spec=case["spec"])
            extract_latency = round(time.monotonic() - t1, 3)
            if (type(baseline) is not dict or baseline.get("success") is not True
                    or type(candidate) is not dict or candidate.get("success") is not True
                    or type(candidate.get("structured_extraction")) is not dict):
                outcomes.append({"layout": case["kind"], "status": "fetch_failed"})
                continue
            extra = candidate["structured_extraction"]
            actual = _field_counter(extra.get("records"))
            if (extra.get("authorization_verified") is not False
                    or extra.get("fact_verification") == "verified"
                    or extra.get("provenance", {}).get("fact_verification")
                    != "not_verified"):
                outcomes.append({"layout": case["kind"], "status": "invalid_claim"})
                continue
            outcomes.append({
                "layout": case["kind"], "status": "evaluated",
                "structured": _match(expected, actual),
                "baseline_typed_values": _match(expected, _baseline_values(baseline, case["kind"]))
                                         if case["kind"] != "cards" else None,
                "markdown_evidence": _markdown_coverage(baseline.get("markdown"), expected),
                "baseline_seconds": base_latency,
                "structured_seconds": extract_latency,
                "structured_truncated": extra.get("truncated"),
                "baseline_tables": len(baseline.get("tables", [])),
                "baseline_jsonld": len(baseline.get("jsonld", [])),
                "crawl_requests": 2,
                "llm_calls": 0,
                "paid_provider_cost": "not_applicable_self_hosted",
            })
        except Exception as exc:
            # Never show URL, raw source, annotated expected values or token.
            outcomes.append({"layout": case["kind"],
                             "status": "exception_" + type(exc).__name__[:35]})
    return {
        "mode": "attended_structured_layout_comparison",
        "evidence_source": "operator_annotated_ground_truth",
        "site_completeness": "unknown",
        "approvals_modified": False,
        "persisted_settings_modified": False,
        "metrics_are": "exact_field_value_match; Markdown is evidence coverage only",
        "results": outcomes,
        "evaluated": sum(x.get("status") == "evaluated" for x in outcomes),
        "passed": len(outcomes) == 3 and
                  all(x.get("status") == "evaluated" for x in outcomes),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True,
                        help="Local JSON manifest with three approved, annotated layouts")
    parser.add_argument("--preview", action="store_true",
                        help="Validate manifests and live approval state; no fetch")
    parser.add_argument("--live-approved", action="store_true",
                        help="Explicitly authorize two existing guarded crawls per case")
    args = parser.parse_args(argv)
    if args.preview == args.live_approved:
        parser.error("Choose exactly one of --preview or --live-approved")
    try:
        cases = _load_manifest(args.manifest)
        _preflight_permissions(cases)
        if args.preview:
            result = {"status": "ready_for_attended_comparison",
                      "layouts": [x["kind"] for x in cases],
                      "fetches_if_live": 6, "approvals_modified": False}
        else:
            result = compare(cases)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        result = {"status": "preflight_failed", "error_type": type(exc).__name__,
                  "approvals_modified": False}
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result.get("status") == "ready_for_attended_comparison" or result.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
