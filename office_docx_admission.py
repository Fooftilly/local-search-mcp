"""Metadata-only DOCX archive admission for the #304 feasibility study.

No file paths, URL access, ZIP entry extraction, converters or subprocesses.
This rejects *obvious* oversized/ambiguous archives before a future
isolated worker; it DOES NOT make a DOCX safe to parse. In particular ZIP
metadata can lie about expanded size and CRC, and MarkItDown's DOCX
preprocessor materializes all members. Conversion must still be inside a
separately enforced, network-denied CPU/RAM/wall-time sandbox.
"""
from __future__ import annotations

from io import BytesIO
import stat
import struct
import unicodedata
import zipfile

# Conservative study limits, not yet approved production document limits.
MAX_DOCX_BYTES = 4 * 1024 * 1024
MAX_MEMBERS = 128
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 12 * 1024 * 1024
MAX_MEMBER_RATIO = 100
MAX_NAME_BYTES = 180
MAX_DIRECTORY_BYTES = 96 * 1024
_REQUIRED = frozenset({"[Content_Types].xml", "_rels/.rels",
                       "word/document.xml"})
_ALLOWED_COMPRESSIONS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_NESTED_ARCHIVES = (".docx", ".docm", ".pptx", ".pptm", ".xlsx", ".xlsm",
                    ".zip", ".7z", ".rar")


class DocxAdmissionRejected(ValueError):
    """Untrusted archive does not satisfy the bounded ZIP metadata policy."""


def _member_name(name: str) -> str:
    """Reject ambiguous archive paths without resolving or extracting them."""
    if (not name or len(name) > MAX_NAME_BYTES
            or "\\" in name or ":" in name or name.startswith("/")
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise DocxAdmissionRejected("DOCX member name rejected")
    try:
        if len(name.encode("utf-8", "strict")) > MAX_NAME_BYTES:
            raise DocxAdmissionRejected("DOCX member name rejected")
    except UnicodeError as exc:
        raise DocxAdmissionRejected("DOCX member name rejected") from exc
    path = name[:-1] if name.endswith("/") else name
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise DocxAdmissionRejected("DOCX member path rejected")
    return unicodedata.normalize("NFC", name).casefold()


def _bounded_directory_header(payload: bytes) -> None:
    """Reject oversized ZIP indexes *before* zipfile materializes entries.

    Read only the fixed end-of-central-directory record. ZIP64, split ZIPs,
    trailing data and large archive comments are intentionally unsupported
    in this limited DOCX pilot. This is not a ZIP parser or CRC validator.
    """
    minimum = max(0, len(payload) - (22 + 65535))
    index = payload.rfind(b"PK\\x05\\x06", minimum)
    while index >= minimum:
        if index + 22 <= len(payload):
            (_, disk, directory_disk, disk_count, total_count,
             directory_bytes, directory_offset, comment_bytes) = (
                struct.unpack_from("<4sHHHHIIH", payload, index))
            if index + 22 + comment_bytes == len(payload):
                if (disk or directory_disk or disk_count != total_count
                        or not 1 <= total_count <= MAX_MEMBERS
                        or comment_bytes > 1024
                        or directory_bytes > MAX_DIRECTORY_BYTES
                        or directory_offset + directory_bytes != index
                        or directory_bytes < 46 * total_count):
                    raise DocxAdmissionRejected("DOCX ZIP directory bounds rejected")
                return
        index = payload.rfind(b"PK\\x05\\x06", minimum, index)
    raise DocxAdmissionRejected("DOCX ZIP directory missing")


def inspect_docx_container(payload: object) -> dict[str, int]:
    """Examine bounded ZIP directory metadata; NEVER open/decompress a member.

    The return value contains only aggregate sizes and counts, no source text,
    filenames, document identifiers, URLs or approval decisions. A passing
    result is *not* ZIP CRC validation, XML validation or DOCX conversion.
    """
    if (type(payload) is not bytes
            or not 4 <= len(payload) <= MAX_DOCX_BYTES
            or not payload.startswith(b"PK\x03\x04")):
        raise DocxAdmissionRejected("DOCX container header or size rejected")
    _bounded_directory_header(payload)
    names: set[str] = set()
    required: set[str] = set()
    expanded = 0
    try:
        with zipfile.ZipFile(BytesIO(payload), "r") as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_MEMBERS:
                raise DocxAdmissionRejected("DOCX member count rejected")
            for item in entries:
                key = _member_name(item.filename)
                if key in names:
                    raise DocxAdmissionRejected("DOCX duplicate member rejected")
                names.add(key)
                if item.flag_bits & 1:
                    raise DocxAdmissionRejected("DOCX encrypted member rejected")
                if item.compress_type not in _ALLOWED_COMPRESSIONS:
                    raise DocxAdmissionRejected("DOCX compression rejected")
                mode = (item.external_attr >> 16) & stat.S_IFMT(0o777777)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise DocxAdmissionRejected("DOCX special-file member rejected")
                if item.file_size < 0 or item.compress_size < 0:
                    raise DocxAdmissionRejected("DOCX invalid sizes")
                if item.is_dir() and (item.file_size or mode == stat.S_IFREG):
                    raise DocxAdmissionRejected("DOCX invalid directory")
                if (item.file_size > MAX_MEMBER_BYTES
                        or (item.file_size and
                            (not item.compress_size
                             or item.file_size > MAX_MEMBER_RATIO * item.compress_size))):
                    raise DocxAdmissionRejected("DOCX expansion limit rejected")
                expanded += item.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise DocxAdmissionRejected("DOCX total expansion rejected")
                lower = item.filename.casefold()
                if (lower.endswith(_NESTED_ARCHIVES)
                        or lower.endswith("vbaproject.bin")
                        or lower.startswith(("word/activex/", "word/embeddings/"))):
                    raise DocxAdmissionRejected("DOCX active or nested content rejected")
                if item.filename in _REQUIRED and not item.is_dir():
                    if item.file_size == 0:
                        raise DocxAdmissionRejected("DOCX empty required member")
                    required.add(item.filename)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError) as exc:
        raise DocxAdmissionRejected("Invalid DOCX ZIP container") from exc
    if required != _REQUIRED:
        raise DocxAdmissionRejected("DOCX required members missing")
    return {"archive_bytes": len(payload), "members": len(names),
            "expanded_bytes_claimed": expanded}
