"""Execute the embedded dashboard script against a small mocked browser.

Node is optional for manual Python-only installations; GitHub-hosted
Ubuntu CI runners provide it. No external npm dependencies are required.
"""
from pathlib import Path
import shutil
import subprocess
import unittest

from dashboard_live_script import LIVE_SCRIPT


class DashboardLiveBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_live_polling_notifications_and_focus(self):
        root = Path(__file__).resolve().parent
        result = subprocess.run(
            ["node", str(root / "dashboard_live_runtime.js")],
            input=LIVE_SCRIPT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        self.assertIn("dashboard live browser state tests passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
