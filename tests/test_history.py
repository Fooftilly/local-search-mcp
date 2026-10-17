import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from history_log import append_history, history_path, logged_tool, safe_url

class HistoryTests(unittest.TestCase):
    def test_sanitized_url(self):
        self.assertEqual(safe_url('https://user:password@example.com/post?key=abc#secret'), 'https://example.com/post')

    def test_search_audit_records_effective_page_and_safe_filter_metadata(self):
        import json
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
            "XDG_STATE_HOME": d,
            "LOCAL_SEARCH_MCP_HISTORY_FILE": str(Path(d) / "events.jsonl"),
        }):
            @logged_tool("search")
            def fake_search(query, max_results=8, language="", categories="",
                            time_range="", engines="", page=1):
                return [{"title": "Result", "url": "https://example.org/page?token=secret",
                         "engine": "brave.news"}]
            fake_search("q")
            fake_search("q", language="auto", categories="news", time_range="week",
                        engines="brave.news,crates.io", page=3)
            events = [json.loads(row) for row in history_path().read_text().splitlines()]
        self.assertEqual([e["page"] for e in events], [1, 3])
        self.assertEqual(events[1]["language"], "auto")
        self.assertEqual(events[1]["categories"], "news")
        self.assertEqual(events[1]["time_range"], "week")
        self.assertEqual(events[1]["engines"], "brave.news,crates.io")
        self.assertNotIn("engines", events[0])
        self.assertNotIn("token=secret", str(events))

    def test_private_history_file_and_rotation(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XDG_STATE_HOME":d, "LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES":"64000"}):
            for i in range(40):
                append_history({'kind':'fetch','url':'https://example.com/','large':'x'*1900,'n':i})
            self.assertTrue(history_path().exists())
            self.assertTrue(Path(str(history_path())+'.1').exists())
            self.assertEqual(history_path().stat().st_mode & 0o777,0o600)
            self.assertEqual(history_path().parent.stat().st_mode & 0o777,0o700)

if __name__=='__main__': unittest.main()
