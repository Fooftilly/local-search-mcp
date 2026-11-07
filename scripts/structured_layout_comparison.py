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
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import urlsplit

from structured_schema import validate_spec

_KINDS = frozenset({"table", "cards", "jsonld"})
_MAX_MANIFEST_BYTES = 48 * 1024
_MAX_ANNOTATIONS = 20
_MAX_RECORDS = 20
# Fixed per-user path: CLI input must never select arbitrary local files.
_MANIFEST = Path.home() / ".config" / "local-search-mcp" / "structured-layouts.json"


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
        if (type(case) is not dict
                or not {"kind", "url", "spec", "expected"}.issubset(case)
                or len(case) not in (4, 5)
                or set(case) - {"kind", "url", "spec", "expected", "baseline_fields"}):
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
        mapping = case.get("baseline_fields", {})
        if (type(mapping) is not dict or len(mapping) > len(field_names)
                or not set(mapping).issubset(field_names)
                or (mapping and case["kind"] == "cards")):
            raise ValueError("Invalid typed baseline field mapping")
        normalized_map = {}
        for field_name, source_name in mapping.items():
            if (type(source_name) is not str
                    or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}",
                                    source_name, flags=re.ASCII) is None):
                raise ValueError("Invalid baseline field name")
            baseline_name = (source_name if case["kind"] == "jsonld"
                             else source_name.casefold())
            if baseline_name in normalized_map:
                raise ValueError("Ambiguous baseline field mapping")
            normalized_map[baseline_name] = field_name
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
                         "expected": expected, "baseline_fields": normalized_map})
    return approved


def _load_manifest() -> list[dict]:
    # No CLI-controlled file paths. Open the fixed operator config only as
    # a regular file, without following its final symlink or a TOCTOU gap.
    # O_NONBLOCK prevents a malicious named pipe from hanging the runner.
    try:
        fd = os.open(_MANIFEST, os.O_RDONLY | os.O_CLOEXEC |
                     os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError("Configured manifest unavailable") from exc
    with os.fdopen(fd, "rb") as handle:
        details = os.fstat(handle.fileno())
        if (not stat.S_ISREG(details.st_mode)
                or details.st_size > _MAX_MANIFEST_BYTES):
            raise ValueError("Configured manifest is not a bounded regular file")
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


def _baseline_rows(response: dict, kind: str,
                   field_mapping: dict[str, str] | None = None) -> list[dict]:
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
    # Card layouts have no typed baseline in the existing MCP API.
    # This returns only already-present typed values, never invented cards.
    mapping = field_mapping or {}
    converted = []
    for row in rows:
        selected = {}
        for key, value in row.items():
            field_name = mapping.get(key, key)
            if field_name in selected:
                # A JSON-LD object can contain both headline and name.
                # Never silently replace a native field with an alias.
                raise ValueError("Ambiguous typed baseline field mapping")
            selected[field_name] = value
        converted.append(selected)
    # Preserve the bounded (at most 40) source rows until comparison
    # explicitly measures the separate 20-row score cap.
    return converted


def _record_alignment(truth: list[dict], observed: list[dict]) -> dict:
    """Maximum one-to-one row matching, preserving field associations.

    A field can be present in both outputs but attached to the WRONG
    record. Field-level precision cannot detect that. The small 20-row
    cap permits an exact bipartite matching for annotated field subsets.
    """
    if (type(truth) is not list or type(observed) is not list
            or len(truth) > _MAX_RECORDS or len(observed) > _MAX_RECORDS):
        return {"matched": 0, "precision_pct": None,
                "completeness_pct": None}
    assigned = [-1] * len(observed)
    def match(index: int, visited: set[int]) -> bool:
        expected = truth[index]
        for candidate_id, row in enumerate(observed):
            if (candidate_id in visited or type(row) is not dict
                    or any(type(row.get(name)) is not str
                           or _clean(row[name]) != value
                           for name, value in expected.items())):
                continue
            visited.add(candidate_id)
            if assigned[candidate_id] == -1 or match(assigned[candidate_id], visited):
                assigned[candidate_id] = index
                return True
        return False
    matched = sum(match(i, set()) for i in range(len(truth)))
    return {"matched": matched, "expected_records": len(truth),
            "observed_records": len(observed),
            "precision_pct": round(100 * matched / len(observed), 1)
                             if observed else None,
            "completeness_pct": round(100 * matched / len(truth), 1)
                                if truth else None}


def _limit_context(response: dict) -> dict:
    info = response.get("structured_info")
    table = info.get("tables", {}) if type(info) is dict else {}
    if type(table) is not dict:
        table = {}
    jsonld = response.get("jsonld_info")
    if type(jsonld) is not dict:
        jsonld = {}
    markdown = response.get("markdown")
    return {
        "markdown_truncated": response.get("truncated")
                              if type(response.get("truncated")) is bool else None,
        "markdown_preview_chars": len(markdown) if type(markdown) is str else None,
        "table_candidates_truncated": table.get("truncated")
                                      if type(table.get("truncated")) is bool else None,
        "table_scan_capped": table.get("input_scan_capped")
                             if type(table.get("input_scan_capped")) is bool else None,
        "table_row_or_column_preview_capped": any(
            x.get("rows_truncated") or x.get("columns_truncated")
            for x in table.get("table_limits", [])
            if type(x) is dict)
            if type(table.get("table_limits")) is list else None,
        "jsonld_html_scan_truncated": jsonld.get("source_html_scan_truncated")
                                      if type(jsonld.get("source_html_scan_truncated")) is bool
                                      else None,
        "jsonld_limits_applied": jsonld.get("limits_applied")
                                 if type(jsonld.get("limits_applied")) is bool else None,
    }


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
            available_baseline_rows = _baseline_rows(
                baseline, case["kind"], case.get("baseline_fields"))
            # An operator's label alone cannot establish a table or JSON-LD
            # layout. Missing typed rows are not a valid three-layout
            # comparison, even if a heading happens to be extractable.
            # Cards have no typed baseline; they rely on operator annotation
            # and actual nonempty extracted records.
            if (case["kind"] in {"table", "jsonld"}
                    and not _field_counter(available_baseline_rows[:_MAX_RECORDS])):
                outcomes.append({"layout": case["kind"],
                                 "status": "typed_layout_not_observed"})
                continue
            if case["kind"] == "cards" and not extra.get("records"):
                outcomes.append({"layout": case["kind"],
                                 "status": "card_records_not_observed"})
                continue
            baseline_rows = available_baseline_rows[:_MAX_RECORDS]
            baseline_limits = _limit_context(baseline)
            baseline_limits["comparison_rows_observed"] = len(available_baseline_rows)
            baseline_limits["comparison_rows_scored"] = len(baseline_rows)
            baseline_limits["comparison_row_cap_applied"] = (
                len(available_baseline_rows) > _MAX_RECORDS)
            outcomes.append({
                "layout": case["kind"], "status": "evaluated",
                "structured": _match(expected, actual),
                "structured_record_alignment": _record_alignment(
                    case["expected"], extra["records"]),
                "baseline_typed_values": _match(expected, _field_counter(baseline_rows))
                                         if case["kind"] != "cards" else None,
                "baseline_record_alignment": _record_alignment(
                    case["expected"], baseline_rows)
                                         if case["kind"] != "cards" else None,
                "baseline_limits": baseline_limits,
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
    parser.add_argument("--preview", action="store_true",
                        help="Validate manifests and live approval state; no fetch")
    parser.add_argument("--live-approved", action="store_true",
                        help="Explicitly authorize two existing guarded crawls per case")
    args = parser.parse_args(argv)
    if args.preview == args.live_approved:
        parser.error("Choose exactly one of --preview or --live-approved")
    try:
        cases = _load_manifest()
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
