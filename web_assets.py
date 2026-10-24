"""Fixed first-party browser assets; paths are not derived from requests.

The application intentionally does not serve arbitrary files from a directory.
"""
from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "web" / "static"

STYLESHEET = (_ASSETS / "dashboard.css").read_text(encoding="utf-8")
LIVE_SCRIPT = (_ASSETS / "dashboard-live.js").read_text(encoding="utf-8")
