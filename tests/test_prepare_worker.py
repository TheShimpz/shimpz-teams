"""The preparation helper's worker: bounded decoding, fresh pixels, and PDF text only (ADR-0093)."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from PIL import Image

from prepare import limits, worker
from tests import prepare_fixtures

ROOT = Path(__file__).resolve().parents[1]


def _answer(kind: str, data: bytes) -> dict[str, object]:
    return worker.answer(worker.encode_request(kind, data))


def _opened(answer: dict[str, object]) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(answer["base64"])))


class ImageWorkerTests(unittest.TestCase):
    def test_a_large_photo_becomes_a_bounded_metadata_free_jpeg(self) -> None:
        answer = _answer("image", prepare_fixtures.oriented_jpeg((3600, 2700), orientation=1))
        self.assertEqual(answer["type"], "image")
        self.assertEqual(answer["media_type"], "image/jpeg")
        self.assertLessEqual(max(answer["width"], answer["height"]), limits.DERIVATIVE_LONG_EDGE)
        self.assertLessEqual(answer["width"] * answer["height"], limits.DERIVATIVE_PIXELS)
        self.assertLessEqual(len(base64.b64decode(answer["base64"])), limits.MAX_DERIVATIVE_BYTES)
        with _opened(answer) as derivative:
            self.assertEqual(derivative.size, (answer["width"], answer["height"]))
            self.assertEqual(dict(derivative.getexif()), {})
            self.assertNotIn("icc_profile", derivative.info)
            self.assertNotIn("exif", derivative.info)

    def test_orientation_is_applied_before_the_metadata_is_dropped(self) -> None:
        answer = _answer("image", prepare_fixtures.oriented_jpeg((300, 100), orientation=6))
        self.assertEqual((answer["width"], answer["height"]), (100, 300))

    def test_transparency_keeps_a_png_and_webp_is_read(self) -> None:
        png = _answer("image", prepare_fixtures.image("PNG", (64, 32), mode="RGBA"))
        self.assertEqual((png["media_type"], png["width"], png["height"]), ("image/png", 64, 32))
        webp = _answer("image", prepare_fixtures.image("WEBP", (64, 32)))
        self.assertEqual(webp["media_type"], "image/jpeg")

    def test_hostile_or_unsupported_images_are_refused_with_a_closed_reason(self) -> None:
        cases = (
            (prepare_fixtures.image("PNG", (4000, 4000)), "too_large"),
            (prepare_fixtures.image("PNG", (limits.MAX_IMAGE_EDGE + 1, 2)), "too_large"),
            (prepare_fixtures.animated_webp(), "animated"),
            (prepare_fixtures.image("GIF", (8, 8)), "unsupported"),
            (prepare_fixtures.image("PNG", (8, 8))[:40], "unreadable"),
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "unreadable"),
        )
        for data, reason in cases:
            with self.subTest(reason=reason, size=len(data)):
                self.assertEqual(_answer("image", data), {"type": "opaque", "reason": reason})


class PdfWorkerTests(unittest.TestCase):
    def test_pdf_text_is_extracted_page_by_page(self) -> None:
        answer = _answer("pdf", prepare_fixtures.text_pdf("Invoice 42", "Total 1000"))
        self.assertEqual(answer["type"], "text")
        self.assertIn("Invoice 42", answer["text"])
        self.assertIn("Total 1000", answer["text"])

    def test_encrypted_image_only_oversized_and_malformed_pdfs_are_refused(self) -> None:
        cases = (
            (prepare_fixtures.encrypted_pdf(), "encrypted"),
            (prepare_fixtures.blank_pdf(2), "no_text"),
            (prepare_fixtures.blank_pdf(limits.MAX_PDF_PAGES + 1), "too_large"),
            (b"%PDF-1.4\n garbage without objects", "unreadable"),
            (prepare_fixtures.text_pdf(*(["x" * 700] * 49)), "too_large"),
        )
        for data, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(_answer("pdf", data), {"type": "opaque", "reason": reason})


class FramingTests(unittest.TestCase):
    def test_only_an_exact_bounded_request_is_prepared(self) -> None:
        unreadable = {"type": "opaque", "reason": "unreadable"}
        for raw in (
            b"",
            b"no header separator",
            b'{"kind":"script","size":1}\nx',
            b'{"kind":"image","size":2}\nx',
            b'{"kind":"image","size":true}\nx',
            b'{"kind":"image","size":1,"extra":1}\nx',
            b"[1]\nx",
            b'{"kind":"pdf","size":0}\n',
            b"x" * (limits.MAX_HELPER_HEADER_BYTES + 1) + b"\n",
        ):
            with self.subTest(raw=raw[:40]):
                self.assertEqual(worker.answer(raw), unreadable)

    def test_the_entrypoint_writes_exactly_one_json_answer_and_nothing_to_stderr(self) -> None:
        request = worker.encode_request("pdf", prepare_fixtures.text_pdf("Hello from the helper"))
        completed = subprocess.run(
            [sys.executable, "-m", "prepare.worker"],
            input=request,
            capture_output=True,
            cwd=ROOT,
            check=True,
            timeout=30,
        )
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(json.loads(completed.stdout)["text"], "Hello from the helper")


if __name__ == "__main__":
    unittest.main()
