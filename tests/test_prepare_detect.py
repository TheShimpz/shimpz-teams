"""Attachment type detection from bytes, never from names or uploaded types (ADR-0093)."""

import unittest

from prepare import detect, limits
from tests import prepare_fixtures


class DetectTests(unittest.TestCase):
    def test_images_and_pdfs_are_recognized_by_their_bytes_alone(self) -> None:
        cases = (
            (prepare_fixtures.image("JPEG", (4, 4)), detect.IMAGE, "image/jpeg"),
            (prepare_fixtures.image("PNG", (4, 4)), detect.IMAGE, "image/png"),
            (prepare_fixtures.image("WEBP", (4, 4)), detect.IMAGE, "image/webp"),
            (prepare_fixtures.text_pdf("hello"), detect.PDF, "application/pdf"),
        )
        for data, kind, media_type in cases:
            with self.subTest(media_type=media_type):
                self.assertEqual(detect.detect("document.txt", data), detect.Detected(kind, media_type))

    def test_text_subtypes_follow_the_extension_only_for_valid_text(self) -> None:
        self.assertEqual(detect.detect("a.csv", b"a,b\n1,2\n").media_type, "text/csv")
        self.assertEqual(detect.detect("a.md", "# Título\n".encode()).media_type, "text/markdown")
        self.assertEqual(detect.detect("a.JSON", b'{"a": [1, 2]}').media_type, "application/json")
        self.assertEqual(detect.detect("a.json", b"{not json").media_type, "text/plain")
        self.assertEqual(detect.detect("main.py", b"print('hi')\n").media_type, "text/plain")
        self.assertEqual(detect.detect("a.txt", b"\xef\xbb\xbfwith a BOM").kind, detect.TEXT)

    def test_binary_and_disguised_bytes_are_opaque(self) -> None:
        opaque = detect.Detected(detect.OPAQUE, detect.OCTET_STREAM)
        for name, data in (
            ("photo.jpg", b"not really a jpeg"[:0] + b"\x00\x01\x02binary"),
            ("notes.txt", b"\xff\xfe invalid utf-8"),
            ("notes.txt", b"text with a NUL\x00 byte"),
            ("notes.txt", b"text with an escape \x1b[31m"),
            ("notes.txt", b"delete \x7f"),
            ("image.gif", prepare_fixtures.image("GIF", (4, 4))),
            ("empty.txt", b""),
            ("bom.txt", b"\xef\xbb\xbf"),
        ):
            with self.subTest(name=name, data=data[:16]):
                self.assertEqual(detect.detect(name, data), opaque)

    def test_text_decoding_and_json_detection_stay_within_the_text_ceiling(self) -> None:
        self.assertIsNone(detect.decode_text(b"a" * (limits.MAX_TEXT_SOURCE_BYTES + 1)))
        self.assertIsNone(detect.decode_text(b"\xc3("))
        self.assertIsNone(detect.decode_text(b"\xef\xbb\xbf"))
        self.assertFalse(detect._valid_json(b"[" + b" " * limits.MAX_TEXT_SOURCE_BYTES + b"]"))


if __name__ == "__main__":
    unittest.main()
