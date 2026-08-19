from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from reading_pack_bot.errors import PackValidationError
from reading_pack_bot.pack import load_pack
from tests.helpers import FIXTURE, FIXTURE_SHA256, digest


class PackTests(unittest.TestCase):
    def load(self, path=FIXTURE, expected_hash=FIXTURE_SHA256):
        return load_pack(
            path,
            expected_sha256=expected_hash,
            max_bytes=524288,
        )

    def mutated(self, transform):
        raw = transform(FIXTURE.read_bytes())
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "pack.md"
        path.write_bytes(raw)
        return path, digest(raw)

    def test_golden_fixture(self):
        pack = self.load()
        self.assertEqual(pack.sha256, FIXTURE_SHA256)
        self.assertEqual(
            pack.name,
            "Reading Pack for *Clockwork Garden*",
        )
        self.assertEqual(
            pack.description,
            "data for AI input, not a substitute for the book",
        )
        self.assertEqual(pack.header["profile"], "nonfiction-reading:required")
        self.assertEqual(pack.end_counts["chapters"], 2)

    def test_h1_without_description_keeps_the_full_name(self):
        path, checksum = self.mutated(
            lambda raw: raw.replace(
                b"# Reading Pack for *Clockwork Garden*"
                b" \xe2\x80\x94 data for AI input, not a substitute for the book\n",
                b"# Reading Pack for *Clockwork Garden*\n",
                1,
            )
        )
        pack = self.load(path, checksum)
        self.assertEqual(pack.name, "Reading Pack for *Clockwork Garden*")
        self.assertIsNone(pack.description)

    def test_h1_uses_the_final_separator_between_name_and_description(self):
        path, checksum = self.mutated(
            lambda raw: raw.replace(
                b"# Reading Pack for *Clockwork Garden*",
                b"# Reading Pack for *Clockwork \xe2\x80\x94 Garden*",
                1,
            )
        )
        pack = self.load(path, checksum)
        self.assertEqual(pack.name, "Reading Pack for *Clockwork \u2014 Garden*")
        self.assertEqual(
            pack.description,
            "data for AI input, not a substitute for the book",
        )

    def test_optional_policy_end_count_is_accepted(self):
        path, checksum = self.mutated(
            lambda raw: raw.replace(b" | ref=1\n", b" | ref=1 | policy=6\n", 1)
        )
        pack = self.load(path, checksum)
        self.assertEqual(pack.end_counts["policy"], 6)

    def test_hash_mismatch_stops_before_parse(self):
        with self.assertRaisesRegex(PackValidationError, "operator pin"):
            self.load(expected_hash="0" * 64)

    def test_malformed_expected_hash(self):
        with self.assertRaisesRegex(PackValidationError, "malformed"):
            self.load(expected_hash="abc")

    def test_hash_is_computed_when_pin_is_omitted(self):
        pack = self.load(expected_hash=None)
        self.assertEqual(pack.sha256, FIXTURE_SHA256)

    def test_oversize(self):
        with self.assertRaisesRegex(PackValidationError, "byte limit"):
            load_pack(FIXTURE, expected_sha256=FIXTURE_SHA256, max_bytes=1024)

    def test_bom_rejected(self):
        path, checksum = self.mutated(lambda raw: b"\xef\xbb\xbf" + raw)
        with self.assertRaisesRegex(PackValidationError, "BOM"):
            self.load(path, checksum)

    def test_invalid_utf8_rejected(self):
        path, checksum = self.mutated(lambda raw: raw[:-1] + b"\xff\n")
        with self.assertRaisesRegex(PackValidationError, "UTF-8"):
            self.load(path, checksum)

    def test_nul_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"# Reading", b"#\x00Reading", 1))
        with self.assertRaisesRegex(PackValidationError, "NUL"):
            self.load(path, checksum)

    def test_crlf_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"\n", b"\r\n"))
        with self.assertRaisesRegex(PackValidationError, "LF"):
            self.load(path, checksum)

    def test_missing_final_lf_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.rstrip(b"\n"))
        with self.assertRaisesRegex(PackValidationError, "exactly one LF"):
            self.load(path, checksum)

    def test_leading_blank_rejected(self):
        path, checksum = self.mutated(lambda raw: b"\n" + raw)
        with self.assertRaisesRegex(PackValidationError, "first physical"):
            self.load(path, checksum)

    def test_duplicate_pack_line_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"\n\n#", b"\n" + raw.split(b"\n", 1)[0] + b"\n#", 1))
        with self.assertRaisesRegex(PackValidationError, "exactly one PACK"):
            self.load(path, checksum)

    def test_missing_required_header_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b" | basis=", b" | source=", 1))
        with self.assertRaisesRegex(PackValidationError, "basis"):
            self.load(path, checksum)

    def test_missing_section_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"## BIB |", b"## BOOK |", 1))
        with self.assertRaisesRegex(PackValidationError, "SYS, BIB"):
            self.load(path, checksum)

    def test_duplicate_section_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"## BIB |", b"## SYS |", 1))
        with self.assertRaisesRegex(PackValidationError, "duplicated"):
            self.load(path, checksum)

    def test_missing_h1_name_rejected(self):
        path, checksum = self.mutated(
            lambda raw: raw.replace(b"# Reading Pack for", b"Reading Pack for", 1)
        )
        with self.assertRaisesRegex(PackValidationError, "one non-empty H1"):
            self.load(path, checksum)

    def test_duplicate_h1_name_rejected(self):
        path, checksum = self.mutated(
            lambda raw: raw.replace(
                b"# Reading Pack for", b"# Duplicate name\n\n# Reading Pack for", 1
            )
        )
        with self.assertRaisesRegex(PackValidationError, "one non-empty H1"):
            self.load(path, checksum)

    def test_empty_h1_name_rejected(self):
        path, checksum = self.mutated(
            lambda raw: raw.replace(raw.splitlines()[2], b"# ", 1)
        )
        with self.assertRaisesRegex(PackValidationError, "one non-empty H1"):
            self.load(path, checksum)

    def test_unresolved_template_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"# Reading", b"# {{ Reading }}", 1))
        with self.assertRaisesRegex(PackValidationError, "template"):
            self.load(path, checksum)

    def test_unknown_end_count_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b" | ref=1\n", b" | ref=1 | secret=1\n", 1))
        with self.assertRaisesRegex(PackValidationError, "exactly"):
            self.load(path, checksum)

    def test_retired_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"status=canonical", b"status=retired", 1))
        with self.assertRaisesRegex(PackValidationError, "retired"):
            self.load(path, checksum)

    def test_unknown_status_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b"status=canonical", b"status=approved", 1))
        with self.assertRaisesRegex(PackValidationError, "unsupported"):
            self.load(path, checksum)

    def test_missing_required_end_count_rejected(self):
        path, checksum = self.mutated(lambda raw: raw.replace(b" | gloss=1", b"", 1))
        with self.assertRaisesRegex(PackValidationError, "exactly"):
            self.load(path, checksum)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pack.md"
            path.symlink_to(FIXTURE)
            with self.assertRaisesRegex(PackValidationError, "symbolic"):
                self.load(path, FIXTURE_SHA256)

    def test_directory_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            with self.assertRaises(PackValidationError):
                self.load(path, FIXTURE_SHA256)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pack.fifo"
            os.mkfifo(path)
            with self.assertRaisesRegex(PackValidationError, "regular file"):
                self.load(path, FIXTURE_SHA256)


if __name__ == "__main__":
    unittest.main()
