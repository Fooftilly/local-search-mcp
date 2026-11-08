"""Offline acceptance harness tests; no optional MarkItDown dependency needed."""
from __future__ import annotations

from io import BytesIO, StringIO
import json
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from scripts import docx_pilot_offline_acceptance as acceptance
from office_docx_admission import inspect_docx_container
from office_docx_pilot import DocxPilotUnavailable


class DocxOfflineAcceptanceTests(unittest.TestCase):
    def test_generated_fixture_is_a_bounded_ooxml_document(self):
        docx = acceptance.sample_docx_bytes()
        report = inspect_docx_container(docx)
        self.assertEqual(report["members"], 5)
        self.assertLess(len(docx), 16_384)
        with zipfile.ZipFile(BytesIO(docx)) as doc:
            self.assertEqual(len(doc.namelist()), 5)
            self.assertTrue(all(not n.startswith("/") for n in doc.namelist()))
            for name in doc.namelist():
                ET.fromstring(doc.read(name))
            body = doc.read("word/document.xml").decode("utf-8")
            relationships = doc.read("word/_rels/document.xml.rels").decode("utf-8")
        for marker in (acceptance.HEADING, acceptance.UNICODE,
                       acceptance.CELL_A, acceptance.CELL_B,
                       acceptance.CELL_C, acceptance.CELL_D):
            self.assertIn(marker, body)
        self.assertIn("relationships/styles", relationships)
        self.assertIn("styles.xml", relationships)

    def test_fixture_bytes_stable_across_zip_clock_boundaries(self):
        # When writestr() receives a plain filename it embeds localtime(),
        # with two-second DOS timestamp precision. The fixture must not.
        with patch.object(zipfile.time, "localtime",
                          return_value=(2020, 1, 1, 0, 0, 0, 0, 1, -1)):
            first = acceptance.sample_docx_bytes()
        with patch.object(zipfile.time, "localtime",
                          return_value=(2026, 11, 8, 20, 30, 16, 6, 312, -1)):
            second = acceptance.sample_docx_bytes()
        self.assertEqual(first, second)
        with zipfile.ZipFile(BytesIO(first)) as doc:
            self.assertEqual(
                {item.date_time for item in doc.infolist()},
                {(1980, 1, 1, 0, 0, 0)},
            )
            self.assertTrue(all(
                item.compress_type == zipfile.ZIP_DEFLATED
                for item in doc.infolist()
            ))

    def test_scoring_requires_heading_table_unicode_and_complete_output(self):
        sample = (
            f"# {acceptance.HEADING}\n\n{acceptance.UNICODE}\n\n"
            f"| {acceptance.CELL_A} | {acceptance.CELL_B} |\n"
            "| --- | --- |\n"
            f"| {acceptance.CELL_C} | {acceptance.CELL_D} |"
        )
        result = acceptance.score_conversion(sample, truncated=False)
        self.assertTrue(result["passed"])
        self.assertEqual(set(result), {
            "heading_markdown", "unicode_preserved", "table_markdown",
            "truncated", "passed",
        })
        for replacement in (
            sample.replace("# ", "", 1),
            sample.replace(acceptance.UNICODE, "ASCII only"),
            sample.replace("|", " "),
            sample.replace(acceptance.CELL_D, "missing"),
        ):
            with self.subTest(result=replacement[:30]):
                self.assertFalse(
                    acceptance.score_conversion(replacement, truncated=False)["passed"]
                )
        self.assertFalse(acceptance.score_conversion(sample, truncated=True)["passed"])
        with self.assertRaises(ValueError):
            acceptance.score_conversion(None, truncated=False)

    def test_table_scoring_requires_adjacent_markdown_structure(self):
        intro = f"# {acceptance.HEADING}\n{acceptance.UNICODE}\n"
        header = f"| {acceptance.CELL_A} | {acceptance.CELL_B} |"
        separator = "| :--- | ---: |"
        data = f"| {acceptance.CELL_C} | {acceptance.CELL_D} |"
        valid = intro + "\n".join((header, separator, data))
        self.assertTrue(acceptance.score_conversion(
            valid, truncated=False)["table_markdown"])
        # GFM tables also permit omitting exterior pipes.
        without_outer = intro + (
            f"{acceptance.CELL_A} | {acceptance.CELL_B}\n"
            "--- | ---\n"
            f"{acceptance.CELL_C} | {acceptance.CELL_D}"
        )
        self.assertTrue(acceptance.score_conversion(
            without_outer, truncated=False)["table_markdown"])
        invalid = {
            "flattened-with-pipe": intro + (
                f"{acceptance.CELL_A} | {acceptance.CELL_B}\n"
                f"{acceptance.CELL_C}\n{acceptance.CELL_D}"
            ),
            "no-delimiter": intro + "\n".join((header, data)),
            "missing-data-row": intro + "\n".join((
                header, separator, acceptance.CELL_C, acceptance.CELL_D)),
            "wrong-column-order": intro + "\n".join((
                header, separator,
                f"| {acceptance.CELL_D} | {acceptance.CELL_C} |")),
            "nonadjacent-row": intro + "\n".join((
                header, separator, "ordinary paragraph", data)),
            "malformed-delimiter": intro + "\n".join((
                header, "| - | -- |", data)),
        }
        for name, markdown in invalid.items():
            with self.subTest(case=name):
                score = acceptance.score_conversion(markdown, truncated=False)
                self.assertFalse(score["table_markdown"])
                self.assertFalse(score["passed"])

    def test_evaluate_passes_only_generated_bytes_to_offline_worker(self):
        markdown = (
            f"# {acceptance.HEADING}\n{acceptance.UNICODE}\n"
            f"| {acceptance.CELL_A} | {acceptance.CELL_B} |\n"
            "| --- | --- |\n"
            f"| {acceptance.CELL_C} | {acceptance.CELL_D} |"
        )
        def fake_worker(value):
            self.assertEqual(value, acceptance.sample_docx_bytes())
            return {
                "markdown": markdown,
                "truncated": False,
                "authorization_verified": False,
            }
        with patch.object(acceptance, "run_offline_docx_bytes",
                          side_effect=fake_worker) as called:
            report = acceptance.evaluate_real_worker()
        called.assert_called_once()
        self.assertTrue(report["passed"])
        self.assertFalse(report["authorization_verified"])
        self.assertIn("elapsed_ms", report)
        self.assertNotIn("markdown", report)
        self.assertNotIn(acceptance.UNICODE, str(report))

    def test_reject_false_authorization_claim(self):
        with patch.object(acceptance, "run_offline_docx_bytes", return_value={
            "markdown": "not authorized",
            "truncated": False,
            "authorization_verified": True,
        }):
            with self.assertRaises(DocxPilotUnavailable):
                acceptance.evaluate_real_worker()

    def test_cli_requires_explicit_run_and_hides_worker_failures(self):
        with self.assertRaises(SystemExit) as error:
            acceptance.main([])
        self.assertEqual(error.exception.code, 2)

        with patch.object(acceptance, "evaluate_real_worker",
                          side_effect=DocxPilotUnavailable(
                              "secret internal path and document text")):
            output = StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(acceptance.main(["--run"]), 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "offline_conversion_failed")
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("internal path", output.getvalue())

    def test_cli_reports_only_summary_metrics(self):
        report = {
            "scenario": "generated_docx_heading_table_unicode",
            "passed": True,
            "authorization_verified": False,
            "archive_bytes": 100,
            "output_bytes": 300,
            "elapsed_ms": 10,
        }
        with patch.object(acceptance, "evaluate_real_worker",
                          return_value=report), patch("sys.stdout", StringIO()) as out:
            self.assertEqual(acceptance.main(["--run"]), 0)
            self.assertEqual(json.loads(out.getvalue()), report)


if __name__ == "__main__":
    unittest.main()
