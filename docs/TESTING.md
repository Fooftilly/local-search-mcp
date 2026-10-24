# MCP regression-test strategy

This slice of [issue #4](https://github.com/Fooftilly/local-search-mcp/issues/4) adds protocol-level tests without contacting external websites, user credentials, SearXNG or Crawl4AI. CI runs `python -m unittest discover -s tests -v` against supported Python versions.

## What is exercised

- FastMCP's real in-process `Client(mcp)` dispatch and the registered `web_search`/`web_fetch` tool names.
- A previously unseen domain returns structured `approval_required` and creates a pending database request, without connecting to a crawler or granting permission.
- Invalid and special-use URLs are rejected before backend HTTP traffic.
- Successful search and approved fetch results flow through real tool dispatch with a deterministic `httpx.MockTransport` standing in for the two local backend APIs.
- SQLite policy and audit/history use a temporary `XDG_STATE_HOME`; no user data, live API tokens, or personal paths are involved.

## What this does not prove

In-memory protocol tests **do not run the browser**, exercise network egress/redirect DNS rebinding inside Chromium, or prove remote OpenAI tunnel connectivity. The original roadmap issues [#2](https://github.com/Fooftilly/local-search-mcp/issues/2) and [#3](https://github.com/Fooftilly/local-search-mcp/issues/3) are already closed; their closed status does not establish that a live operator deployment has passed these tests. Follow [EGRESS-ISOLATION.md](EGRESS-ISOLATION.md) for the Docker-host probes and [SYSTEMD.md](SYSTEMD.md) for systemd startup/recovery procedures.

## Real loopback dashboard and failure regressions (second slice)

`tests/test_http_security.py` launches a disposable `ThreadingHTTPServer` bound to `127.0.0.1` on an OS-selected port. It checks the entire pending → allow → deny → rename → delete lifecycle and SQLite decision audit via real HTTP requests, hostile `Host`, `Origin`, `Referer`, `Sec-Fetch-Site`, and invalid CSRF tokens. It checks the live local `/healthz` route, blocked form domains, DNS lookup failures, and mixed public/private A/AAAA answers. Private and special-use URL input must not leak sensitive URL query parameters in error responses.

`tests/test_failure_paths.py` mocks only external subprocess calls and upstream HTTP services to cover missing prepared runtime files, unsafe symlinked tunnel executables, unhealthy/obsolete Crawl4AI instances, and malformed upstream health JSON. All state/history overrides point to temporary files; integration tests never use a developer's real config or call the real Internet.

The CI matrix covers Python 3.11/3.13/3.14 and includes a targeted Ruff gate for undefined names and a few unsafe code-execution patterns (`F821,F822,F823,S307,S602`). This narrow gate is **not** a comprehensive security audit.

## What still needs verification

These tests do **not** run a real browser or untrusted Chromium subresources, verify private-network egress isolation, simulate real DNS rebinding during a page load, test the remote tunnel end to end, or prove reboot-time user-systemd recovery. Disposable real HTTP MCP transport and live operator-driven Docker isolation validation remain future work. Roadmap issues [#2](https://github.com/Fooftilly/local-search-mcp/issues/2), [#3](https://github.com/Fooftilly/local-search-mcp/issues/3) and [#4](https://github.com/Fooftilly/local-search-mcp/issues/4) have **already been closed**. This document does not reopen them or imply that their live-host acceptance tests were run. Any still-required end-to-end or operator validation should be tracked separately before claiming deployment-level assurance.
