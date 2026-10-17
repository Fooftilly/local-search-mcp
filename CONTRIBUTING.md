# Contributing

- Open an issue before large feature work. No automatic merging.
- Keep MCP tools read-only and retain an explicit local-only approval boundary.
- Add regression tests for security-sensitive changes.
- Never commit `.env`, runtime tokens, search history, local SQLite state or downloaded binaries.
- Run `python -m unittest discover -s tests -v` and `python -m compileall -q .` before proposing a PR.
- Document changes affecting domain permissions, egress safety and setup behavior.
