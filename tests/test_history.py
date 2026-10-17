import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from history_log import append_history, history_path, safe_url

class HistoryTests(unittest.TestCase):
    def test_sanitized_url(self):
        self.assertEqual(safe_url('https://user:password@example.com/post?key=abc#secret'), 'https://example.com/post')

    def test_private_history_file_and_rotation(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XDG_STATE_HOME":d, "LOCAL_SEARCH_MCP_HISTORY_MAX_BYTES":"64000"}):
            for i in range(40):
                append_history({'kind':'fetch','url':'https://example.com/','large':'x'*1900,'n':i})
            self.assertTrue(history_path().exists())
            self.assertTrue(Path(str(history_path())+'.1').exists())
            self.assertEqual(history_path().stat().st_mode & 0o777,0o600)
            self.assertEqual(history_path().parent.stat().st_mode & 0o777,0o700)

if __name__=='__main__': unittest.main()
