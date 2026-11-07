"""Real subprocess isolation regressions for the guarded structured pilot."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import structured_schema
import structured_selector_host as host


FIXTURES = Path(__file__).resolve().parents[1] / "web" / "templates"


def schema(strategy: str = "css") -> dict:
    return {
        "strategy": strategy,
        "base_selector": "article.card" if strategy == "css" else "//article",
        "fields": [{"name": "headline",
                    "selector": "h2" if strategy == "css" else ".//h2",
                    "type": "text"}],
    }


class IsolatedSelectorWorkerTests(unittest.TestCase):
    def test_real_subprocess_extracts_both_with_no_urls_or_tokens(self):
        page = (FIXTURES / "structured_selector_test_page.html").read_text()
        for strategy in ("css", "xpath"):
            with self.subTest(strategy=strategy):
                result = host.run_isolated_selectors(page, schema(strategy))
                self.assertEqual(result["records"][0]["headline"], "Public headline")
                self.assertEqual(result["extraction_method"],
                                 "crawl4ai_json_" + strategy)
                self.assertFalse(result["authorization_verified"])
                self.assertFalse(result["truncated"])
                self.assertNotIn("url", result)

    def test_worker_cell_clipping_is_explicit_and_result_stays_bounded(self):
        page = (FIXTURES / "structured_selector_test_long_cell.html").read_text()
        result = host.run_isolated_selectors(page, schema())
        self.assertEqual(result["rows_returned"], 1)
        self.assertEqual(len(result["records"][0]["headline"]), 240)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode("utf-8")),
                             structured_schema.MAX_OUTPUT_BYTES)

    def test_html_input_caps_before_any_process_or_network(self):
        page = "x" * (host.MAX_HTML_BYTES + 1)
        with patch.object(host.subprocess, "Popen",
                          side_effect=AssertionError("No worker may spawn")):
            with self.assertRaises(host.StructuredWorkerUnavailable):
                host.run_isolated_selectors(page, schema())
            with self.assertRaises(host.StructuredWorkerUnavailable):
                host.run_isolated_selectors("界" * (host.MAX_HTML_BYTES // 3 + 1),
                                            schema())
            with self.assertRaises(structured_schema.StructuredSchemaRejected):
                host.run_isolated_selectors("public", {**schema(),
                                           "base_selector": "article:hover"})

    def test_process_capacity_rejected_before_spawn(self):
        self.assertTrue(host._MAX_WORKERS.acquire(blocking=False))
        self.assertTrue(host._MAX_WORKERS.acquire(blocking=False))
        try:
            with patch.object(host.subprocess, "Popen",
                              side_effect=AssertionError("Capacity must fail early")):
                with self.assertRaises(host.StructuredWorkerUnavailable):
                    host.run_isolated_selectors("public", schema())
        finally:
            host._MAX_WORKERS.release()
            host._MAX_WORKERS.release()

    def test_timeout_terminates_hanging_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp, "hung_worker.py")
            script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            with (patch.object(host, "_WORKER_FILE", script),
                  patch.object(host, "WALL_SECONDS", 0.2)):
                started = time.monotonic()
                with self.assertRaisesRegex(host.StructuredWorkerUnavailable, "wall time"):
                    host.run_isolated_selectors("public", schema())
            self.assertLess(time.monotonic() - started, 4.0)

    def test_child_rejects_network_and_reports_kernel_limits(self):
        # The test runner makes no external request. The child attempts only
        # an AF_INET socket() syscall after seccomp initialization.
        answer = subprocess.run(
            [sys.executable, "-I", str(host._WORKER_FILE), "--check-isolation"],
            capture_output=True, text=True, timeout=5,
            cwd="/", env={"PYTHONNOUSERSITE": "1"},
            check=True,
        )
        result = json.loads(answer.stdout)
        self.assertTrue(result["network_denied"])
        self.assertEqual(result["cpu_soft_seconds"], 2)
        self.assertLessEqual(result["max_address_space"], 384 * 1024 * 1024)
        self.assertLessEqual(result["max_output_file"], 64 * 1024)
        self.assertNotIn("url", result)

    def test_invalid_worker_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp, "invalid_worker.py")
            script.write_text('print("SENSITIVE_NOT_JSON")\n', encoding="utf-8")
            with patch.object(host, "_WORKER_FILE", script):
                with self.assertRaises(host.StructuredWorkerUnavailable) as err:
                    host.run_isolated_selectors("public", schema())
            self.assertNotIn("SENSITIVE_NOT_JSON", str(err.exception))


if __name__ == "__main__":
    unittest.main()
