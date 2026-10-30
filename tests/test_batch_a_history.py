"""Page history export must preserve audited action labels without bearer data."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

import history_cli


class SectionHistoryTests(unittest.TestCase):
    def test_safe_section_action_survives_json_and_csv_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in [
                {"kind": "page", "action": "section", "time_utc": "2026-10-30T22:00:00Z",
                 "status": "success", "document_id": "SECRET_HANDLE",
                 "text": "SECRET_PAGE", "page_count": 4},
                {"kind": "page", "action": "find", "status": "success",
                 "document_id": "ANOTHER_SECRET"},
                {"kind": "page", "action": "=HYPERLINK('bad')", "status": "success",
                 "document_id": "SECRET"},
            ]) + "\n", encoding="utf-8")
            parsed = list(history_cli.events(path))
            self.assertEqual(parsed[0]["action"], "section")
            self.assertEqual(parsed[1]["action"], "find")
            self.assertNotIn("action", parsed[2])
            self.assertNotIn("SECRET", str(parsed))
            for format_flag in ("--json", "--csv"):
                with (patch.object(history_cli, "history_path", return_value=path),
                      patch.object(sys, "argv", ["history_cli.py", "--kind", "page",
                                                 format_flag])):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(history_cli.main(), 0)
                    exported = stdout.getvalue()
                    self.assertIn("section", exported)
                    self.assertNotIn("SECRET", exported)
                    self.assertNotIn("HYPERLINK", exported)

    def test_terminal_page_kind_distinguishes_section_from_page(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.jsonl"
            path.write_text(json.dumps({
                "kind": "page", "action": "section",
                "status": "success", "time_utc": "2026-10-30T22:00:00Z",
                "document_id": "NOT_FOR_TERMINAL", "text": "SECRET_SNIPPET",
            }) + "\n", encoding="utf-8")
            with (patch.object(history_cli, "history_path", return_value=path),
                  patch.object(sys, "argv", ["history_cli.py", "--kind", "page"])):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(history_cli.main(), 0)
                self.assertIn("section of temporary document", stdout.getvalue())
                self.assertNotIn("SECRET", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
