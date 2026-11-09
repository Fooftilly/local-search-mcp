"""Real subprocess isolation regressions for the guarded structured pilot."""
from __future__ import annotations

import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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

    def test_xpath_body_heading_matches_attended_spec(self):
        # Exact selector pair used by the attended XPath smoke test; this
        # must work independently of network access or page size.
        page = (FIXTURES / "structured_selector_test_page.html").read_text()
        spec = {
            "strategy": "xpath", "base_selector": "//body",
            "fields": [{"name": "heading", "selector": ".//h1", "type": "text"}],
        }
        result = host.run_isolated_selectors(page, spec)
        self.assertEqual(result["records"], [{"heading": "Usable"}])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["extraction_method"], "crawl4ai_json_xpath")

    def test_xpath_field_is_scoped_to_its_article_not_document(self):
        page = (FIXTURES / "structured_selector_test_multiple_articles.html").read_text()
        scoped = schema("xpath")
        result = host.run_isolated_selectors(page, scoped)
        self.assertEqual(
            [row["headline"] for row in result["records"]],
            ["First article", "Second article", None],
        )
        # A // field selector would scan from the document root and copy
        # the outside heading into every row. Reject before spawning.
        unscoped = {**scoped, "fields": [
            {**scoped["fields"][0], "selector": "//h2"}]}
        with patch.object(host.subprocess, "Popen",
                          side_effect=AssertionError("invalid selector reached child")):
            with self.assertRaises(structured_schema.StructuredSchemaRejected):
                host.run_isolated_selectors(page, unscoped)

    def test_worker_cell_clipping_is_explicit_and_result_stays_bounded(self):
        page = (FIXTURES / "structured_selector_test_long_cell.html").read_text()
        result = host.run_isolated_selectors(page, schema())
        self.assertEqual(result["rows_returned"], 1)
        self.assertEqual(len(result["records"][0]["headline"]), 240)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode("utf-8")),
                             structured_schema.MAX_OUTPUT_BYTES)

    def test_stale_charset_and_malformed_markup_preserve_unicode_and_text(self):
        # Source declarations can be stale once Crawl4AI has decoded HTML.
        # The test fixture also has recoverable malformed markup and escaped
        # script-shaped text, kept outside Python per AGENTS.md.
        page = (FIXTURES / "structured_selector_test_malformed_charset.html").read_text()
        spec = {"strategy": "css", "base_selector": "article.card",
                "fields": [
                    {"name": "headline", "selector": "h2", "type": "text"},
                    {"name": "alt_text", "selector": "img",
                     "type": "attribute", "attribute": "alt"},
                    {"name": "source_text", "selector": "p", "type": "text"},
                ]}
        out = host.run_isolated_selectors(page, spec)
        record = out["records"][0]
        self.assertEqual(record["headline"], "Café résumé")
        self.assertEqual(record["alt_text"], "Crème brûlée")
        self.assertIn("never-execute", record["source_text"])
        self.assertNotIn("&lt;", record["source_text"])
        self.assertFalse(out["authorization_verified"])
        self.assertFalse(out["truncated"])

    def test_concurrent_worker_requests_respect_capacity(self):
        # Real simultaneous subprocess attempts must enforce the two-worker
        # gate even when no task goes through the ordinary bridge cache.
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp, "slow_worker.py")
            script.write_text("import time\ntime.sleep(0.65)\n", encoding="utf-8")
            barrier = Barrier(5)
            started = []
            real_popen = host.subprocess.Popen
            def recorded_spawn(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                started.append(process.pid)
                return process
            def run(_):
                barrier.wait(timeout=5)
                try:
                    host.run_isolated_selectors("public", schema())
                except host.StructuredWorkerUnavailable as exc:
                    return str(exc)
                return "unexpected-success"
            with (patch.object(host, "_WORKER_FILE", script),
                  patch.object(host.subprocess, "Popen",
                               side_effect=recorded_spawn),
                  ThreadPoolExecutor(max_workers=5) as executor):
                results = list(executor.map(run, range(5)))
        # The design guarantees exactly two concurrent child slots.
        self.assertEqual(len(started), 2)
        self.assertEqual(results.count("Selector worker capacity exhausted"), 3)
        self.assertNotIn("unexpected-success", results)

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
        self.assertTrue(result["file_open_denied"])
        self.assertEqual(result["cpu_soft_seconds"], 2)
        self.assertLessEqual(result["max_address_space"], 384 * 1024 * 1024)
        self.assertLessEqual(result["max_output_file"], 64 * 1024)
        self.assertNotIn("url", result)

    def test_child_cannot_open_or_modify_existing_host_file(self):
        """Test with a disposable fixture, never a real operator file."""
        with tempfile.TemporaryDirectory() as directory:
            victim = Path(directory, "guarded-fixture")
            victim.write_bytes(b"unchanged")
            victim.chmod(0o600)
            before = victim.stat()
            script = """
import errno, json, os, sys
sys.path.insert(0, sys.argv[1])
import structured_selector_worker as worker
worker._lock_down()
path = sys.argv[2]
attempts = {
    "read": lambda: os.open(path, os.O_RDONLY),
    "truncate": lambda: os.truncate(path, 0),
    "chmod": lambda: os.chmod(path, 0),
    "utime": lambda: os.utime(path, ns=(0, 0)),
}
denied = {}
for name, action in attempts.items():
    try:
        result = action()
    except OSError as exc:
        denied[name] = exc.errno == errno.EPERM
    else:
        if name == "read":
            os.close(result)
        denied[name] = False
os.write(1, json.dumps(denied, separators=(",", ":")).encode())
"""
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script,
                 str(host._WORKER_FILE.parent), str(victim)],
                capture_output=True, text=True, cwd="/",
                env={"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                timeout=6,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "read": True, "truncate": True,
                "chmod": True, "utime": True,
            })
            after = victim.stat()
            self.assertEqual(victim.read_bytes(), b"unchanged")
            self.assertEqual(after.st_mode, before.st_mode)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

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
