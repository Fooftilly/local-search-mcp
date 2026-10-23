"""Untrusted history is inert in terminals and spreadsheet exports."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import history_cli


class HistoryCliOutputSafetyTests(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.path = Path(state.name) / "events.jsonl"
        env = patch.dict(os.environ, {"LOCAL_SEARCH_MCP_HISTORY_FILE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def record(self, **fields):
        event = {"time_utc": "2026-10-22T12:00:00Z",
                 "kind": "search", "status": "success", **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def run_cli(self, *args):
        stdout = io.StringIO()
        with (patch.object(sys, "argv", ["history_cli.py", *args]),
              contextlib.redirect_stdout(stdout)):
            self.assertEqual(history_cli.main(), 0)
        return stdout.getvalue()

    def test_text_mode_escapes_terminal_and_direction_controls(self):
        self.record(query="q\x1b[2K\x1b]52;c;QUJD\x07\r\n\u0085\u202eEND")
        output = self.run_cli()
        for unsafe in ("\x1b", "\x07", "\r", "\x85", "\u202e"):
            self.assertNotIn(unsafe, output)
        for escaped in (r"\x1b", r"\x07", r"\x0d", r"\x0a",
                        r"\x85", r"\u202e"):
            self.assertIn(escaped, output)
        self.assertEqual(output.count("\n"), 2)

    def test_csv_mode_neutralizes_formula_prefixes(self):
        malicious = [
            '=HYPERLINK("https://example.org/", "open")',
            "+SUM(1,1)", "-2+3", "@SUM(1,2)",
            "\t=1+1", "\r=1+1", "\n=1+1", "  =1+1",
        ]
        for query in malicious:
            self.record(query=query)
        self.record(query="ordinary search")
        rows = list(csv.DictReader(io.StringIO(self.run_cli("--csv"))))
        self.assertEqual(len(rows), len(malicious) + 1)
        self.assertEqual([row["query"] for row in rows[:-1]],
                         ["'" + query for query in malicious])
        self.assertEqual(rows[-1]["query"], "ordinary search")

    def test_json_mode_escapes_c1_and_format_controls(self):
        self.record(query="safe\u0085\u202e")
        output = self.run_cli("--json")
        self.assertNotIn("\u0085", output)
        self.assertNotIn("\u202e", output)
        self.assertIn(r"\u0085", output)
        self.assertIn(r"\u202e", output)
        self.assertEqual(json.loads(output)["query"], "safe\u0085\u202e")

    def test_stats_mode_escapes_untrusted_event_fields(self):
        self.record(kind="search\x1b[2J", status="success\u202e")
        output = self.run_cli("--stats")
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\u202e", output)
        self.assertIn(r"\x1b", output)
        self.assertIn(r"\u202e", output)


if __name__ == "__main__":
    unittest.main()
