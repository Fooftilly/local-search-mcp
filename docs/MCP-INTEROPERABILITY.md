# MCP interoperability acceptance (#215)

**Scope:** this is a non-mutating test/audit improvement, not a protocol migration or a claim that every client works. Current default-branch tools use FastMCP annotations, typed JSON input schemas and documented result shapes. CI's `tests/test_mcp_contract.py` already tests real in-process FastMCP calls; `tests/test_mcp_interoperability.py` adds an explicit backwards-compatibility inventory and client-facing smoke matrix.

## What can be proved in repository CI

Run from the project's virtual environment:

```sh
./.venv/bin/python -m unittest -v tests.test_mcp_interoperability
```

The test uses an **in-process FastMCP client with mocked HTTP providers**, temporary local state and no Docker, real provider requests or domain approvals. It checks baseline tool discovery (allowing future additive tools), required input properties and defaults, read-only metadata, the default four-field search result shape, dictionary-shaped capability inventory with disabled engines excluded, validation errors before network contact and blocked private fetches.

These tests verify the installed FastMCP *Python library's* behavior under the test runtime; they do **not** prove remote ChatGPT, Cursor, Hermes, another SDK or the actual HTTP transport. A successful `Client.call_tool` and `result.data` is not evidence that every optional MCP `outputSchema` or `structuredContent` is compatible across different client SDK versions. Any transport/schema change must be explicitly reviewed; do not add `outputSchema` annotations on speculative assumptions.

## Attended local FastMCP CLI check (no crawler wake on capability read)

With the reviewed checkout deployed and the MCP bridge already listening on its **loopback** address:

```sh
./.venv/bin/fastmcp list http://127.0.0.1:8790/mcp --auth none
./.venv/bin/fastmcp call http://127.0.0.1:8790/mcp web_search_capabilities --auth none
```

Record discovered tool names and input argument names; do not record private tokens. `web_search_capabilities` is strictly read-only and will return `status=unavailable` while SearXNG sleeps; **that is an allowed outcome**, not a tool discovery failure. When SearXNG is already running, compare the enabled count/category inventory with observed local `/config`. Do not enable engines, start Docker or perform a search solely to make this test green.

## ChatGPT and second-client acceptance (not performed by CI)

Repeat discovery and an explicitly user-authorized bounded public search independently through:

1. Connected ChatGPT, confirming the tool name, argument schema and structured output; this client had previously discovered `web_search_capabilities` with 69 enabled engines and 23 categories on the installed host as of 2026-10-30, **before any new API rollout**.
2. A *second* already-configured, authorized client (such as Cursor or Hermes). Inspect its tool list/cache refresh behavior, exact optional/default arguments, errors and parsed result data. Do not add a new remote tunnel or credentials just for this audit.

For each client record library/client version, transport type, discovery snapshot, `tools/list` result count, whether `tools/call` returns expected `isError`/structured data, and the exact location of any mismatch. Avoid copying raw query history, public crawl content, private URL query strings or credentials into issues.

### Compatibility decision

- [ ] CI baseline tests pass on the deployed Python 3.14 runtime.
- [ ] Local FastMCP CLI discovers the **deployed revision**, including newly merged tools, and invokes read-only capabilities.
- [ ] ChatGPT discovery, optional argument behavior and structured-result decoding pass on that same revision.
- [ ] One separate connected client passes the same criteria or a reproducible mismatch is captured.
- [ ] If an incompatibility is found, isolate a **separate minimal fix PR** preserving the prior tool defaults and Host/Origin/approval boundaries; no "mass" schema redesign.
- [ ] Leave issue #215 open until external-client evidence completes the gate.

This is evidence for schema stability and limited in-process interoperability, not authorization for MCP clients to change domain approvals or modify the local operator host.
