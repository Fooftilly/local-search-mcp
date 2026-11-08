"""Opt-in, wholly offline DOCX conversion compatibility check for #304.

Generates a deterministic, valid minimal OOXML document entirely in memory.
Only an explicit --run converts it using the optional installed MarkItDown
worker. No URL, arbitrary file path, filesystem write or network request.
Diagnostic JSON contains no extracted document text and makes no claim of
MCP authorization, publisher fidelity or production readiness.

Requires requirements-docx-pilot.txt installed in an isolated environment.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import json
import sys
import time
import zipfile

from office_docx_admission import inspect_docx_container
from office_docx_pilot import DocxPilotUnavailable, run_offline_docx_bytes


HEADING = "Offline DOCX Pilot Heading"
UNICODE = "Č Ć Š Ž Đ č ć š ž đ π Ω"
CELL_A = "PilotColumnOne"
CELL_B = "PilotColumnTwo"
CELL_C = "PilotValueOne"
CELL_D = "PilotValueTwo"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml"
ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml"
ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
Target="word/document.xml"/>
</Relationships>"""
_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
Target="styles.xml"/>
</Relationships>"""
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Normal" w:default="1">
<w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1">
<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:qFormat/></w:style>
</w:styles>"""
_DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
<w:r><w:t>{HEADING}</w:t></w:r></w:p>
<w:p><w:r><w:t>{UNICODE}</w:t></w:r></w:p>
<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="3000"/></w:tblGrid>
<w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>{CELL_A}</w:t></w:r></w:p></w:tc>
<w:tc><w:tcPr/><w:p><w:r><w:t>{CELL_B}</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>{CELL_C}</w:t></w:r></w:p></w:tc>
<w:tc><w:tcPr/><w:p><w:r><w:t>{CELL_D}</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl><w:sectPr/></w:body></w:document>"""


def sample_docx_bytes() -> bytes:
    """Create a fixed five-part DOCX fixture without file or network access."""
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        for name, part in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _ROOT_RELS),
            ("word/document.xml", _DOCUMENT),
            ("word/_rels/document.xml.rels", _DOC_RELS),
            ("word/styles.xml", _STYLES),
        ):
            docx.writestr(name, part.encode("utf-8"))
    payload = stream.getvalue()
    inspect_docx_container(payload)
    return payload


def score_conversion(markdown: object, *, truncated: bool) -> dict[str, object]:
    """Measure a few fidelity markers; do not return any document content."""
    if type(markdown) is not str:
        raise ValueError("Unexpected converter result")
    lines = markdown.splitlines()
    heading = any(
        line.lstrip().startswith("#")
        and HEADING in line
        for line in lines
    )
    unicode_preserved = UNICODE in markdown
    columns = all(text in markdown for text in (CELL_A, CELL_B, CELL_C, CELL_D))
    table = columns and any("|" in line and CELL_A in line for line in lines)
    return {
        "heading_markdown": heading,
        "unicode_preserved": unicode_preserved,
        "table_markdown": table,
        "truncated": truncated,
        "passed": heading and unicode_preserved and table and not truncated,
    }


def evaluate_real_worker() -> dict[str, object]:
    """Run the real optional child. Output is aggregate evidence, never text."""
    document = sample_docx_bytes()
    started = time.monotonic()
    result = run_offline_docx_bytes(document)
    if result.get("authorization_verified") is not False:
        raise DocxPilotUnavailable("Unsafe authorization result")
    verdict = score_conversion(result["markdown"], truncated=result["truncated"])
    return {
        "scenario": "generated_docx_heading_table_unicode",
        "archive_bytes": len(document),
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "output_bytes": len(result["markdown"].encode("utf-8")),
        "authorization_verified": False,
        **verdict,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", required=True,
        help="run local-only DOCX conversion with optional pinned MarkItDown",
    )
    parser.parse_args(argv)
    try:
        report = evaluate_real_worker()
    except (DocxPilotUnavailable, ValueError, OSError):
        # Do not emit exception details, paths, document content or tracebacks.
        print(json.dumps({"scenario": "generated_docx_heading_table_unicode",
                          "passed": False, "status": "offline_conversion_failed"}))
        return 1
    print(json.dumps(report, separators=(",", ":")))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
