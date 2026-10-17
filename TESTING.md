# MCP regression-test strategy

This slice of [issue #4](https://github.com/Fooftilly/local-search-mcp/issues/4) adds protocol-level tests without contacting external websites, user credentials, SearXNG or Crawl4AI. CI runs `python -m unittest discover -s tests -v` against supported Python versions.

## What is exercised

- FastMCP's real in-process `Client(mcp)` dispatch and the registered `web_search`/`web_fetch` tool names.
- A previously unseen domain returns structured `approval_required` and creates a pending database request, without connecting to a crawler or granting permission.
- Invalid and special-use URLs are rejected before backend HTTP traffic.
- Successful search and approved fetch results flow through real tool dispatch with a deterministic `httpx.MockTransport` standing in for the two local backend APIs.
- SQLite policy and audit/history use a temporary `XDG_STATE_HOME`; no user data, live API tokens, or personal paths are involved.

## What this does not prove

In-memory protocol tests **do not run the browser**, exercise network egress/redirect DNS rebinding inside Chromium, or prove remote OpenAI tunnel connectivity. Issue #2 still requires real Docker host and browser security checks; issue #3 still requires real systemd startup/recovery testing.

Future slices of #4 should cover a disposable real HTTP MCP endpoint, full dashboard approval lifecycle across requests, host/Origin/Referer/CSRF variations, malformed JSON and startup failures, and a targeted lint/static-security gate. Do not merge this documentation as evidence of those unfinished requirements.
