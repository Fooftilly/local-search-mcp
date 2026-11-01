# Contributing

- Open an issue before large feature work. No automatic merging.
- Keep MCP tools read-only and retain an explicit local-only approval boundary.
- Add regression tests for security-sensitive changes.
- Never commit `.env`, runtime tokens, search history, local SQLite state or downloaded binaries.
- Run `python -m unittest discover -s tests -v` and `python -m compileall -q .` before proposing a PR.
- Document changes affecting domain permissions, egress safety and setup behavior.
- Keep web markup, styles and browser scripts in `web/templates/*.html`, `web/static/*.css` and `web/static/*.js`. Do not construct HTML, CSS or JavaScript as string literals in Python or other non-web implementation code. Supply validated data and escaped template variables instead; preserve CSP, same-origin protections, and server-rendered/no-JS fallbacks. Existing legacy `admin_ui.py` inline fragments are tracked for complete migration in #259; do not add new ones.
