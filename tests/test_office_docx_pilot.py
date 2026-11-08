"""No-network/offline DOCX worker contract tests (no MarkItDown installed).

Synthetic ZIP entries test the process envelope, NOT Office conversion
fidelity. A real public DOCX corpus must be tested separately on the host.
"""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import zipfile

import office_docx_pilot as pilot
import office_docx_worker as worker


def synthetic_docx() -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in ("[Content_Types].xml", "_rels/.rels",
                     "word/document.xml"):
            z.writestr(name, b"test-content")
    return stream.getvalue()


class OfflineDocxPilotTests(unittest.TestCase):
    def test_invalid_archive_rejected_before_process_creation(self):
        with patch.object(pilot.subprocess, "Popen",
                          side_effect=AssertionError("Must not spawn")):
            with self.assertRaisesRegex(pilot.DocxPilotUnavailable, "metadata"):
                pilot.run_offline_docx_bytes(b"not-a-docx")

    def test_true_kernel_isolation_denies_network_and_file_open(self):
        if sys.platform != "linux":
            self.skipTest("Linux seccomp isolation only")
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(pilot._WORKER_FILE),
             "--check-isolation"],
            capture_output=True, text=True, cwd="/",
            env={"PYTHONDONTWRITEBYTECODE": "1"}, timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        answer = json.loads(result.stdout)
        self.assertTrue(answer["network_denied"])
        self.assertTrue(answer["file_open_denied"])
        self.assertEqual(answer["cpu_soft_seconds"], worker.CPU_SECONDS)
        self.assertLessEqual(answer["max_address_space"], worker.MAX_AS_BYTES)
        self.assertLessEqual(answer["max_output_file"], worker.MAX_OUTPUT_BYTES)

    def test_only_offline_bounded_worker_envelope_is_accepted(self):
        # This fake worker emits a known envelope and does not parse DOCX.
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "fixed_result.py")
            script.write_text(
                "import json,sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.write(json.dumps({'markdown':'text',"
                "'truncated':False}))\n")
            with patch.object(pilot, "_WORKER_FILE", script):
                result = pilot.run_offline_docx_bytes(synthetic_docx())
        self.assertEqual(result, {
            "markdown": "text", "truncated": False,
            "authorization_verified": False,
        })
        self.assertNotIn("url", result)
        self.assertNotIn("document_id", result)

    def test_failing_worker_never_exposes_secret_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "bad.py")
            script.write_text(
                "import sys\n"
                "sys.stderr.write('SENSITIVE_DOCX_CONTENT')\n"
                "raise SystemExit(3)\n")
            with patch.object(pilot, "_WORKER_FILE", script):
                with self.assertRaises(pilot.DocxPilotUnavailable) as raised:
                    pilot.run_offline_docx_bytes(synthetic_docx())
        self.assertNotIn("SENSITIVE_DOCX_CONTENT", str(raised.exception))

    def test_worker_stdout_must_be_small_and_parseable(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "large.py")
            script.write_text("import os\nos.write(1,b'x'*70000)\n")
            with patch.object(pilot, "_WORKER_FILE", script):
                with self.assertRaisesRegex(pilot.DocxPilotUnavailable, "oversized"):
                    pilot.run_offline_docx_bytes(synthetic_docx())
            script.write_text("print('not-json')\n")
            with patch.object(pilot, "_WORKER_FILE", script):
                with self.assertRaisesRegex(pilot.DocxPilotUnavailable, "malformed"):
                    pilot.run_offline_docx_bytes(synthetic_docx())

    def test_timeout_kills_worker_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "sleep.py")
            script.write_text("import time\ntime.sleep(60)\n")
            with (patch.object(pilot, "_WORKER_FILE", script),
                  patch.object(pilot, "WALL_SECONDS", 0.2)):
                started = time.monotonic()
                with self.assertRaisesRegex(pilot.DocxPilotUnavailable, "wall time"):
                    pilot.run_offline_docx_bytes(synthetic_docx())
                self.assertLess(time.monotonic() - started, 4)

    def test_only_docx_converter_registered_when_optional_package_present(self):
        class FakeMarkItDown:
            def __init__(self, *, enable_builtins, enable_plugins):
                self.options = (enable_builtins, enable_plugins)
                self.registered = []
            def register_converter(self, instance):
                self.registered.append(instance)

        class FakeDocxConverter:
            pass

        fake_package = types.ModuleType("markitdown")
        fake_package.MarkItDown = FakeMarkItDown
        fake_package.StreamInfo = object
        fake_converters = types.ModuleType("markitdown.converters")
        fake_converters.DocxConverter = FakeDocxConverter
        with (patch("importlib.metadata.version", return_value="0.1.9"),
              patch.dict(sys.modules, {"markitdown": fake_package,
                                       "markitdown.converters": fake_converters})):
            converter, info = worker._converter()
        self.assertEqual(converter.options, (False, False))
        self.assertEqual(len(converter.registered), 1)
        self.assertIsInstance(converter.registered[0], FakeDocxConverter)
        self.assertIs(info, object)

    def test_wrong_optional_version_rejected_before_converter_import(self):
        with patch("importlib.metadata.version", return_value="9.9.9"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported"):
                worker._converter()


if __name__ == "__main__":
    unittest.main()
