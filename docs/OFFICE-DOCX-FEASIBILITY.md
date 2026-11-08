# Issue #304 — DOCX conversion feasibility (offline admission only)

**Status:** research/preflight, **not a DOCX fetch or conversion feature**.
No additional MCP tools, document downloads, Docker images, authorization
changes or MarkItDown dependencies are installed by this change. The PDF
acceptance in #141 is a **separate, explicitly deferred** workflow.

## Reviewed upstream contract (2026-11-08)

- The newest published stable Microsoft MarkItDown release inspected was
  [v0.1.9 (2026-10-21)](https://github.com/microsoft/markitdown/releases/tag/v0.1.9);
  use this **tag** to recheck compatibility rather than relying on `main`.
- [`MarkItDown.convert_stream`](https://github.com/microsoft/markitdown/blob/v0.1.9/packages/markitdown/src/markitdown/_markitdown.py)
  accepts a seekable binary stream with typed `StreamInfo`. Its permissive
  `convert`/`convert_uri` alternatives can access local paths or HTTP
  resources, so **never pass user URLs/paths directly**. A nonseekable stream
  is copied into an unbounded `BytesIO` inside the upstream method.
- MarkItDown's [DOCX converter](https://github.com/microsoft/markitdown/blob/v0.1.9/packages/markitdown/src/markitdown/converters/_docx_converter.py)
  uses Mammoth and HTML-to-Markdown conversion. The v0.1.9 `docx`
  dependency extra requires `mammoth>=1.12.2,<2` and `lxml`, not the
  `[all]` extra. It is appropriate to **evaluate** after isolation, not
  safe to activate on arbitrary network content merely because it accepts
  streams.
- Critically, its [DOCX preprocessor](https://github.com/microsoft/markitdown/blob/v0.1.9/packages/markitdown/src/markitdown/converter_utils/docx/pre_process.py)
  executes `files = {name: zip_input.read(name) for name in zip_input.namelist()}`.
  It decompresses **every** ZIP member into memory before conversion.
  Rejecting an input by compressed HTTP body size alone cannot bound RAM,
  CPU or ZIP expansion. This is an upstream behavior observed in source, not
  an exploit or a tested live attack. The DOCX converter subsequently
  repacks the archive and parses XML/HTML, adding allocation overhead.
- [MarkItDown's security note](https://github.com/microsoft/markitdown#security-considerations)
  says conversion performs I/O with the privileges of its process.
  Even `convert_stream` must be run in an independently **network-denied,
  low-privilege, killable worker** with no local file access beyond its
  already authorized bytes. Do not follow external relationships/images
  or attach any LLM/cloud client.

## Implemented: bounded ZIP metadata admission

`office_docx_admission.inspect_docx_container(payload)` accepts an
**in-memory, already-bounded bytes object**. It does **not** read a file,
fetch a URL, decompress a member or inspect XML. It examines the ZIP
central directory only and returns redacted aggregate counts and claimed
sizes. The deliberately conservative, tunable *study* ceilings are:

| Limit | Pilot value |
| --- | ---: |
| Total compressed DOCX bytes | 4 MiB |
| ZIP central-directory entries | 128 |
| ZIP central-directory bytes (pre-parse) | 96 KiB |
| Maximum claimed uncompressed member | 4 MiB |
| Maximum claimed cumulative expansion | 12 MiB |
| Claimed per-member expansion ratio | 100:1 |
| Member filename UTF-8 bytes | 180 |

It rejects missing/empty minimal DOCX parts, duplicate or
casefold-colliding names, path traversal and noncanonical separators,
encrypted members, special-file/symlink members, unsupported compression,
oversized or split central directories, ZIP64 end-record overrides,
and forged central-directory entry counts **before** invoking
`zipfile.ZipFile`; macro/ActiveX/embedded files and nested archives.
Malformed ZIP filename encodings and unsupported ZIP extraction versions are
normalized to the single `DocxAdmissionRejected` error contract. This deliberately rejects
some otherwise-valid Office documents. It never decodes or extracts a member,
and *does not* claim to detect all malicious OOXML relationships or scripts.

**Important:** ZIP central-directory sizes can lie. This is an early filter,
**not** a sanitizer or a substitute for a worker's CPU, memory, decompressed
byte-count, file-descriptor, network, process and wall-time limits. The
function does not validate CRCs, document structure, XML external entities,
logical page count, accessibility or output quality. Keep it offline until
the guarded download and isolated converter are independently accepted.

## Experimental DOCX-only offline worker (follow-up research)

A separate, **not integrated** pilot is available in
`office_docx_pilot.run_offline_docx_bytes(bytes)` and
`office_docx_worker.py`. This is a developer/operator research interface,
**not** a new MCP tool, public URL downloader, installed service, standalone
CLI or production converter.

The optional dependency is pinned to `markitdown[docx]==0.1.9` in
`requirements-docx-pilot.txt` and is **never installed by default**.
Evaluate it only in a separate reviewed Python virtual environment. It
registers exactly `DocxConverter` with built-ins and plugins disabled,
then calls the upstream `convert_stream(BytesIO(...))` with a fixed
`.docx` MIME/extension. No URLs, local paths, LLMs, arbitrary plugins,
archive recursion or OCR are accepted as input.

The local-only host validates DOCX ZIP metadata **before** spawning, allows
one worker at a time, uses `python -I -B` with a minimal environment and
closed file descriptors, passes only the bounded DOCX bytes through stdin,
and stores output in an anonymous Linux `memfd` (not a named disk file).
On timeout it SIGKILLs the worker's isolated process group. It never
provides a URL, document handle, authorization proof or MCP result.

The child sets finite address space (**1 GiB**), CPU (**8 s**),
output file size (**48 KiB**) and file-descriptor caps. Seccomp denies
networking, fork, executable loading and ptrace **before** optional
MarkItDown initialization; the trusted native initializer may start
CPU threads using clone in that pre-input phase. A second filter denies
clone, new file opens and filesystem mutations **before** reading
untrusted document data. File restrictions explicitly include path-based
`truncate`, permission/ownership/timestamp updates and extended-attribute
changes, which need not open a new file descriptor. A Linux regression
exercises `truncate`, `chmod` and `utime` against an existing fixture,
checks their kernel denial, then verifies the bytes and metadata are intact.
The second filter requests libseccomp
`SCMP_FLTATR_CTL_TSYNC` synchronization: if an already-running native
thread cannot receive the same restrictions, the worker fails closed.
An offline regression starts a thread before the final filter and verifies
that the thread also loses file-open access. It fails closed if the
installed library requires new threads or filesystem resources during
actual conversion.
Markdown is capped at 32 KiB, returned solely as **unauthorized research
text**, and must not be released through a remote MCP endpoint.

Offline tests validate the real seccomp socket/open denial, process group
timeout, output limits and redaction, without installing MarkItDown.
They use fake fixed-output children and synthetic ZIP fixtures and
**do not prove that any genuine DOCX converts successfully**.
The pinned MarkItDown package may initialize resource files, native
libraries or background threads; compatibility after the restrictions
and actual peak memory still require attended testing. This is a local
containment feasibility experiment, **not** a complete guarantee against
a malicious native dependency or every kernel syscall attack.
There is no automatic package installation or live conversion performed
by this PR.

## Opt-in genuine OOXML conversion acceptance harness

`scripts/docx_pilot_offline_acceptance.py` generates a deterministic
five-part, internally linked DOCX (heading, Markdown-sized table and
Unicode text) entirely in memory. It is a structurally valid synthetic
OOXML fixture, **not** an independently approved publicly hosted file.
The harness calls the existing isolated worker with the already-admitted
bytes; it does not take any URL or path arguments, persist a document, grant
access, start a service or change Docker settings.

The ordinary repository test suite covers the generated ZIP/XML structure,
fidelity scoring, redacted reports and the explicit `--run` opt-in, but
**does not install or execute the optional converter**. To assess genuine
MarkItDown compatibility, use a separate, operator-reviewed Python
environment on Linux:

```bash
cd ~/GitHub/local-search-mcp
python3 -m venv /tmp/local-search-docx-acceptance
/tmp/local-search-docx-acceptance/bin/python -m pip install -r requirements-docx-pilot.txt
/tmp/local-search-docx-acceptance/bin/python -m scripts.docx_pilot_offline_acceptance --run
```

This installation may contact the package repository; **the conversion
harness itself has no network/download operation**. Do not install these
optional dependencies in the running bridge environment. The harness
returns only `passed`, elapsed time, archive/output byte counts and
booleans for heading, table, Unicode and truncation; it never prints
document text. A nonzero exit or `offline_conversion_failed` means the
optional dependency, OS containment, input or conversion did not meet the
gate; do not weaken security limits just to force a pass.

This local fixture is a **single compatibility smoke test**, not the issue's
three independently approved public layout samples. Even a passing
result neither validates hostile OOXML nor supplies source/final-URL
authorization, peer-IP verification, cancellation under an active network
download or production release permission. The remaining acceptance work
below is unchanged.

## Remaining gates before any hosted DOCX tool

1. **Download trust boundary.** Build a separate authorization-preserving,
   byte-stream transport for approved public `.docx` only. Enforce source
   and every redirect/final URL, DNS and peer-IP verification, live
   permissions/one-use proof, MIME/content sniffing, Content-Length and actual
   streamed bytes, deadline, and revocation before releasing output. A normal
   `web_fetch` Markdown response is **not** an authorized binary download.
2. **Independent killable converter.** Use a separately sandboxed process
   (no network, no inherited credentials, no filesystem paths, no child
   spawning), finite CPU/RAM/wall clock, bounded input/expanded data, and
   strict stdout size. Call only `convert_stream` with an already validated,
   seekable `BytesIO` and fixed `StreamInfo(extension=".docx")`; never
   `convert` or `convert_uri`. Do not install `[all]`, OCR, plugins, cloud
   extraction or image LLMs. Test that a timed-out parser actually dies.
3. **Format and correctness corpus.** Measure headings, paragraphs, tables,
   Unicode and hyperlinks in at least three independently approved public
   DOCX layouts with operator-annotated expected output. Compare with
   publisher HTML/TXT when available and measure wall time, peak RAM and
   output bytes. Test malformed ZIP/OOXML, extreme compression, duplicates,
   encrypted/embedded files, malicious relationships, bad redirects, DNS
   rebinding, permission expiry and concurrent cancellation. Synthetic ZIP
   metadata fixtures in this PR are **not** valid DOCX documents or proof
   of conversion fidelity.
4. **Release semantics.** If accepted, use an explicitly opt-in tool/format
   with redacted statuses, source/final provenance, bounded Markdown and
   existing authorization-aware evidence paging. Do not broaden the existing
   12-tool interface or turn on an experimental feature based on this
   feasibility filter alone.

**Decision:** Continue with a separately reviewed, killable, offline
conversion spike only after the byte-download and sandbox contracts are
specified. **No-go for public DOCX conversion at this stage.** PPTX/XLSX
remain out of scope until the DOCX pilot is accepted.
