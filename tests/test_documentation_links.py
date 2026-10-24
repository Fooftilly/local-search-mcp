"""Documentation links must keep working when stable guides move into docs/."""
from pathlib import Path
import re
import unittest
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (ROOT / "README.md", ROOT / "SECURITY.md",
           *(ROOT / "docs").glob("*.md"))


class DocumentationLinksTests(unittest.TestCase):
    def test_relative_markdown_links_resolve_within_repository(self):
        for file in SOURCES:
            with self.subTest(file=file.name):
                self.assertTrue(file.is_file())
                for match in re.finditer(r"\]\(([^)]+)\)", file.read_text(encoding="utf-8")):
                    link = match.group(1).strip()
                    if (not link or link.startswith(("#", "https://", "http://", "mailto:"))):
                        continue
                    path = unquote(link.split("#", 1)[0].split("?", 1)[0])
                    dest = (file.parent / path).resolve()
                    self.assertTrue(dest.is_relative_to(ROOT), f"{file}: unsafe local link")
                    self.assertTrue(dest.is_file(), f"{file}: broken link {link}")


if __name__ == "__main__":
    unittest.main()
