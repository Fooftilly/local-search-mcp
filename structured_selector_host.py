"""Supervisor for a one-shot, CPU-limited and network-denied selector worker.

Only HTML from the existing approved Crawl4AI response is accepted. Never
send URLs, crawler tokens, approval state, or credentials to the child.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading

import structured_schema

MAX_HTML_BYTES = 256 * 1024
MAX_CHILD_RESULT_BYTES = 64 * 1024
WALL_SECONDS = 5
_MAX_WORKERS = threading.BoundedSemaphore(2)
_WORKER_FILE = Path(__file__).resolve().with_name("structured_selector_worker.py")


class StructuredWorkerUnavailable(RuntimeError):
    """Extraction worker could not complete under its isolation constraints."""


def _terminate_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_isolated_selectors(html: object, spec: object) -> dict:
    """Return normalized untrusted rows; does NOT authorize releasing them.

    The parent bridge alone owns all source/final URL and one-use checks.
    """
    canonical = structured_schema.validate_spec(spec)
    if sys.platform != "linux":
        raise StructuredWorkerUnavailable("Linux selector isolation required")
    if type(html) is not str or len(html) > MAX_HTML_BYTES:
        raise StructuredWorkerUnavailable("HTML size is outside the worker budget")
    try:
        body = html.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise StructuredWorkerUnavailable("HTML encoding rejected") from exc
    if not body or len(body) > MAX_HTML_BYTES:
        raise StructuredWorkerUnavailable("HTML size is outside the worker budget")
    payload = json.dumps({"html": html, "spec": canonical},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_HTML_BYTES * 2:
        raise StructuredWorkerUnavailable("Worker input budget exceeded")
    if not _MAX_WORKERS.acquire(blocking=False):
        raise StructuredWorkerUnavailable("Selector worker capacity exhausted")

    try:
        with tempfile.TemporaryFile(mode="w+b") as output:
            try:
                child = subprocess.Popen(
                    [sys.executable, "-I", str(_WORKER_FILE)],
                    stdin=subprocess.PIPE,
                    stdout=output, stderr=subprocess.DEVNULL,
                    cwd="/", env={"PYTHONNOUSERSITE": "1",
                                  "PYTHONDONTWRITEBYTECODE": "1"},
                    start_new_session=True, close_fds=True,
                )
            except OSError as exc:
                raise StructuredWorkerUnavailable("Selector worker could not start") from exc
            try:
                try:
                    child.communicate(input=payload, timeout=WALL_SECONDS)
                except subprocess.TimeoutExpired as exc:
                    raise StructuredWorkerUnavailable("Selector worker exceeded wall time") from exc
            finally:
                _terminate_group(child)
            if child.returncode != 0:
                raise StructuredWorkerUnavailable("Selector worker failed closed")
            if output.tell() > MAX_CHILD_RESULT_BYTES:
                raise StructuredWorkerUnavailable("Selector worker output oversized")
            output.seek(0)
            response = output.read(MAX_CHILD_RESULT_BYTES + 1)

        if not response or len(response) > MAX_CHILD_RESULT_BYTES:
            raise StructuredWorkerUnavailable("Selector worker output oversized")
        try:
            parsed = json.loads(response)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise StructuredWorkerUnavailable("Selector worker returned invalid JSON") from exc
        if (type(parsed) is not dict or set(parsed) != {"rows", "source_capped"}
                or type(parsed["rows"]) is not list
                or type(parsed["source_capped"]) is not bool):
            raise StructuredWorkerUnavailable("Selector worker returned invalid shape")
        try:
            normalized = structured_schema.normalize_extracted_content(
                json.dumps(parsed["rows"], ensure_ascii=False), canonical)
        except structured_schema.StructuredSchemaRejected as exc:
            raise StructuredWorkerUnavailable("Selector worker returned invalid records") from exc
        if parsed["source_capped"]:
            normalized["truncated"] = True
            normalized["input_scan_capped"] = True
        return normalized
    finally:
        _MAX_WORKERS.release()
