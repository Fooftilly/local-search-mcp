"""Offline-only ZIP metadata admission tests; no DOCX content is parsed.

Synthetic member payloads are not a valid Office document. This validates
only early archive rejection, NOT conversion quality or sandbox correctness.
"""
from __future__ import annotations

from io import BytesIO
from unittest.mock import patch
import stat
import struct
import unittest
import warnings
import zipfile

import office_docx_admission as subject


REQUIRED = [
    ("[Content_Types].xml", b"content-types"),
    ("_rels/.rels", b"relationships"),
    ("word/document.xml", b"body"),
]


def archive(entries=None, *, method=zipfile.ZIP_DEFLATED) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=method) as file:
        for name, content in REQUIRED if entries is None else entries:
            file.writestr(name, content)
    return stream.getvalue()


class DocxArchiveAdmissionTests(unittest.TestCase):
    def test_metadata_only_no_unzip_write_or_conversion(self):
        payload = archive(REQUIRED + [("word/media/résumé.png", b"image")])
        with patch.object(zipfile.ZipFile, "read",
                          side_effect=AssertionError("Entry decompression forbidden")):
            report = subject.inspect_docx_container(payload)
        self.assertEqual(report, {
            "archive_bytes": len(payload),
            "members": 4,
            "expanded_bytes_claimed": sum(len(value) for _, value in REQUIRED) + 5,
        })
        self.assertNotIn("document", str(report))
        self.assertNotIn("résumé", str(report))

    def test_reject_bad_header_wrong_types_truncated_container(self):
        valid = archive()
        for payload in (None, "", bytearray(valid), memoryview(valid), b"",
                        b"NONZIP" + valid, valid[:20], valid[:-22], b"PK\x03\x04"):
            with self.subTest(payload_type=type(payload).__name__,
                              length=len(payload) if hasattr(payload, "__len__") else 0):
                with self.assertRaises(subject.DocxAdmissionRejected):
                    subject.inspect_docx_container(payload)

    def test_reject_missing_empty_required_members(self):
        for entries in (
            REQUIRED[:2],
            [*REQUIRED[:2], ("word/document.xml", b"")],
            [*REQUIRED[:2], ("word/other.xml", b"not-a-document")],
        ):
            with self.subTest(entries=len(entries)):
                with self.assertRaises(subject.DocxAdmissionRejected):
                    subject.inspect_docx_container(archive(entries))

    def test_reject_duplicate_and_casefold_colliding_members(self):
        for name in ("word/document.xml", "WORD/DOCUMENT.XML"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                payload = archive(REQUIRED + [(name, b"another")])
            with self.subTest(name=name), self.assertRaisesRegex(
                    subject.DocxAdmissionRejected, "duplicate"):
                subject.inspect_docx_container(payload)

    def test_reject_noncanonical_path_names_and_controls(self):
        invalid = (
            "../escape", "word/../escape", "/absolute", "word//dup",
            "word/./document.xml", "word\\file", "C:/absolute",
            "word/file\n", "word/" + "x" * 200,
        )
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(archive(REQUIRED + [(name, b"body")]))

    def test_reject_macro_active_and_nested_containers(self):
        invalid = (
            "word/vbaProject.bin", "WORD/VBAPROJECT.BIN",
            "word/activeX/activeX1.bin", "word/embeddings/object.bin",
            "word/media/inner.docx", "word/media/inner.zip",
            "word/media/other.xlsx",
        )
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(archive(REQUIRED + [(name, b"payload")]))

    def test_reject_symlinks_and_unsupported_compression(self):
        link = zipfile.ZipInfo("word/media/linked.png")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(subject.DocxAdmissionRejected):
            subject.inspect_docx_container(archive(REQUIRED + [(link, b"target")]))
        with self.assertRaises(subject.DocxAdmissionRejected):
            subject.inspect_docx_container(archive(method=zipfile.ZIP_BZIP2))

    def test_reject_zipped_expansion_bomb_by_metadata(self):
        # A highly compressible ZIP body must be rejected before decompression.
        bomb = archive(REQUIRED + [("word/extra.xml", b"X" * 200000)])
        self.assertLess(len(bomb), 10000)
        with patch.object(zipfile.ZipFile, "read",
                          side_effect=AssertionError("No inflation permitted")):
            with self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(bomb)

    def test_member_total_archive_and_count_ceiling(self):
        data = archive(REQUIRED + [("word/extra.xml", b"extracontent")])
        with patch.object(subject, "MAX_DOCX_BYTES", len(data) - 1):
            with self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(data)
        with patch.object(subject, "MAX_MEMBER_BYTES", 10):
            with self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(data)
        with patch.object(subject, "MAX_EXPANDED_BYTES", 17):
            with self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(data)
        with patch.object(subject, "MAX_MEMBERS", 3):
            with self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(data)

    def test_oversized_directory_rejected_before_zipfile_allocation(self):
        original = bytearray(archive())
        index = original.rfind(b"PK\x05\x06")
        self.assertGreater(index, 0)

        cases = []
        too_many = bytearray(original)
        struct.pack_into("<H", too_many, index + 8, subject.MAX_MEMBERS + 1)
        struct.pack_into("<H", too_many, index + 10, subject.MAX_MEMBERS + 1)
        cases.append(bytes(too_many))

        too_large = bytearray(original)
        struct.pack_into("<I", too_large, index + 12,
                         subject.MAX_DIRECTORY_BYTES + 1)
        cases.append(bytes(too_large))

        wrong_offset = bytearray(original)
        struct.pack_into("<I", wrong_offset, index + 16, 0)
        cases.append(bytes(wrong_offset))

        split_archive = bytearray(original)
        struct.pack_into("<H", split_archive, index + 4, 1)
        cases.append(bytes(split_archive))

        for payload in cases:
            with self.subTest(payload_size=len(payload)), patch.object(
                    zipfile, "ZipFile",
                    side_effect=AssertionError("Unbounded ZIP index allocated")):
                with self.assertRaises(subject.DocxAdmissionRejected):
                    subject.inspect_docx_container(payload)
        with patch.object(subject, "MAX_DIRECTORY_BYTES", 40), patch.object(
                zipfile, "ZipFile",
                side_effect=AssertionError("Unbounded ZIP index allocated")):
            with self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(bytes(original))

    def test_zip64_locator_cannot_override_preparsed_directory_limits(self):
        # Insert a genuine ZIP64 end record + locator in front of an
        # otherwise ordinary EOCD. Ordinary EOCD fields are made plausible
        # by counting both new records as part of its directory byte size;
        # zipfile would instead apply the ZIP64 metadata if reached.
        original = archive()
        index = original.rfind(b"PK\x05\x06")
        self.assertGreater(index, 0)
        central_size = struct.unpack_from("<I", original, index + 12)[0]
        central_offset = struct.unpack_from("<I", original, index + 16)[0]
        zip64_record = struct.pack(
            "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0,
            10000, 10000, 499000, central_offset)
        zip64_locator = struct.pack(
            "<4sIQI", b"PK\x06\x07", 0,
            central_offset + central_size, 1)
        eocd = bytearray(original[index:])
        struct.pack_into("<I", eocd, 12,
                         central_size + len(zip64_record) + len(zip64_locator))
        forged = original[:index] + zip64_record + zip64_locator + eocd
        with patch.object(
                zipfile, "ZipFile",
                side_effect=AssertionError("ZIP64 must fail before allocation")):
            with self.assertRaisesRegex(subject.DocxAdmissionRejected, "ZIP64"):
                subject.inspect_docx_container(forged)

    def test_forged_eocd_count_cannot_hide_more_raw_entries(self):
        # A small directory can contain more actual records than EOCD claims.
        # Reject before zipfile constructs even the first ZipInfo object.
        entries = REQUIRED + [
            (f"word/extra{i}.xml", b"x") for i in range(subject.MAX_MEMBERS)
        ]
        data = bytearray(archive(entries))
        index = data.rfind(b"PK\x05\x06")
        self.assertGreater(index, 0)
        struct.pack_into("<H", data, index + 8, len(REQUIRED))
        struct.pack_into("<H", data, index + 10, len(REQUIRED))
        with patch.object(
                zipfile, "ZipFile",
                side_effect=AssertionError("Forged count must fail preparse")):
            with self.assertRaises(subject.DocxAdmissionRejected):
                subject.inspect_docx_container(bytes(data))

    def test_zipfile_metadata_exceptions_are_normalized(self):
        original = archive()
        central = original.find(b"PK\x01\x02")
        self.assertGreater(central, 0)
        invalid_utf8 = bytearray(original)
        # UTF-8 filename flag, with invalid encoded filename bytes in the
        # central-directory entry (not a decompression or path access).
        struct.pack_into("<H", invalid_utf8, central + 8,
                         struct.unpack_from("<H", original, central + 8)[0] | 0x800)
        invalid_utf8[central + 46] = 0xFF

        unsupported_version = bytearray(original)
        unsupported_version[central + 6] = 255

        for data in (bytes(invalid_utf8), bytes(unsupported_version)):
            with self.subTest(case=data[central + 6:central + 10]):
                with self.assertRaisesRegex(
                        subject.DocxAdmissionRejected, "Invalid DOCX ZIP container"):
                    subject.inspect_docx_container(data)

    def test_encrypted_member_flag_fails_closed(self):
        # Modify metadata bits only; this does not decrypt or decompress data.
        data = bytearray(archive())
        central = data.find(b"PK\x01\x02")
        self.assertGreater(central, 0)
        data[central + 8] |= 0x01
        with self.assertRaises(subject.DocxAdmissionRejected):
            subject.inspect_docx_container(bytes(data))


if __name__ == "__main__":
    unittest.main()
