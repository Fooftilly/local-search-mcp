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
